"""Generate explicitly synthetic fixtures; never connects to a terminal."""
import json
from dataclasses import replace
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cliretry.models import as_json, digest
from cliretry.profiles import CodexProfile
from tests.helpers import CAPACITY, frame


def samples():
    for kind, state in (("idle", "IDLE"), ("busy", "BUSY"), ("complete", "IDLE"),
                        ("capacity", "READY_ERROR"), ("quoted", "IDLE"), ("menu", "MENU")):
        yield kind, frame(kind), state, kind == "capacity"
    for name, text, ready in (
        ("429", "■ exceeded retry limit, last status: 429 Too Many Requests", True),
        ("auth_503", "■ HTTP 503 Service Unavailable: auth_not_found", False),
        ("tool_quote", "  └ " + CAPACITY, False),
    ):
        yield name, frame("capacity", error=text), "READY_ERROR" if name != "tool_quote" else "IDLE", ready
    yield "placeholder", frame("capacity", placeholder=True), "READY_ERROR", True
    yield "missing_style", frame("capacity", placeholder=True, style=False), "UNKNOWN", False
    yield "user_text", frame("capacity", user_text="继续"), "READY_ERROR", False
    yield "emoji_text", frame("capacity", user_text="😀"), "READY_ERROR", False
    f = frame("capacity")
    lines = list(f.lines)
    lines[16] = "```text"
    yield "open_code_fence", replace(f, lines=tuple(lines)), "UNKNOWN", False
    lines[19] = "```"
    yield "closed_code_fence", replace(f, lines=tuple(lines)), "IDLE", False
    f = frame("capacity", error="■ exceeded retry limit, last status: 429 Too Many Requests; retry-after: 120 seconds")
    lines, eols = list(f.lines), list(f.hard_eols)
    text = lines[18]
    lines[17], lines[18] = text[:60], text[60:]
    eols[17] = False
    yield "wrapped_429", replace(f, lines=tuple(lines), hard_eols=tuple(eols)), "READY_ERROR", True


def main():
    target = Path(__file__).resolve().parents[1] / "tests/fixtures/codex_local_v1"
    target.mkdir(parents=True, exist_ok=True)
    manifest = {"schema_version": 1, "source": "synthetic", "redacted": True,
                "iterm_version": None, "sdk_version": None, "codex_sha256": None,
                "profile_revision": CodexProfile.revision,
                "note": "Constructed test data; no live capture or compatibility evidence.",
                "samples": []}
    with (target / "frames.jsonl").open("w", encoding="utf-8") as output:
        for name, f, state, ready in samples():
            data = as_json(f)
            output.write(json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n")
            manifest["samples"].append({"name": name, "frame_hash": digest(data),
                                        "ui_state": state, "ready": ready})
    (target / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    timeline = [(0, "idle"), (2, "idle"), (4, "busy"), (8, "capacity"),
                (14, "capacity"), (24, "capacity"), (34, "capacity"), (38, "capacity"), (40, "capacity")]
    with (target / "events.jsonl").open("w") as output:
        for at, name in timeline:
            output.write(json.dumps({"at": at, "event": "frame", "fixture": name}) + "\n")
    (target / "expected.json").write_text(json.dumps({
        "scenario": "observe_fresh_failure", "mode": "OBSERVE", "jitter": 0,
        "actions": ["new_failure", "schedule", "would_retry"],
        "input_calls": [], "final_state": "OBSERVING",
    }, indent=2) + "\n")
    print(f"Generated {len(manifest['samples'])} synthetic fixtures")


if __name__ == "__main__":
    main()
