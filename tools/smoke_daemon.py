"""Exercise the real daemon/control socket in an isolated temporary state directory.

Connects read-only to iTerm2 if available. Never watches or writes any session.
"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time


def main():
    executable = Path(sys.executable).with_name("cliretry")
    with tempfile.TemporaryDirectory(prefix="cliretry-smoke-", dir="/tmp") as temporary:
        directory = Path(temporary)
        config = directory / "config.toml"
        config.write_text(f'[daemon]\nstate_dir = "{directory}"\n', encoding="utf-8")
        with (directory / "daemon.stderr").open("w+") as stderr:
            process = subprocess.Popen([str(executable), "daemon", "--config", str(config)],
                                       stdout=subprocess.DEVNULL, stderr=stderr)
            try:
                deadline = time.monotonic() + 15
                while not (directory / "control.sock").exists():
                    if process.poll() is not None or time.monotonic() > deadline:
                        stderr.seek(0)
                        raise RuntimeError(f"daemon failed: {stderr.read()}")
                    time.sleep(.05)

                def command(name, *args):
                    result = subprocess.run([str(executable), name, "--config", str(config), "--json", *args],
                                            text=True, capture_output=True, timeout=10)
                    if result.returncode:
                        raise RuntimeError(result.stderr)
                    return json.loads(result.stdout)

                status = command("status")
                assert status["ok"] and status["result"]["sessions"] == []
                duplicate = subprocess.run([str(executable), "daemon", "--config", str(config)],
                                           text=True, capture_output=True, timeout=10)
                assert duplicate.returncode != 0 and "DAEMON_ALREADY_RUNNING" in duplicate.stderr
                stopped = command("shutdown")
                assert stopped["ok"]
                process.wait(timeout=15)
                assert process.returncode == 0
                print(json.dumps({"daemon_start": "passed", "empty_status": "passed",
                                  "duplicate_rejected": "passed", "shutdown": "passed",
                                  "iterm_connected_at_status": status["result"]["connected"],
                                  "session_input_calls": 0}))
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=15)


if __name__ == "__main__":
    main()

