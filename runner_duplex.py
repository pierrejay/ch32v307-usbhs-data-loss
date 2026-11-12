#!/usr/bin/env python3
"""runner.py's test for the two-loop images, on Linux, with the host writing and reading at once:

    sudo python3 -B runner_duplex.py --duplex --ep3 --count 5000   (both loopbacks at once)
    sudo python3 -B runner_duplex.py --duplex --ep3 --ep-in 0x82 --count 5000   (-split images)

Same protocol and verdicts as `runner.py`:
indexed ASCII records, each burst one 3078-byte write to 0x01 then one 3078-byte read from 0x81,
2000 ms each, stop at the first difference. A read cut by its deadline counts what arrived, as
PyUSB does, so a lost packet shows as a short echo. It talks to usbfs directly: no libusb needed.
"""
import argparse
import ctypes
import os
import select
import sys
import threading
import time
from pathlib import Path

VID, PID = '1a86', '5537'
EP_OUT, EP_IN = 0x01, 0x81
RECORDS_PER_BURST, RECORD_SIZE = 171, 18
BURST_SIZE = RECORDS_PER_BURST * RECORD_SIZE  # 3078 = 6 * 512 + 6
# Records are numbered with six digits: beyond this many bursts, an index would take a seventh.
MAX_BURSTS = 999999 // RECORDS_PER_BURST


class Urb(ctypes.Structure):
    _fields_ = [('type', ctypes.c_ubyte), ('endpoint', ctypes.c_ubyte), ('status', ctypes.c_int),
                ('flags', ctypes.c_uint), ('buffer', ctypes.c_void_p), ('buffer_length', ctypes.c_int),
                ('actual_length', ctypes.c_int), ('start_frame', ctypes.c_int),
                ('number_of_packets', ctypes.c_int), ('error_count', ctypes.c_int),
                ('signr', ctypes.c_uint), ('usercontext', ctypes.c_void_p)]


class Bulk(ctypes.Structure):
    _fields_ = [('ep', ctypes.c_uint), ('len', ctypes.c_uint), ('timeout', ctypes.c_uint),
                ('data', ctypes.c_void_p)]


def ioc(direction, number, size):
    return direction << 30 | size << 16 | ord('U') << 8 | number


BULK = ioc(3, 2, ctypes.sizeof(Bulk))
SUBMITURB = ioc(2, 10, ctypes.sizeof(Urb))
DISCARDURB = ioc(0, 11, 0)
REAPURBNDELAY = ioc(1, 13, ctypes.sizeof(ctypes.c_void_p))
CLAIMINTERFACE = ioc(2, 15, ctypes.sizeof(ctypes.c_uint))
URB_TYPE_BULK = 3

# The C ioctl, so that structures go to the kernel by address: Python's fcntl.ioctl passes an integer
# as a C int, too small for an address, and copies a small buffer to a temporary one, while the
# kernel keeps a submitted URB's address until it is reaped.
LIBC = ctypes.CDLL(None, use_errno=True)
LIBC.ioctl.argtypes = (ctypes.c_int, ctypes.c_ulong, ctypes.c_void_p)


def ioctl(fd, request, structure):
    result = LIBC.ioctl(fd, request, ctypes.addressof(structure))
    if result < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return result


def make_record(index):
    letters = bytes(65 + ((index + n) % 26) for n in range(10))
    return b'%06d|' % index + letters + b'\n'


def make_burst(number):
    first = number * RECORDS_PER_BURST
    return b''.join(make_record(first + n) for n in range(RECORDS_PER_BURST))


def open_device():
    found = [d for d in Path('/sys/bus/usb/devices').iterdir()
             if (d / 'idVendor').exists() and (d / 'idVendor').read_text().strip() == VID
             and (d / 'idProduct').read_text().strip() == PID]
    if len(found) != 1:
        sys.exit(f'INVALID: expected one {VID}:{PID} device, found {len(found)}')
    bus, dev = (int((found[0] / name).read_text()) for name in ('busnum', 'devnum'))
    fd = os.open(f'/dev/bus/usb/{bus:03d}/{dev:03d}', os.O_RDWR)
    interface = ctypes.c_uint(0)
    ioctl(fd, CLAIMINTERFACE, interface)
    print(f'device {VID}:{PID} at bus {bus} device {dev}, speed {(found[0] / "speed").read_text().strip()} Mb/s')
    return fd, found[0]


