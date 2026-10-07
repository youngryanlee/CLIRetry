"""Create and close only an owned disposable iTerm2 window; never sends input."""
import argparse
import asyncio
import json
from pathlib import Path
import shlex
import sys

import iterm2

from cliretry.adapters.iterm2_adapter import Iterm2Adapter
from cliretry.config import Config
from cliretry.models import as_json


async def run(args):
    output = Path(args.output_dir).expanduser().absolute()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    adapter = Iterm2Adapter(Config())
    window = None
    result = {"source": "live_iterm2_synthetic_tui", "input_calls": 0, "complete": False}
    try:
        await adapter.connect()
        custom = iterm2.LocalWriteOnlyProfile()
        custom.set_name("CLIRetry isolated read-only test")
        command = shlex.join([sys.executable, str(Path(__file__).with_name("fake_codex_tui.py").absolute()),
                              "--output", str(output / "stdin.jsonl"), "--duration", "30"])
        custom.set_use_custom_command(iterm2.Profile.USE_CUSTOM_COMMAND_ENABLED)
        custom.set_command(command)
        window = await asyncio.wait_for(iterm2.Window.async_create(
            adapter.connection, profile_customizations=custom), 10)
        if window is None:
            raise RuntimeError("No test window created")
        session = window.tabs[0].sessions[0]
        result["owned_window_id"] = window.window_id
        result["owned_session_id"] = session.session_id
        (output / "ownership.json").write_text(json.dumps(result))
        await asyncio.wait_for(session.async_set_grid_size(iterm2.Size(100, 24)), 5)
        await adapter.rpc(adapter.app.async_refresh())
        async with asyncio.timeout(5):
            while not (output / "stdin.jsonl").exists():
                await asyncio.sleep(.1)
        await asyncio.sleep(.5)
        f = await adapter.capture(session.session_id)
        (output / "frame.json").write_text(json.dumps(as_json(f), ensure_ascii=False))
        result.update({"transport": adapter.transport, "iterm_version": adapter.app_version(),
                       "sdk_version": adapter.sdk_version, "width": f.width, "height": f.height,
                       "observation": as_json(adapter.profile.classify(f)),
                       "styled_cells": sum(c.faint is not None for row in f.cells for c in row),
                       "variables": f.variables, "complete": True})
    except Exception as exc:
        result["error_type"] = type(exc).__name__
        result["error"] = str(exc)
        raise
    finally:
        if window is not None:
            try:
                await asyncio.wait_for(window.async_close(force=True), 5)
                result["owned_window_closed"] = True
            except Exception as exc:
                result["cleanup_error"] = type(exc).__name__
        await adapter.close()
        (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True)
    asyncio.run(run(parser.parse_args()))
