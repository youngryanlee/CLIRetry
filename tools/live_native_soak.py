"""Create four owned idle native Codex tabs and run the real OBSERVE resource gate."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import shlex
from types import SimpleNamespace

import iterm2

from cliretry.adapters.iterm2_adapter import Iterm2Adapter
from cliretry.config import Config
from soak_observe import run as observe_run


async def run(args):
    root = Path(args.output_dir).absolute()
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    adapter = Iterm2Adapter(Config())
    window = None
    session_ids = []
    result = {"source": "live_native_codex_observe", "pid": os.getpid(), "complete": False}
    try:
        await adapter.connect()
        executable = str(Path(args.executable).resolve(strict=True))
        command = [executable, "--no-daemon", "--no-alt-screen", "--sandbox", "read-only",
                   "--ask-for-approval", "never", "-c", "check_for_update_on_startup=false",
                   "-C", str(Path.home())]
        for index in range(4):
            profile = iterm2.LocalWriteOnlyProfile()
            profile.set_name(f"CLIRetry OBSERVE soak {index + 1}/4")
            profile.set_use_custom_command(iterm2.Profile.USE_CUSTOM_COMMAND_ENABLED)
            profile.set_command(shlex.join(command))
            if window is None:
                window = await asyncio.wait_for(iterm2.Window.async_create(
                    adapter.connection, profile_customizations=profile), 15)
                if window is None:
                    raise RuntimeError("First native test tab exited")
                tab = window.tabs[0]
                result["window_id"] = window.window_id
            else:
                tab = await asyncio.wait_for(window.async_create_tab(
                    profile_customizations=profile, select=False), 15)
                if tab is None:
                    raise RuntimeError("Native test tab exited")
            session_ids.append(tab.sessions[0].session_id)
            result["session_ids"] = list(session_ids)
            (root / "ownership.json").write_text(json.dumps(result, indent=2))
        await asyncio.sleep(3)
        await adapter.close()
        print(json.dumps({"started": True, "pid": os.getpid(), "session_count": len(session_ids),
                          "duration_s": args.duration, "output_dir": str(root)}), flush=True)
        await observe_run(SimpleNamespace(output_dir=str(root / "observation"), config=None,
                                         session=session_ids, executable=executable, duration=args.duration))
        result["complete"] = True
    finally:
        if window is not None:
            try:
                await adapter.connect()
                await adapter.rpc(adapter.app.async_refresh())
                owned = next((w for w in adapter.app.windows if w.window_id == window.window_id), None)
                if owned is not None:
                    current_ids = {s.session_id for tab in owned.tabs for s in tab.sessions}
                    if not current_ids <= set(session_ids):
                        raise RuntimeError("Test window now contains an unowned session; leaving it open")
                    await asyncio.wait_for(owned.async_close(force=True), 5)
                result["owned_window_closed"] = True
            except Exception as exc:
                result["cleanup_error"] = str(exc)
        await adapter.close()
        (root / "result.json").write_text(json.dumps(result, indent=2))
        print(json.dumps(result), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--duration", type=int, default=7200)
    args = parser.parse_args()
    if args.duration < 1:
        parser.error("duration must be positive")
    asyncio.run(run(args))