def endpoints(device):
    """The interface's bulk endpoints from sysfs, as the descriptors give them: the first OUT and
    IN for the first loopback, the second OUT and IN for the inverting one."""
    interface = device / f'{device.name}:1.0'
    addresses = sorted(int((e / 'bEndpointAddress').read_text(), 16) for e in interface.glob('ep_*'))
    outs, ins = [a for a in addresses if a < 0x80], [a for a in addresses if a >= 0x80]
    return outs[0], ins[0], outs[1], ins[1]


def write(fd, data, timeout_ms, ep=None):
    buffer = ctypes.create_string_buffer(data, len(data))
    bulk = Bulk(EP_OUT if ep is None else ep, len(data), timeout_ms, ctypes.addressof(buffer))
    return ioctl(fd, BULK, bulk)


def read_sync(fd, ep, length, timeout_ms):
    """One IN transfer by the synchronous ioctl, which keeps clear of the main reader's URB reaping."""
    buffer = ctypes.create_string_buffer(length)
    bulk = Bulk(ep, length, timeout_ms, ctypes.addressof(buffer))
    length = ioctl(fd, BULK, bulk)
    return buffer.raw[:length]


EP3_OUT, EP4_IN, PACKET, CHUNK = 0x03, 0x84, 512, 8


def ep3_packet(n):
    return b'%08d' % n + bytes((n * 7 + k) & 0xff for k in range(PACKET - 8))


def ep3_stream(fd, timeout_ms, done, stop, failure, stats):
    """The CH372 example's second loopback, at the same time as EP1: each 512-byte packet written
    to 0x03 comes back inverted from 0x84, one at a time (the device NAKs 0x03 until 0x84 is read).
    One thread writes 8-packet transfers until `done`; this one reads packet by packet."""
    started = [0]
    written_all = threading.Event()

    def writer():
        number = 0
        while not done.is_set() and not stop.is_set():
            data = b''.join(ep3_packet(number + k) for k in range(CHUNK))
            started[0] = number + CHUNK
            try:
                if write(fd, data, timeout_ms, EP3_OUT) != len(data):
                    failure.append(f'EP3 write incomplete at packet {number}')
                    stop.set()
                    return
            except OSError as error:
                failure.append(f'EP3 write stalled at packets {number}..{number + CHUNK - 1}: {error}')
                stop.set()
                return
            number += CHUNK
        written_all.set()

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    got, recent = 0, []
    while not stop.is_set():
        if got >= started[0]:
            if written_all.is_set() and got >= started[0]:
                break
            time.sleep(0.0005)
            continue
        try:
            # One chunk per read, as written: the host keeps polling 0x84 between packets.
            echo = read_sync(fd, EP4_IN, CHUNK * PACKET, timeout_ms)
        except OSError as error:
            failure.append(f'EP4 read stalled waiting for packets {got}..{got + CHUNK - 1}: {error}; '
                           f'last received {recent}')
            stop.set()
            return
        for offset in range(0, max(len(echo), CHUNK * PACKET), PACKET):
            piece = echo[offset:offset + PACKET]
            plain = bytes(b ^ 0xff for b in piece)
            if plain != ep3_packet(got):
                index = plain[:8].decode('ascii', 'replace')
                body = 'body matches its index' if index.isdigit() and plain == ep3_packet(int(index)) else 'body differs'
                failure.append(f'EP4 packet {got}: received {len(piece)} bytes, index {index!r} ({body}), '
                               f'starting {piece[:12].hex(" ")}; last received {recent}; read returned {len(echo)} bytes')
                stop.set()
                return
            recent = (recent + [got])[-4:]
            got += 1
    stats.append(got)


