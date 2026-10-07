"""Explicit opt-in capture of one selected session. Output contains terminal text.

Review and redact captures before sharing. Never runs as part of the daemon.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path

from cliretry.adapters.iterm2_adapter import Iterm2Adapter
from cliretry.config import load_config
from cliretry.models import RetryError, as_json, digest


APPROVAL_PROMPT_PHRASES = (
    "would you like to run",
    "would you like to allow",
    "do you want to run",
    "do you want to allow",
    "approve this command",
    "approval required",
    "requires approval",
    "requires your approval",
    "needs your approval",
)


def validate_approval_marker(marker):
    """Require a distinctive shell-safe token so generic conversation isn't captured."""
    if not isinstance(marker, str) or len(marker) < 16 or marker != marker.strip():
        raise ValueError("approval marker must be a trimmed string of at least 16 characters")
    if not marker.isascii() or any(not (char.isalnum() or char in "-_") for char in marker):
        raise ValueError("approval marker may contain only ASCII letters, digits, hyphens, and underscores")


def is_approval_capture_candidate(frame, marker):
    """A candidate must show both the caller's marker and an approval cue on one frame."""
    validate_approval_marker(marker)
    visible = "\n".join(frame.lines).casefold()
    return (marker.casefold() in visible
            and any(phrase in visible for phrase in APPROVAL_PROMPT_PHRASES))


def private_create(path):
    """Exclusive, private from the first byte, including under a permissive umask."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    return os.fdopen(fd, "w", encoding="utf-8")


async def capture(args):
    adapter = Iterm2Adapter(load_config(args.config))
    path = Path(args.output)
    manifest_path = path.with_name(path.name + ".manifest.json")
    # Fail early on an obvious collision; private_create below closes the race.
    if path.exists() or path.is_symlink() or manifest_path.exists() or manifest_path.is_symlink():
        raise SystemExit("Output or manifest already exists; choose a new path")
    output = None
    manifest_output = None
    try:
        if args.approval_marker is not None:
            try:
                validate_approval_marker(args.approval_marker)
            except ValueError as exc:
                raise SystemExit(str(exc)) from exc
        await adapter.connect()
        identity = None
        identity_error = "EXECUTABLE_NOT_SPECIFIED"
        paths = (args.executable,) if args.executable else adapter.config.profile.executable_paths
        if paths:
            try:
                identity = await adapter.identity(args.session, paths)
                identity_error = ""
            except RetryError as exc:
                identity_error = exc.code
        if args.approval_marker is not None and identity is None:
            raise SystemExit(f"Approval capture requires verified native Codex identity ({identity_error})")
        frames = []
        for _ in range(args.count):
            frame = await adapter.capture(args.session)
            if args.approval_marker is None or is_approval_capture_candidate(frame, args.approval_marker):
                frames.append(as_json(frame))
            await asyncio.sleep(args.interval)
        if identity is not None:
            await adapter.identity(args.session, paths, identity)
        if args.approval_marker is not None and not frames:
            raise SystemExit("No captured frame passed both approval-marker and approval-prompt gates")

        # No capture file is created until all approval gates have passed. A
        # miss therefore never leaves an ordinary conversation frame on disk.
        output = private_create(path)
        try:
            manifest_output = private_create(manifest_path)
        except BaseException:
            output.close()
            output = None
            path.unlink()
            raise
        json.dump({"source": "captured", "complete": False}, manifest_output)
        manifest_output.flush()
        for f in frames:
            output.write(json.dumps(f, ensure_ascii=False) + "\n")
        output.flush()
        os.fsync(output.fileno())
        manifest = {"schema_version": 1, "source": "captured", "complete": True,
                    "redacted": False, "session_id": args.session,
                    "iterm_version": adapter.app_version(), "sdk_version": adapter.sdk_version,
                    "profile_revision": adapter.profile.revision, "frame_count": len(frames),
                    "process_identity": as_json(identity), "identity_error": identity_error,
                    "frame_hashes": [digest(f) for f in frames]}
        if args.approval_marker is not None:
            manifest["approval_gate"] = {
                "marker_digest": digest(args.approval_marker),
                "prompt_cue": "allowlisted_text_on_same_frame",
            }
        manifest_output.seek(0)
        manifest_output.truncate()
        json.dump(manifest, manifest_output, ensure_ascii=False, indent=2)
        manifest_output.flush()
        os.fsync(manifest_output.fileno())
        print(f"Captured {len(frames)} frames to {path}. Contains terminal text; review before sharing.")
    finally:
        if output is not None:
            output.close()
        if manifest_output is not None:
            manifest_output.close()
        await adapter.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--session", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--config")
    p.add_argument("--executable", help="Exact native Codex path to verify and fingerprint for this capture")
    p.add_argument("--approval-marker",
                   help="Fail-closed mode: save only frames containing this marker and an approval cue")
    p.add_argument("--count", type=int, default=1)
    p.add_argument("--interval", type=float, default=2)
    ns = p.parse_args()
    if not 1 <= ns.count <= 100 or not .25 <= ns.interval <= 30:
        p.error("count must be 1–100 and interval .25–30 seconds")
    if ns.approval_marker is not None:
        try:
            validate_approval_marker(ns.approval_marker)
        except ValueError as exc:
            p.error(str(exc))
    asyncio.run(capture(ns))
