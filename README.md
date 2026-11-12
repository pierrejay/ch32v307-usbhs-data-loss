# CH32V307 USBHS: both loopbacks at once

This branch adds one test to [`main`](https://github.com/pierrejay/ch32v307-usbhs-data-loss): the
two loopbacks of WCH's example busy at the same time. The firmware, the build, the competing
interrupt and the two causes of the loss are those of `main`'s README. Only what changes is
described here.

It shows the hardware limit described in `main`'s README
([The auto toggle and a shared endpoint number](https://github.com/pierrejay/ch32v307-usbhs-data-loss#the-auto-toggle-and-a-shared-endpoint-number)):
with the auto toggle, when one endpoint number carries both directions, a CPU write to its IN side
can undo the SIE's flip of its OUT toggle, and two OUT packets are lost.

## Recommended fix

The auto toggle, with each endpoint on its own number: `multi-toggle-split`, which is `main`'s
`toggle` with EP1's IN on 0x82. With both loopbacks busy, it loses nothing at 5 and 20 us, and the
host sends no PING before every OUT packet. `multi-nyet` (`main`'s `nyet-dataup`) holds too, without
moving any endpoint, at the cost of that PING.

What fails is the auto toggle while EP1 carries both directions (`multi-toggle`, `multi-toggle-in`).

## What changes

- WCH's example has a second loopback: the interrupt handler sends every EP3 OUT packet back
  inverted on EP4 IN. The `multi-*` images keep it busy together with EP1.
- Every `multi-*` image arms IN with one store, so that only reception differs, and applies its
  reception policy to EP1 and EP3.
- The `-split` images answer EP1's loopback on 0x82 instead of 0x81. For them, the Makefile also
  patches `usb_desc.c`.
- `runner_duplex.py` writes and reads at once on both loopbacks. Linux only: it talks to usbfs.

## Run it

```sh
make build POLICY=multi-toggle WORK_US=20
minichlink -w build-multi-toggle-20us/firmware.bin flash -b
sudo python3 runner_duplex.py --duplex --ep3 --count 5000     # add --ep-in 0x82 for -split images
```

## Results

CH32V307WCU6, Linux host, 4 runs of 5000 bursts or the first failure:

| Image | Reception | EP1 IN | 5 us | 20 us |
|---|---|---|---|---|
| `multi-vanilla` | WCH's (`TOG_OK`) | 0x81, from the main loop | loss (burst 1443) | loss (burst 6) |
| `multi-nyet` | `nyet` | 0x81, from the main loop | 4 × 5000 | 4 × 5000 |
| `multi-toggle` | `toggle` | 0x81, from the main loop | 4 × 5000 | loss (run 4, burst 4495) |
| `multi-toggle-split` (recommended) | `toggle` | 0x82, from the main loop | 4 × 5000 | 4 × 5000 |
| `multi-toggle-in` | `toggle`, auto toggle on IN too | 0x81, from the main loop | loss (twice) | – |
| `multi-toggle-in-split` | the same | 0x82, from the main loop | 4 × 5000 | – |
| `multi-toggle-in-isr` | the same | 0x81, in the interrupt only | 4 × 5000 | – |

Every `toggle` loss is two OUT packets on EP1: the 6-byte tail and the next packet. Arming EP1 IN
only inside the interrupt, while the pending flag makes the device NAK every token, also avoids it.

## The cause, with no delayed interrupt

The `multi-hammer-*` images start from `multi-toggle-in-isr`, which loses nothing, and add one line
to the main loop: it rewrites a register with its own value, interrupts off. No competing work
(0 us):

| Image | Register rewritten | 0 us | 5 us |
|---|---|---|---|
| `multi-hammer-ep1-txlen` | `UEP1_TX_LEN` | loss at burst 1 | – |
| `multi-hammer-ep1-txctrl` | `UEP1_TX_CTRL` | loss at burst 0 | – |
| `multi-hammer-ep2-txlen` | `UEP2_TX_LEN`, unused | 2 × 5000 | – |
| `multi-nyet-hammer-ep1-txlen` | `UEP1_TX_LEN`, on `multi-nyet` (manual toggles) | 2 × 5000 | 4 × 5000 |

A write that changes nothing, on EP1's IN side, is enough to lose EP1 OUT packets with the auto
toggle. The same write on another number, or with manual toggles, is harmless.

## License

As on `main`: see `LICENSE`.
