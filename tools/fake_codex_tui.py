"""Isolated test terminal. Never allowed by the production native-process guard.

Launch manually in a disposable iTerm2 Session. Prints a synthetic capacity frame,
records input bytes to an explicitly requested output, then renders BUSY on CR.
"""
import argparse
import json
import os
import select
import sys
import termios
import time
import tty


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--duration", type=int, default=120)
    args = parser.parse_args()
    if not sys.stdin.isatty():
        parser.error("requires a disposable terminal")
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    started = time.monotonic()
    with open(args.output, "x", encoding="utf-8") as log:
        os.chmod(args.output, 0o600)
        try:
            tty.setraw(fd)
            sys.stdout.write("\x1b[2J\x1b[H\x1b[19;1H⚠ Selected model is at capacity. Please try a different model."
                             "\x1b[21;1H› \x1b[23;1H? for shortcuts\x1b[21;3H")
            sys.stdout.flush()
            pending = bytearray()
            while time.monotonic() - started < args.duration:
                if not select.select([fd], [], [], .25)[0]:
                    continue
                data = os.read(fd, 4096)
                log.write(json.dumps({"at": time.monotonic() - started, "hex": data.hex()}) + "\n")
                log.flush()
                if b"\x03" in data:
                    break
                pending.extend(data)
                if b"\r" in data:
                    sys.stdout.write("\x1b[19;1H\x1b[2K• Working (1s • esc to interrupt)\x1b[21;1H\x1b[2K› ")
                else:
                    sys.stdout.buffer.write(data)
                sys.stdout.flush()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
            sys.stdout.write("\r\nIsolated test terminal stopped.\r\n")


if __name__ == "__main__":
    main()

