# CH32V307 USBHS: data loss under interrupt contention

WCH's `CH372Device` USBHS example implements a bulk loopback on endpoint 1. A
host writes bytes to `0x01`; the firmware queues the received packets in its ring
buffer and sends them back on `0x81`.

The observable contract is simple: every completed host write must come back in
full, byte-for-byte and in order. On CH32V307VCT6 and CH32V307WCU6 devices, that
contract breaks when an unrelated interrupt at the same priority occasionally
holds the CPU for 5 us:

- 0 us of work: 1000/1000 bursts exact, 3,078,000 bytes returned;
- 5 us of work: the first incorrect burst returned 2,054 of 3,078 bytes. Two
  consecutive 512-byte blocks were missing; their position within the burst
  varies with the timer phase. Every returned byte remained exact and in order.

The base WCH USBHS driver, endpoint handling, descriptors and loopback body are
copied unchanged. The test adds one periodic timer interrupt, two optional
patches and one host-side comparator.

## Firmware

The `sdk/` submodule is pinned to WCH commit
`69a2eec903b4f919fcb73d1ab6c10c690780e4d1`. The reference example is:

```
sdk/EVT/EXAM/USB/USBHS/DEVICE/CH372Device/User
```

Seven files in `src/` are byte-identical to that directory, including the USBHS
interrupt handler, its endpoint logic and its descriptors. `make check-vendor`
verifies them with `cmp` before every build. The loopback body in `main.c` is also
unchanged; only the initialization around it differs.

