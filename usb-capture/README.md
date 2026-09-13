# USB wire captures

These files record the USB High-Speed wire-level validation performed with:

- CH32V307VCT6 (UUID `72-a2-28-e7-d8-34-3c-ab`)
- MacBook Pro M1 2021, macOS 15.4 arm64
- [Alex Taradov's USB
  Sniffer](https://github.com/ataradov/usb-sniffer)
- sniffer software commit
  `0d26e7e6d3feda2fa585b0f403a54c4cf853e508`
- Wireshark/TShark 4.4.3

The DUT binaries came from repository commit
`c96fe04bc93217ba02573727a15b9f8e6d9ed6c0`.

## Files

| File | Contents |
|---|---|
| `control-0us.pcapng` | Validated 1,000-burst control: 7,000 ACKed OUT and 7,000 ACKed IN packets reconstruct 3,078,000 exact bytes in each direction |
| `failure-5us.pcapng` | Complete run failing at burst 93 |
| `failure-5us-burst93.pcapng` | Original frames 5358–5438 extracted from the failing capture; frame numbers are renumbered |
| `failure-5us.json` | Unmodified runner failure report |
| `failure-5us-received.bin` | The 2,560 bytes returned by the device |

The captures have complete PCAPNG blocks. Neither the control nor the failure
contains a sniffer overflow report or a Wireshark CRC5, CRC16, PID or PID
sequence error.

## Failing burst

In `failure-5us.pcapng`, burst 93 begins at frame 5360. Its decisive sequence
is:

| Frames | Transaction |
|---|---|
| 5376–5378 | P5, 512-byte DATA0, ACK |
| 5379–5381 | P6, 6-byte DATA1, NAK |
| 5382–5385 | PING/NAK, then PING/ACK |
| 5386–5388 | identical P6, 6-byte DATA1, ACK |
| 5390–5412 | P0–P4 returned on IN and acknowledged |
| 5413 onward | IN requests receive NAK; neither P5 nor P6 is returned |

The sniffer folds SOF/IN/NAK-only frames. This does not remove the decisive
OUT/DATA/handshake or PING transactions listed above.

## Recording another run

`capture.py` only starts the unchanged sniffer and the repository's unchanged
`runner.py`; it does not build or flash the DUT. Build the upstream sniffer,
flash the desired firmware and verify it before running:

```sh
.venv/bin/python usb-capture/capture.py \
  --sniffer /path/to/usb-sniffer/software/usb_sniffer \
  --output /tmp/ch372-wire \
  --count 1000
```

The output directory must not already exist. It receives `wire.pcapng`,
`runner.log`, `sniffer.log`, `metadata.json` and any timestamped failure
artifacts produced by `runner.py`. Capture continues for two seconds after the
runner exits so the sniffer can flush its buffered PCAPNG blocks.
