#!/usr/bin/env python3
"""Send an indexed ASCII stream to the WCH CH372 bulk echo and verify it."""

import argparse
import json
import platform
import sys
import time

VID, PID = 0x1A86, 0x5537
EP_OUT, EP_IN = 0x01, 0x81
RECORDS_PER_BURST = 171
RECORD_SIZE = 18
BURST_SIZE = RECORDS_PER_BURST * RECORD_SIZE  # 3078 = 6 * 512 + 6


def make_record(index):
    letters = bytes(65 + ((index + n) % 26) for n in range(10))
    return b"%06d|" % index + letters + b"\n"


def make_burst(number):
    first = number * RECORDS_PER_BURST
    return b"".join(make_record(first + n) for n in range(RECORDS_PER_BURST))


def first_difference(expected, received):
    for offset, (left, right) in enumerate(zip(expected, received)):
        if left != right:
            return offset
    return min(len(expected), len(received))


def contiguous_slice(expected, received):
    """Return the source offset when received is one contiguous slice of expected."""
    if not received or len(received) >= len(expected):
        return None
    offset = expected.find(received)
    return offset if offset >= 0 else None


def preview(data):
    return data[:54].decode("ascii", errors="replace").replace("\n", "\\n")


def open_device():
    import usb.core
    import usb.util

    print(
        "host: %s; Python %s; PyUSB %s"
        % (
            platform.platform(),
            platform.python_version(),
            getattr(usb, "__version__", "unknown"),
        )
    )

    devices = list(usb.core.find(find_all=True, idVendor=VID, idProduct=PID))
    if len(devices) != 1:
        raise RuntimeError(
            "expected one %04x:%04x device, found %d" % (VID, PID, len(devices))
        )

    device = devices[0]
    try:
        configuration = device.get_active_configuration()
    except usb.core.USBError as error:
        if "Configuration not set" not in str(error):
            raise
        device.set_configuration(1)
        configuration = device.get_active_configuration()
    if configuration.bConfigurationValue != 1:
        raise RuntimeError(
            "expected active configuration 1, got %d"
            % configuration.bConfigurationValue
        )
    usb.util.claim_interface(device, 0)
    return device


def save_failure(report, received):
    json_path = "ch372-fail.json"
    raw_path = "ch372-fail-received.bin"
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
        handle.write("\n")
    with open(raw_path, "wb") as handle:
        handle.write(received)
    print("  kept %s and %s" % (json_path, raw_path))


def run(device, count, timeout_ms):
    sent_bytes = 0
    received_bytes = 0

    print(
        "%d bursts; each is one %d-byte write to 0x%02x, then one read from 0x%02x"
        % (count, BURST_SIZE, EP_OUT, EP_IN)
    )

    for number in range(count):
        expected = make_burst(number)
        sent_bytes += len(expected)

        try:
            written = device.write(EP_OUT, expected, timeout_ms)
        except Exception as error:  # PyUSB backend errors differ across hosts.
            print("FAIL progression")
            print("  OUT write stalled at burst %d: %s" % (number, error))
            print("  %d bytes offered; %d bytes received" % (sent_bytes, received_bytes))
            return 1

        if written != len(expected):
            print("FAIL progression")
            print(
                "  OUT write accepted %d of %d bytes at burst %d"
                % (written, len(expected), number)
            )
            print("  %d bytes offered; %d bytes received" % (sent_bytes, received_bytes))
            return 1

        try:
            received = bytes(device.read(EP_IN, BURST_SIZE, timeout_ms))
        except Exception as error:
            print("FAIL progression")
            print("  IN read stalled at burst %d: %s" % (number, error))
            print("  %d bytes written; %d bytes received" % (sent_bytes, received_bytes))
            return 1

        received_bytes += len(received)
        if received == expected:
            continue

        difference = first_difference(expected, received)
        source_offset = contiguous_slice(expected, received)
        report = {
            "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "verdict": "FAIL integrity",
            "burst": number,
            "exact_bursts_before_failure": number,
            "expected_bytes": len(expected),
            "received_bytes": len(received),
            "first_difference": difference,
            "received_is_sent_slice_at": source_offset,
        }

        print("FAIL integrity")
        print(
            "  burst %d: wrote %d bytes, received %d"
            % (number, len(expected), len(received))
        )
        print("  sent |%s|" % preview(expected))
        print("  got  |%s|" % preview(received))
        if source_offset is not None:
            end = source_offset + len(received)
            print("  received data equals sent bytes [%d:%d]" % (source_offset, end))
            if end == len(expected) and source_offset:
                print("  %d-byte prefix missing from the echo" % source_offset)
        else:
            print("  first difference at byte %d" % difference)
        print("  %d earlier bursts were byte-exact" % number)
        save_failure(report, received)
        return 1

    print("PASS")
    print("  %d/%d bursts echoed byte-for-byte" % (count, count))
    print("  %d bytes written; %d bytes received" % (sent_bytes, received_bytes))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=1000, help="number of bursts")
    parser.add_argument("--timeout", type=int, default=2000, help="ms per USB transfer")
    args = parser.parse_args()

    if args.count < 1:
        parser.error("--count must be positive")
    if args.timeout < 1:
        parser.error("--timeout must be positive")

    try:
        device = open_device()
    except Exception as error:
        print("INVALID: %s: %s" % (type(error).__name__, error))
        return 2
    return run(device, args.count, args.timeout)


if __name__ == "__main__":
    sys.exit(main())