def read(fd, length, timeout_ms):
    """One IN transfer; at the deadline it is cancelled and what arrived is returned. Only the main
    thread submits URBs: a reap returns any of the descriptor's URBs."""
    buffer = ctypes.create_string_buffer(length)
    urb = Urb(type=URB_TYPE_BULK, endpoint=EP_IN, buffer=ctypes.addressof(buffer), buffer_length=length)
    ioctl(fd, SUBMITURB, urb)
    deadline = time.monotonic() + timeout_ms / 1000
    reaped = ctypes.c_void_p()
    discarded = False
    while True:
        try:
            ioctl(fd, REAPURBNDELAY, reaped)
            break
        except BlockingIOError:
            left = deadline - time.monotonic()
            if left <= 0 and not discarded:
                ioctl(fd, DISCARDURB, urb)
                discarded = True
            select.select([], [fd], [], max(left, 0.1))
    if urb.status not in (0, -2, -104):  # -ENOENT or -ECONNRESET: discarded at the deadline
        raise OSError(-urb.status, os.strerror(-urb.status))
    return buffer.raw[:urb.actual_length]


def duplicate_check(fd, timeout_ms):
    """For the toggle and nyet-dataup images: a burst whose first packet starts with DUPTEST!
    makes the device expect the other toggle, so the second packet looks like a retransmission,
    to be acknowledged and dropped. The echo must be the burst without bytes 512 to 1024."""
    sent = b'DUPTEST!' + make_burst(0)[8:]
    if write(fd, sent, timeout_ms) != len(sent):
        print('FAIL abort\n  duplicate test: OUT write incomplete')
        return 1
    echo = read(fd, BURST_SIZE, timeout_ms)
    if echo != sent[:512] + sent[1024:]:
        print(f'FAIL duplicate\n  received {len(echo)} bytes, expected the burst without [512:1024]')
        return 1
    print('PASS duplicate: second packet acknowledged and dropped, the others echoed')
    return 0


def run(fd, count, timeout_ms):
    print(f'{count} bursts; each is one {BURST_SIZE}-byte write to 0x{EP_OUT:02x}, then one read from 0x{EP_IN:02x}')
    sent = received = 0
    for number in range(count):
        expected = make_burst(number)
        sent += len(expected)
        try:
            written = write(fd, expected, timeout_ms)
        except OSError as error:
            print(f'FAIL abort\n  OUT write stalled at burst {number}: {error}')
            return 1
        if written != len(expected):
            print(f'FAIL abort\n  OUT write accepted {written} of {len(expected)} bytes at burst {number}')
            return 1
        try:
            echo = read(fd, BURST_SIZE, timeout_ms)
        except OSError as error:
            print(f'FAIL abort\n  IN read stalled at burst {number}: {error}')
            return 1
        received += len(echo)
        if echo == expected:
            continue
        report(number, expected, echo)
        return 1
    print(f'PASS\n  {count}/{count} bursts echoed byte-for-byte\n  {sent} bytes written; {received} bytes received')
    return 0


