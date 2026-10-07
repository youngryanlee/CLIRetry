from __future__ import annotations

import argparse
import asyncio
import json
import os
import stat
import sys
import uuid

from . import __version__
from .config import load_config
from .models import RetryError


def parse_tab_numbers(value):
    try:
        numbers = [int(part) for part in value.split(",")]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("use comma-separated positive tab numbers, e.g. 1,3,5") from exc
    if (not numbers or any(number < 1 for number in numbers)
            or len(numbers) != len(set(numbers))):
        raise argparse.ArgumentTypeError("tab numbers must be positive and unique")
    return numbers


def parser():
    p = argparse.ArgumentParser(prog="cliretry", description="Observe explicitly selected iTerm2 Codex sessions")
    p.add_argument("--version", action="version", version=f"cliretry {__version__}")
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("daemon", "sessions", "status", "doctor", "watch", "enable", "pause", "resume",
                 "unwatch", "shutdown", "inspect", "retry-current", "reset-budget", "logs", "request-result"):
        command = sub.add_parser(name)
        command.add_argument("--config")
        command.add_argument("--json", action="store_true")
        if name != "daemon":
            command.add_argument("--request-id", help="Idempotency key; do not reuse for a different action")
        if name in {"watch", "enable", "pause", "resume", "unwatch", "inspect", "retry-current", "reset-budget", "doctor", "status"}:
            group = command.add_mutually_exclusive_group(required=name not in {"status"})
            group.add_argument("--session")
            if name == "watch":
                group.add_argument("--current", action="store_true",
                                   help="Resolve the active iTerm2 session once; never follow focus")
                group.add_argument("--tabs", type=parse_tab_numbers,
                                   help="Watch tab positions in the current iTerm2 window, e.g. 1,3,5")
            if name in {"pause", "unwatch"}:
                group.add_argument("--all", action="store_true")
        if name == "watch":
            command.add_argument("--profile", default="codex-local-v1")
            command.add_argument("--executable")
        if name == "retry-current":
            command.add_argument("--evidence", required=True)
        if name == "logs":
            command.add_argument("--limit", type=int, default=30)
        if name == "request-result":
            command.add_argument("--id", required=True)
    return p


async def request(config, command, args, request_id):
    path = config.socket_path
    try:
        info = path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise RetryError("UNSAFE_SOCKET_PATH", str(path))
        reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(path, limit=1_048_576), 3)
    except (OSError, TimeoutError) as exc:
        raise RetryError("DAEMON_UNAVAILABLE", "Start cliretry daemon with the same config first", exit_code=3) from exc
    try:
        payload = {"v": 1, "request_id": request_id, "command": command, "args": args}
        writer.write(json.dumps(payload).encode() + b"\n")
        await writer.drain()
        try:
            raw = await asyncio.wait_for(reader.readline(), 30)
        except TimeoutError as exc:
            raise RetryError("REQUEST_OUTCOME_UNKNOWN", f"Do not replay; query request-result --id {request_id}", exit_code=5) from exc
        result = json.loads(raw)
        if result.get("request_id") != request_id or result.get("v") != 1:
            raise RetryError("INVALID_RESPONSE", exit_code=5)
        return result
    finally:
        writer.close()
        await writer.wait_closed()


def main():
    ns = parser().parse_args()
    try:
        config = load_config(ns.config)
        if ns.command == "daemon":
            from .daemon import Daemon
            asyncio.run(Daemon(config).run())
            return
        args = {k: v for k, v in vars(ns).items()
                if k not in {"command", "config", "json", "request_id"} and v is not None and v is not False}
        response = asyncio.run(request(config, ns.command, args, ns.request_id or str(uuid.uuid4())))
        if ns.json:
            print(json.dumps(response, ensure_ascii=False, indent=2))
        elif response["ok"]:
            print(json.dumps(response["result"], ensure_ascii=False, indent=2))
        else:
            print(f"{response['error']['code']}: {response['error']['message']}", file=sys.stderr)
        if not response["ok"]:
            raise SystemExit(response["error"]["exit_code"])
    except RetryError as exc:
        print(json.dumps({"ok": False, "error": {"code": exc.code, "message": str(exc)}})
              if ns.json else f"{exc.code}: {exc}", file=sys.stderr)
        raise SystemExit(exc.exit_code)
    except KeyboardInterrupt:
        raise SystemExit(130)


if __name__ == "__main__":
    main()
