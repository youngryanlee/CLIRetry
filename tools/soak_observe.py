"""Read-only resource run for explicitly selected isolated iTerm2 sessions.

Refuses input at the adapter boundary. Run only after the read-only API probe
succeeds. Four sessions and at least 7200 seconds are required for the live gate.
"""
import argparse
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import time

import psutil

from cliretry.adapters.iterm2_adapter import Iterm2Adapter
from cliretry.config import load_config
from cliretry.daemon import Daemon
from cliretry.models import Mode, PAUSED, RetryError


class ObserveOnlyAdapter(Iterm2Adapter):
    def __init__(self, config):
        super().__init__(config)
        self.input_calls = 0

    async def guarded_type(self, *args, **kwargs):
        self.input_calls += 1
        raise AssertionError("Observe soak attempted input")

    async def guarded_submit(self, *args, **kwargs):
        self.input_calls += 1
        raise AssertionError("Observe soak attempted Enter")


async def run(args):
    root = Path(args.output_dir).expanduser().absolute()
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.umask(0o077)
    # Short control path avoids macOS sockaddr_un limits; evidence stays in root.
    with tempfile.TemporaryDirectory(prefix="clr-soak-", dir="/tmp") as socket_dir:
        config = load_config(args.config)
        config = replace(config, daemon=replace(config.daemon, state_dir=str(root / "state"),
                          socket_path=str(Path(socket_dir) / "control.sock")))
        adapter = ObserveOnlyAdapter(config)
        daemon = Daemon(config, adapter=adapter)
        process = psutil.Process()
        process.cpu_percent(None)
        task = asyncio.create_task(daemon.run())
        started = time.monotonic()
        summary = {"source": "live_iterm2", "complete": False, "input_calls": 0,
                   "sessions": args.session, "requested_duration_s": args.duration,
                   "disconnected_samples": 0, "paused_samples": 0, "samples": 0,
                   "unverifiable_samples": 0,
                   "max_rss_bytes": 0, "max_cpu_percent": 0}
        try:
            async with asyncio.timeout(45):
                while not adapter.connected or daemon.store is None or daemon.server is None:
                    if task.done():
                        await task
                        raise RuntimeError("Daemon stopped before connecting")
                    await asyncio.sleep(.25)
            for sid in args.session:
                await daemon.command("watch", {"session": sid, "executable": args.executable})
            started = time.monotonic()
            with (root / "resources.jsonl").open("x", encoding="utf-8") as output:
                while time.monotonic() - started < args.duration:
                    if task.done():
                        await task
                        raise RuntimeError("Daemon stopped during observation")
                    states = {sid: {"state": b.state, "reason": b.reason}
                              for sid, b in daemon.bindings.items()}
                    assert all(b.mode == Mode.OBSERVE for b in daemon.bindings.values())
                    assert not adapter.input_calls
                    rss, cpu = process.memory_info().rss, process.cpu_percent(None)
                    summary["samples"] += 1
                    summary["max_rss_bytes"] = max(summary["max_rss_bytes"], rss)
                    summary["max_cpu_percent"] = max(summary["max_cpu_percent"], cpu)
                    summary["disconnected_samples"] += int(not adapter.connected)
                    summary["paused_samples"] += int(any(b.state in PAUSED for b in daemon.bindings.values()))
                    summary["unverifiable_samples"] += int(any(b.reason in {
                        "COMPOSER_UNVERIFIABLE", "PROFILE_MISMATCH", "SNAPSHOT_INVALID"
                    } for b in daemon.bindings.values()))
                    output.write(json.dumps({"elapsed_s": time.monotonic() - started,
                                             "rss_bytes": rss, "cpu_percent": cpu,
                                             "connected": adapter.connected, "states": states}) + "\n")
                    output.flush()
                    if daemon.store.failed or daemon.connection_error == "MONITOR_FAILED":
                        raise RetryError("MONITOR_FAILED")
                    await asyncio.sleep(min(5, max(0, args.duration - (time.monotonic() - started))))
            summary["complete"] = True
        finally:
            daemon.request_stop()
            try:
                await asyncio.wait_for(task, 20)
            except BaseException as exc:
                summary["complete"] = False
                summary["shutdown_error"] = type(exc).__name__
            summary["elapsed_s"] = time.monotonic() - started
            summary["input_calls"] = adapter.input_calls
            summary["duration_gate"] = len(args.session) == 4 and summary["elapsed_s"] >= 7200
            summary["requires_manual_review"] = True
            (root / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
            print(json.dumps(summary))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", action="append", required=True)
    parser.add_argument("--executable", required=True)
    parser.add_argument("--duration", type=int, default=7200)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--config")
    args = parser.parse_args()
    if not 1 <= len(args.session) <= 4 or len(set(args.session)) != len(args.session) or args.duration < 1:
        parser.error("Select 1–4 distinct sessions and a positive duration")
    asyncio.run(run(args))