def duplex(fd, count, timeout_ms, ep3=False):
    """Writes and reads at once, as a network device carries traffic both ways: one thread writes
    the bursts back to back while the main thread reads the echo and checks it burst by burst.
    The device's ring holds back the writer (NAK) when the reader falls behind."""
    print(f'{count} bursts; written to 0x{EP_OUT:02x} by one thread while 0x{EP_IN:02x} is read, duplex')
    stop, failure = threading.Event(), []

    def writer():
        for number in range(count):
            if stop.is_set():
                return
            data = make_burst(number)
            try:
                if write(fd, data, timeout_ms) != len(data):
                    failure.append(f'OUT write incomplete at burst {number}')
                    return
            except OSError as error:
                failure.append(f'OUT write stalled at burst {number}: {error}')
                return

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    done, ep3_stats = threading.Event(), []
    if ep3:
        print(f'and at once, 512-byte packets written to 0x{EP3_OUT:02x} come back inverted from 0x{EP4_IN:02x}')
        ep3_thread = threading.Thread(target=ep3_stream, args=(fd, timeout_ms, done, stop, failure, ep3_stats),
                                      daemon=True)
        ep3_thread.start()
    received = 0
    for number in range(count):
        if stop.is_set():
            print(f'FAIL abort\n  {failure[0]}\n  {number} EP1 bursts were byte-exact')
            return 1
        expected = make_burst(number)
        try:
            echo = read(fd, BURST_SIZE, timeout_ms)
        except OSError as error:
            stop.set()
            print(f'FAIL abort\n  IN read stalled at burst {number}: {error}')
            return 1
        received += len(echo)
        if echo != expected:
            if stop.is_set() and failure:
                # The EP3/EP4 stream failed first and stopped the EP1 writer.
                print(f'FAIL abort\n  {failure[0]}\n  {number} EP1 bursts were byte-exact')
                return 1
            stop.set()
            report(number, expected, echo)
            if failure:
                print(f'  also: {failure[0]}')
            return 1
    thread.join()
    done.set()
    if ep3:
        ep3_thread.join(timeout_ms / 1000 * 3)
    if failure:
        print(f'FAIL abort\n  {failure[0]}')
        return 1
    if ep3 and not ep3_stats:
        print('FAIL abort\n  EP3/EP4 stream did not finish')
        return 1
    print(f'PASS\n  {count}/{count} bursts echoed byte-for-byte, duplex\n  {received} bytes received')
    if ep3:
        print(f'  {ep3_stats[0]} EP3/EP4 packets echoed inverted, in order')
    return 0


def report(number, expected, echo):
    print(f'FAIL integrity\n  burst {number}: wrote {len(expected)} bytes, received {len(echo)}')
    missing = len(expected) - len(echo)
    starts = [s for s in range(len(echo) + 1)
              if missing > 0 and echo[:s] == expected[:s] and echo[s:] == expected[s + missing:]]
    if len(starts) == 1:
        print(f'  {missing}-byte range [{starts[0]}:{starts[0] + missing}] is missing from the echo')
        print('  all other bytes are exact and in order')
    else:
        first = next((i for i, (a, b) in enumerate(zip(expected, echo)) if a != b), min(len(expected), len(echo)))
        print(f'  first difference at byte {first}')
    print(f'  {number} earlier bursts were byte-exact')


def main():
    global EP_IN
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--count', type=int, default=1000, help='number of bursts')
    parser.add_argument('--timeout', type=int, default=2000, help='ms per USB transfer')
    parser.add_argument('--duplicate', action='store_true', help='first, the duplicate test (toggle and nyet-dataup images)')
    parser.add_argument('--duplex', action='store_true', help='write and read at once')
    parser.add_argument('--ep3', action='store_true', help='with --duplex, the EP3/EP4 loopback at the same time')
    parser.add_argument('--ep-in', type=lambda s: int(s, 0), default=0x81, help='the IN endpoint of the first loopback')
    parser.add_argument('--auto-eps', action='store_true', help='take the endpoints from the descriptors')
    args = parser.parse_args()
    if not 1 <= args.count <= MAX_BURSTS:
        parser.error(f'--count must be between 1 and {MAX_BURSTS}; run longer tests as several invocations')
    if args.timeout < 1:
        parser.error('--timeout must be positive')
    # Four threads share the interpreter: hand it over fast, so that each USB completion is acted on promptly.
    sys.setswitchinterval(0.0001)
    EP_IN = args.ep_in
    fd, device = open_device()
    if args.auto_eps:
        global EP_OUT, EP3_OUT, EP4_IN
        EP_OUT, EP_IN, EP3_OUT, EP4_IN = endpoints(device)
        print(f'endpoints from the descriptors: 0x{EP_OUT:02x} 0x{EP_IN:02x} 0x{EP3_OUT:02x} 0x{EP4_IN:02x}')
    if args.duplicate and duplicate_check(fd, args.timeout):
        return 1
    if args.duplex:
        return duplex(fd, args.count, args.timeout, args.ep3)
    return run(fd, args.count, args.timeout)


if __name__ == '__main__':
    sys.exit(main())
