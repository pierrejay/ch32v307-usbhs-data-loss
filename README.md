# CH32V307 USBHS: data loss when the USB interrupt is delayed

WCH's `CH372Device` USBHS example sends back on `0x81` everything written to bulk endpoint `0x01`.
When another interrupt delays the USB interrupt by a few microseconds, the echo comes back short:
packets that the host saw acknowledged never reach the application.

This repository reproduces the loss with WCH's own code, explains it and fixes it. There are two
causes, each fixed by a small change:

| Direction | What goes wrong | Fix |
|---|---|---|
| Reception, from 1 us of delay | A late handler reads the `TOG_OK` bit of a later packet and drops a good one; two packets are lost | Arm EP1 OUT with NYET instead of ACK |
| Transmission, at 20 us | `USBHS_Endp_DataUp` arms IN with two stores; the second can undo a packet sent in between; one packet is lost | Arm IN with one store |

With both changes (`nyet-dataup`), 100,000 bursts at 20 us come back exact.

## The test

The firmware is WCH's example: the USB handler, endpoint code, descriptors and loopback are
byte-identical to the SDK. One thing is added: TIM2 interrupts every 997 us, at the USB interrupt's
priority, and keeps the CPU for `WORK_US` microseconds. It does not touch USB.

The host (`runner.py`) writes a burst of 3,078 bytes to `0x01` (six 512-byte packets and a 6-byte
tail), reads 3,078 bytes back from `0x81`, compares, and stops at the first difference.

## Run it