The exact vendor EP1 OUT handler is visible at
[`ch32v30x_usbhs_device.c`, lines 447-466](https://github.com/openwch/ch32v307/blob/69a2eec903b4f919fcb73d1ab6c10c690780e4d1/EVT/EXAM/USB/USBHS/DEVICE/CH372Device/User/ch32v30x_usbhs_device.c#L447-L466).
The unmodified main loop also briefly disables `USBHS_IRQn` while updating the
ring-buffer consumer state, so the endpoint implementation already has to
tolerate non-zero interrupt service latency.

The only application changes are visible in two places:

- `main.c` removes the vendor's startup UART diagnostics and starts the test
  interrupt after USB initialization;
- `competing_irq.c` configures TIM2 every 997 us, at the same priority as USBHS.
  TIM2 acknowledges its own flag and occupies the CPU for the compiled-in
  duration. It never reads or writes a USB register.

997 us has no fixed phase relationship with the 125 us USB microframe, so USB
events encounter the competing ISR intermittently. The 0 us image still runs
TIM2 and pays the same interrupt-entry overhead; only the time spent in its
handler changes. The USB handler contains no injected delay.

The vendor configuration runs the core at 96 MHz. Five microseconds therefore
represents approximately 480 CPU cycles, or 4% of one high-speed microframe.

The build links directly with WCH's generic CH32V307
`EVT/EXAM/SRC/Ld/Link.ld`, using its default 288 KiB flash / 32 KiB RAM
allocation. The image uses about 10 KiB of static RAM and also fits devices
configured for the 192 KiB flash / 128 KiB RAM split.

## Host test

`runner.py` generates readable, indexed records:

```
000000|ABCDEFGHIJ
000001|BCDEFGHIJK
...
```

One burst contains 3,078 bytes: six 512-byte packets followed by six bytes. For
each burst the runner performs exactly:

```python
device.write(0x01, expected)
received = device.read(0x81, 3078)
```

It compares the echo immediately and stops at the first discrepancy. A short or
modified echo is `FAIL integrity`; a USB timeout is `FAIL progression`; a setup
failure is `INVALID`. An integrity failure also writes `ch372-fail.json` and the
received bytes to `ch372-fail-received.bin`.

## Setup

The build uses WCH GCC 12.2.0. `make toolchain` fetches a pinned native
package from Community-PIO for
[macOS](https://github.com/Community-PIO-CH32V/toolchain-riscv-mac) or
[Linux x86-64](https://github.com/Community-PIO-CH32V/toolchain-riscv-linux).

These packages were cross-checked against the GCC 12.2.0 toolchain shipped
in the official [MRS2 Toolchain & Debugger 2.4.0](https://www.mounriver.com/download)
distribution. The Linux build components used here are byte-for-byte
identical; on macOS, both distributions produce byte-for-byte identical
firmware.

### Linux

```sh
sudo apt install git make patch python3 python3-usb libusb-1.0-0
git submodule update --init
make toolchain  # Fetch WCH GCC toolchain
make            # Build all test binaries
```

Run the runner as `sudo`, or install a udev rule that grants access to
`1a86:5537`.

### macOS

```sh
brew install libusb python
git submodule update --init
make toolchain  # Fetch WCH GCC toolchain
make            # Build all test binaries
python3 -m venv .venv
.venv/bin/python -m pip install pyusb
```

Runner normally doesn't need root privileges for this custom USB device on macOS.

## 1. Reproduce the failure

`vanilla` is the default policy. It retains the WCH endpoint code byte-for-byte.

Build, flash and run the 0 us control firmware:

```sh
# Flash DUT
minichlink -w build-vanilla-0us/firmware.bin flash -b

# Run test
sudo python3 runner.py --count 1000       # Linux
.venv/bin/python runner.py --count 1000   # macOS
```

Expected result: `PASS`, with 3,078,000 bytes returned exactly.

Then repeat with 5 us of work in the competing interrupt:

```sh
# Flash DUT
minichlink -w build-vanilla-5us/firmware.bin flash -b

# Run test
sudo python3 runner.py --count 1000       # Linux
.venv/bin/python runner.py --count 1000   # macOS
```

Representative failure:

```sh
FAIL integrity
  burst 146: wrote 3078 bytes, received 2054
```
(The precise burst depends on the phase between TIM2 and USB traffic.)

A new runner process does not reset the device or its free-running timer.
Reset or reflash the device before starting an independent campaign.

The two images differ only in the duration of the TIM2 handler. The USB handler,
endpoint policy, loopback code and host stimulus are identical.

## 2. Evaluate two candidate workarounds

The repository carries two small patches for comparison under the same 5 us
contention:

| Policy | Change to EP1 OUT | Rationale |
|---|---|---|
| `no-tog-ok` | enqueue without checking `TOG_OK`; keep ACK | follow the acceptance pattern used by other WCH examples |
| `nyet` | retain the `TOG_OK` check; arm with NYET | preserve duplicate detection while applying host backpressure |

The patches are stored in `patches/` and applied only to a generated source copy
inside the build directory. The vendor source in `src/` remains unchanged.

The `no-tog-ok` policy matches the bulk OUT handling used by WCH's USBHS
NCM, ECM and RNDIS examples at the pinned SDK commit. TinyUSB's
[CH32 USBHS driver](https://github.com/hathach/tinyusb/blob/53f8c53c2cbd73a91a172f1ae35e9abc00eb5075/src/portable/wch/dcd_ch32_usbhs.c#L396-L416)
also processes OUT transfers without checking `TOG_OK`.

Caveats:

- `no-tog-ok` may pass a retransmitted packet to the application if the host
  did not receive the device's ACK, silently duplicating data. This condition
  is outside the scope of the loopback test below.
- `nyet` adds handshake/PING traffic under sustained OUT load and can reduce
  throughput. This reproducer verifies integrity; it does not quantify that
  overhead.

Test `no-tog-ok`:

```sh
minichlink -w build-no-tog-ok-5us/firmware.bin flash -b
sudo python3 runner.py --count 1000       # Linux
.venv/bin/python runner.py --count 1000   # macOS
```

Test `nyet`:

```sh
minichlink -w build-nyet-5us/firmware.bin flash -b
sudo python3 runner.py --count 1000       # Linux
.venv/bin/python runner.py --count 1000   # macOS
```

Expected output in both cases:

```sh
PASS
  1000/1000 bursts echoed byte-for-byte
  3078000 bytes written; 3078000 bytes received
```

## License

Original reproducer code and documentation are available under the MIT License.
Files copied or adapted from the WCH SDK retain their original WCH copyright
notices and usage terms. See `LICENSE` for the exact scope.
