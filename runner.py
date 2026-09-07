#!/usr/bin/env python3
"""Send an indexed ASCII stream to the WCH CH372 bulk echo and verify it."""

import argparse
import json
import os
import platform
import sys
import time

VID, PID = 0x1A86, 0x5537
EP_OUT, EP_IN = 0x01, 0x81
RECORDS_PER_BURST = 171
RECORD_SIZE = 18
BURST_SIZE = RECORDS_PER_BURST * RECORD_SIZE  # 3078 = 6 * 512 + 6
MAX_BURSTS = 999999 // RECORDS_PER_BURST


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


def contiguous_deletion_starts(expected, received):
    """Return every start offset that explains received by one deletion."""
    missing = len(expected) - len(received)
    if missing <= 0:
        return []
    return [
        start
        for start in range(len(received) + 1)
        if received[:start] == expected[:start]
        and received[start:] == expected[start + missing :]
    ]


def preview(data, start):
    return data[start : start + 54].decode("ascii", errors="replace").replace(
        "\n", "\\n"
    )


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
    except usb.core.USBError:
        device.set_configuration(1)
        configuration = device.get_active_configuration()
    if configuration.bConfigurationValue != 1:
        raise RuntimeError(
            "expected active configuration 1, got %d"
            % configuration.bConfigurationValue
        )
    usb.util.claim_interface(device, 0)
    return device


def save_failure(report, received, stamp):
    sequence = 0
    while True:
        suffix = "" if sequence == 0 else "-%d" % sequence
        stem = "ch372-fail-%s%s" % (stamp, suffix)
        json_path = stem + ".json"
        raw_path = stem + "-received.bin"
        if not os.path.exists(json_path) and not os.path.exists(raw_path):
            break
        sequence += 1

    with open(json_path, "x", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
        handle.write("\n")
    with open(raw_path, "xb") as handle:
        handle.write(received)
    print("  kept %s and %s" % (json_path, raw_path))


def run(device, count, timeout_ms, keep_artifacts=True):
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
            print("FAIL abort")
            print("  OUT write stalled at burst %d: %s" % (number, error))
            print("  %d bytes offered; %d bytes received" % (sent_bytes, received_bytes))
            return 1

        if written != len(expected):
            print("FAIL abort")
            print(
                "  OUT write accepted %d of %d bytes at burst %d"
                % (written, len(expected), number)
            )
            print("  %d bytes offered; %d bytes received" % (sent_bytes, received_bytes))
            return 1

        try:
            received = bytes(device.read(EP_IN, BURST_SIZE, timeout_ms))
        except Exception as error:
            print("FAIL abort")
            print("  IN read stalled at burst %d: %s" % (number, error))
            print("  %d bytes written; %d bytes received" % (sent_bytes, received_bytes))
            return 1

        received_bytes += len(received)
        if received == expected:
            continue

        difference = first_difference(expected, received)
        deletion_starts = contiguous_deletion_starts(expected, received)
        missing = len(expected) - len(received)
        missing_range = None
        if len(deletion_starts) == 1:
            missing_range = [deletion_starts[0], deletion_starts[0] + missing]
        now = time.gmtime()
        stamp = time.strftime("%Y%m%d-%H%M%SZ", now)
        report = {
            "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", now),
            "verdict": "FAIL integrity",
            "burst": number,
            "exact_bursts_before_failure": number,
            "expected_bytes": len(expected),
            "received_bytes": len(received),
            "first_difference": difference,
            "missing_range": missing_range,
            "timeout_ms": timeout_ms,
        }

        print("FAIL integrity")
        print(
            "  burst %d: wrote %d bytes, received %d"
            % (number, len(expected), len(received))
        )
        if missing_range is not None:
            print(
                "  %d-byte range [%d:%d] is missing from the echo"
                % (missing, missing_range[0], missing_range[1])
            )
            print("  all other bytes are exact and in order")
        elif deletion_starts:
            print(
                "  echo equals sent data with one contiguous %d-byte range removed"
                % missing
            )
            print("  missing range position is ambiguous")
        else:
            context = max(0, difference - RECORD_SIZE)
            print("  sent[%d:] |%s|" % (context, preview(expected, context)))
            print("  got [%d:] |%s|" % (context, preview(received, context)))
            print("  first difference at byte %d" % difference)
        print("  %d earlier bursts were byte-exact" % number)
        if keep_artifacts:
            save_failure(report, received, stamp)
        return 1

    print("PASS")
    print("  %d/%d bursts echoed byte-for-byte" % (count, count))
    print("  %d bytes written; %d bytes received" % (sent_bytes, received_bytes))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--count",
        type=int,
        default=1000,
        help="number of bursts (1-%d)" % MAX_BURSTS,
    )
    parser.add_argument("--timeout", type=int, default=2000, help="ms per USB transfer")
    parser.add_argument(
        "--no-artifacts",
        action="store_true",
        help="do not write failure JSON or received-byte files",
    )
    args = parser.parse_args()

    if args.count < 1:
        parser.error("--count must be positive")
    if args.count > MAX_BURSTS:
        parser.error("--count must not exceed %d" % MAX_BURSTS)
    if args.timeout < 1:
        parser.error("--timeout must be positive")

    try:
        device = open_device()
    except Exception as error:
        print("INVALID: %s: %s" % (type(error).__name__, error))
        return 2
    return run(device, args.count, args.timeout, not args.no_artifacts)


if __name__ == "__main__":
    sys.exit(main())
