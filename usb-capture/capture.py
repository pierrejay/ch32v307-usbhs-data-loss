#!/usr/bin/env python3
"""Capture USB HS traffic while running the repository's loopback test."""

import argparse
import json
import signal
import subprocess
import sys
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sniffer", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--count", type=int, default=1000)
    parser.add_argument("--runner-timeout", type=int, default=10)
    args = parser.parse_args()

    if args.count < 1:
        parser.error("--count must be positive")
    if args.runner_timeout < 1:
        parser.error("--runner-timeout must be positive")
    if not args.sniffer.is_file():
        parser.error("--sniffer must name the usb_sniffer executable")

    root = Path(__file__).resolve().parent.parent
    runner_path = root / "runner.py"
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)

    sniffer_command = [
        str(args.sniffer.resolve()),
        "--capture",
        "--speed",
        "hs",
        "--fold",
        "--fifo",
        str(output / "wire.pcapng"),
    ]
    runner_command = [
        sys.executable,
        str(runner_path),
        "--count",
        str(args.count),
    ]
    metadata = {
        "utc_start": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sniffer_command": sniffer_command,
        "runner_command": runner_command,
    }
    capture = None

    try:
        with (output / "sniffer.log").open("w") as sniffer_log:
            capture = subprocess.Popen(
                sniffer_command, stdout=sniffer_log, stderr=subprocess.STDOUT
            )

            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if capture.poll() is not None:
                    raise RuntimeError("sniffer exited before capture started")
                if "Starting capture" in (output / "sniffer.log").read_text():
                    break
                time.sleep(0.02)
            else:
                raise RuntimeError("sniffer did not report capture ready")

            with (output / "runner.log").open("w") as runner_log:
                started = time.monotonic()
                runner = subprocess.Popen(
                    runner_command,
                    cwd=output,
                    stdout=runner_log,
                    stderr=subprocess.STDOUT,
                )
                try:
                    metadata["runner_exit"] = runner.wait(
                        timeout=args.runner_timeout
                    )
                except subprocess.TimeoutExpired:
                    runner.kill()
                    runner.wait()
                    metadata["invalid"] = "runner exceeded its timeout"
                metadata["runner_elapsed_s"] = time.monotonic() - started

            # The upstream sniffer folds idle frames once per second and its
            # libusb reads use a 250 ms timeout. Allow complete PCAPNG blocks
            # to reach disk before stopping the process.
            time.sleep(2)
            if capture.poll() is not None:
                metadata["invalid"] = "sniffer exited before requested stop"
    finally:
        if capture is not None:
            if capture.poll() is None:
                capture.send_signal(signal.SIGINT)
            try:
                metadata["sniffer_exit"] = capture.wait(timeout=3)
            except subprocess.TimeoutExpired:
                capture.kill()
                capture.wait()
                metadata["invalid"] = "sniffer did not stop"

        metadata["utc_end"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
        )
        if output.exists():
            (output / "metadata.json").write_text(
                json.dumps(metadata, indent=2) + "\n"
            )

    if (output / "runner.log").exists():
        print((output / "runner.log").read_text(), end="")
    if "invalid" in metadata:
        print("INVALID: %s" % metadata["invalid"])
        return 2
    return metadata.get("runner_exit", 2)


if __name__ == "__main__":
    sys.exit(main())