Linux (macOS: see [Setup](#setup)):

```sh
sudo apt install git make patch python3 python3-usb libusb-1.0-0
git submodule update --init
make toolchain      # WCH GCC 12.2.0
make                # the images used below
```

WCH's code with 5 us of competing work fails:

```sh
minichlink -w build-vanilla-5us/firmware.bin flash -b
sudo python3 runner.py --count 1000
```

```
FAIL integrity
  burst 310: wrote 3078 bytes, received 2560
  518-byte range [2560:3078] is missing from the echo
  all other bytes are exact and in order
  310 earlier bursts were byte-exact
```

The fix passes at 20 us, and a retransmitted packet is still dropped:

```sh
minichlink -w build-nyet-dataup-20us/firmware.bin flash -b
sudo python3 runner.py --duplicate --count 1000
```

Other images: `make build POLICY=name WORK_US=N`.

## Results

Each policy is one patch in `patches/`, applied to a copy of the sources at build time.

| Policy | Change | 0 us | 1 us | 5 us | 20 us |
|---|---|---|---|---|---|
| `vanilla` | none | 1000/1000 | loss | loss (burst 310, 480) | – |
| `nyet-dataup` | EP1 OUT armed with NYET, IN armed with one store | 1000/1000, dup | 1000/1000, dup | 5000/5000, dup | 100,000/100,000, dup |
| `nyet` | EP1 OUT armed with NYET | – | – | 1000/1000 | loss on IN (burst 3930) |
| `no-tog-ok` | EP1 OUT taken without checking `TOG_OK` | – | – | 1000/1000 | – |
| `toggle-rx` | EP1 OUT taken on the RX auto toggle's flip | – | – | – | loss on IN (burst 4011) |
| `toggle` | `toggle-rx`, IN armed with one store | 1000/1000, dup | 1000/1000, dup | 5000/5000, dup | 100,000/100,000, dup |

"dup": the duplicate test passed (`runner.py --duplicate`, see [Host runner](#host-runner)). "–": not
run. Devices: CH32V307VCT6 (macOS host) and CH32V307WCU6 (Linux host). `vanilla` loses from 1 us,
and in almost every 1,000-burst run from 4 us. With the host writing and reading at once,
`nyet-dataup` also runs 4 × 5000 bursts at 5 and 20 us without loss.

`no-tog-ok` is what WCH's NCM, ECM and RNDIS examples and
[TinyUSB](https://github.com/hathach/tinyusb/blob/53f8c53c2cbd73a91a172f1ae35e9abc00eb5075/src/portable/wch/dcd_ch32_usbhs.c#L396-L416)
do, but it passes a retransmitted packet to the application, which then gets the data twice.
`toggle` keeps duplicate detection too, but relies on the auto toggle, which needs IN and OUT on
different endpoint numbers (see [below](#the-auto-toggle-and-a-shared-endpoint-number)).

For the minimal fix, which keeps this example's endpoints, we recommend `nyet-dataup`. If IN and OUT
can also move to different endpoint numbers, `toggle` is the suitable fix: that is how the ch32-hal
driver avoids the problem.

## Why packets are lost

### Reception: `TOG_OK` read too late

The SIE raises a single transfer flag and describes the transaction in `INT_ST`: endpoint, token and
`TOG_OK`, which says whether the packet had the expected toggle. A retransmitted packet has the
wrong toggle, and WCH's handler drops packets without `TOG_OK`.

While the flag is pending, the device NAKs every token (`INT_BUSY`), but `INT_ST` keeps changing. It
describes the latest token on the bus, including one the device NAKed, and the flag rises before
`MIS_ST.SIE_FREE`, while `TOG_OK` is not final. This was also reported, with a Cynthion analyzer, in
[ch32-rs/ch32-hal#229](https://github.com/ch32-rs/ch32-hal/pull/229).

A handler entered late can therefore read the `TOG_OK` of the next packet, which the device NAKed.
It takes the good packet for a retransmission and drops it. Its expected toggle does not move, so the
next packet, when the host sends it again, is dropped too. Both were acknowledged on the wire.

`nyet` keeps WCH's handler and its `TOG_OK` test. With NYET, the host must get a PING acknowledged
before each OUT packet, and the device NAKs the PING while the flag is pending: the next OUT packet
waits for the handler.

### Transmission at 20 us: `USBHS_Endp_DataUp` stores twice

```c
USBHSD_UEP_TXCTRL( endp ) = (USBHSD_UEP_TXCTRL( endp ) &= ~USBHS_UEP_T_RES_MASK) | USBHS_UEP_T_RES_ACK;
```

This compiles to one read and two stores of the same value (`lbu; andi; sb; sb`).
`USBHS_UEP_T_RES_ACK` is 0, so the first store already arms the endpoint. If the competing
interrupt falls between the two stores, a packet can go out and its completion handler can flip the
toggle, before the second store writes back the old toggle. The next packet goes out with the same
PID and the host drops it as a retransmission. It takes a whole 512-byte packet between two
instructions, which is why it shows only at 20 us. One store fixes it.

### The auto toggle and a shared endpoint number

`toggle` lets the SIE flip EP1's OUT toggle itself, and takes a packet when the toggle has flipped,
read once the SIE is free. It does not depend on `INT_ST`.

But on the CH32V307, with the auto toggle, a CPU write to an endpoint's IN side (`UEPn_TX_LEN`,
`UEPn_TX_CTRL`) made as the SIE flips the toggle of the same number's OUT side undoes the flip. The
OUT packet is acknowledged but not taken, and the host's next packet, which now looks like a
retransmission, is acknowledged and dropped. Two OUT packets are lost.

Here EP1 carries both directions and the main loop arms EP1 IN. This loopback alone did not show the
loss in 100,000 bursts. With a second bulk loopback busy at the same time (EP3 to EP4, the host
writing and reading at once), `toggle` loses two OUT packets at 20 us, and at 5 us with the auto
toggle on IN as well. The loss goes away when EP1 IN moves to EP2, or when EP1 IN is armed only inside
the interrupt, while the pending flag makes the device NAK every token.

That last version shows the cause. Its main loop is given one more line, which rewrites
`UEP1_TX_LEN` with its own value, interrupts off: two OUT packets are lost within the first two
bursts, with no competing work. The same line on `UEP2_TX_LEN`, or on `UEP1_TX_LEN` with manual
toggles as in `nyet`, loses none in 10,000 bursts. `nyet-dataup` keeps manual toggles and is not
exposed. The two-loop images and their results are on the
[`multi-endpoint`](https://github.com/pierrejay/ch32v307-usbhs-data-loss/tree/multi-endpoint) branch.

## Evidence

The captures and how to read them are in [`usb-capture/`](usb-capture/README.md).

- Reception, `vanilla` at 5 us, macOS host. The 0 us control capture rebuilds all 3,078,000 bytes
  from 7,000 acknowledged OUT and 7,000 acknowledged IN packets. In the failing capture (burst 93),
  P5 is acknowledged; P6 is NAKed, then sent again after a PING and acknowledged; the device returns
  P0 to P4 only. Neither capture has a CRC or PID error.
- Reception, in RAM. Halted without reset after a failure, the board shows the ring advanced by five
  slots instead of seven, empty, and EP1 IN idle. P5 and P6 went to the same RX DMA slot, where the
  short P6 overwrote the start of P5, and neither was admitted to the ring.
- Transmission: `toggle-rx-20us-burst4011.pcapng` (P4 goes out twice as DATA0, P5 never) and
  `nyet-20us-burst3930.pcapng` (after one NAK, P2 goes out as DATA1 like P1, and the host drops it).

## Details

### Firmware

The `sdk/` submodule is WCH's SDK at `69a2eec903b4f919fcb73d1ab6c10c690780e4d1`; the example is
`sdk/EVT/EXAM/USB/USBHS/DEVICE/CH372Device/User`. Seven files in `src/` are byte-identical to it,
and `make check` verifies them with `cmp` before every build. WCH's EP1 OUT handler is at
[`ch32v30x_usbhs_device.c`, lines 447-466](https://github.com/openwch/ch32v307/blob/69a2eec903b4f919fcb73d1ab6c10c690780e4d1/EVT/EXAM/USB/USBHS/DEVICE/CH372Device/User/ch32v30x_usbhs_device.c#L447-L466).
WCH's main loop itself briefly disables `USBHS_IRQn`, so its endpoint code already has to tolerate
some interrupt latency.

The application changes are:
- `main.c` drops the startup UART messages and starts the competing interrupt after USB init; the
  loopback body is unchanged;
- `competing_irq.c` sets `USBHS_IRQn` and `TIM2_IRQn` to priority 0/0 (the USB default, made
  explicit) and runs TIM2 every 997 us. Its handler acknowledges TIM2 and waits `WORK_US` on TIM3, a
  free-running 24 MHz timer it only reads. Neither timer touches a USB register.

997 us is not a multiple of the 125 us microframe: the phase moves by 3 us each period, so the
interrupt meets USB events at every point. The 0 us image still takes the interrupt; only the time
spent in it changes. The core runs at 96 MHz, as in WCH's example: 5 us is about 480 cycles, 4% of a
microframe. The image links with WCH's generic `Link.ld` (288 KiB flash / 32 KiB RAM) and uses about
10 KiB of RAM.

### Host runner

Each burst is indexed ASCII records (`000000|ABCDEFGHIJ`, ...), written with one
`device.write(0x01, …)` and read back with one `device.read(0x81, 3078)`, 2 s each. A short or
modified echo, including a partial read cut by the deadline, is `FAIL integrity`; a USB error or an
incomplete write is `FAIL abort`; a setup failure is `INVALID`. An integrity failure writes
`ch372-fail-<UTC>.json` and `ch372-fail-<UTC>-received.bin` (`--no-artifacts` turns that off).

`--duplicate` first sends a burst whose first packet starts with `DUPTEST!`. The `toggle` and
`nyet-dataup` images then expect the other toggle, so the host's next packet looks like a
retransmission: the echo must lack exactly bytes 512 to 1024, and the following bursts must be exact.

### Setup

`make toolchain` fetches WCH GCC 12.2.0 from Community-PIO, for
[macOS](https://github.com/Community-PIO-CH32V/toolchain-riscv-mac) or
[Linux x86-64](https://github.com/Community-PIO-CH32V/toolchain-riscv-linux). These packages match
the GCC 12.2.0 of WCH's [MRS2 Toolchain & Debugger 2.4.0](https://www.mounriver.com/download): the
Linux build components are byte-for-byte identical, and on macOS both build identical firmware.

On Linux, run the runner with `sudo`, or add a udev rule for `1a86:5537`. On macOS:

```sh
brew install libusb python
git submodule update --init
make toolchain
make
python3 -m venv .venv
.venv/bin/python -m pip install pyusb
.venv/bin/python runner.py --count 1000
```

## License

Original reproducer code and documentation are available under the MIT License.
Files copied or adapted from the WCH SDK retain their original WCH copyright
notices and usage terms. See `LICENSE` for the exact scope.
