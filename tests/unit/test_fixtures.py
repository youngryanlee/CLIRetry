import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from cliretry.models import ScreenFrame, digest
from cliretry.profiles import CodexProfile
from tools.capture_fixture import private_create
from tests.helpers import binding


def test_fixture_manifest_and_classifier_agree():
    root = Path(__file__).resolve().parents[1] / "fixtures/codex_local_v1"
    manifest = json.loads((root / "manifest.json").read_text())
    frames = (root / "frames.jsonl").read_text().splitlines()
    assert manifest["source"] == "synthetic" and manifest["iterm_version"] is None
    assert len(frames) == len(manifest["samples"])
    profile = CodexProfile()
    for raw, expected in zip(frames, manifest["samples"], strict=True):
        data = json.loads(raw)
        assert digest(data) == expected["frame_hash"], expected["name"]
        observed = profile.classify(ScreenFrame.from_dict(data))
        assert observed.ui_state == expected["ui_state"], expected["name"]
        assert observed.ready == expected["ready"], expected["name"]


def test_capture_is_private_from_first_byte_and_never_overwrites(tmp_path):
    target = tmp_path / "capture.jsonl"
    previous = os.umask(0)
    try:
        with private_create(target) as stream:
            assert target.stat().st_mode & 0o777 == 0o600
            stream.write("original")
        with pytest.raises(FileExistsError):
            private_create(target)
    finally:
        os.umask(previous)
    assert target.read_text() == "original"


def test_fixture_timeline_matches_expected_actions():
    from cliretry.config import RecoveryConfig
    from cliretry.engine import frame_step
    from cliretry.scheduler import schedule
    root = Path(__file__).resolve().parents[1] / "fixtures/codex_local_v1"
    manifest = json.loads((root / "manifest.json").read_text())
    expected = json.loads((root / "expected.json").read_text())
    frames = {entry["name"]: ScreenFrame.from_dict(json.loads(raw))
              for entry, raw in zip(manifest["samples"], (root / "frames.jsonl").read_text().splitlines(), strict=True)}
    b, profile, config = binding(), CodexProfile(), RecoveryConfig()
    actions = []
    assert b.mode == expected["mode"]
    for raw in (root / "events.jsonl").read_text().splitlines():
        event = json.loads(raw)
        f = replace(frames[event["fixture"]], monotonic_at=event["at"], wall_at=1000 + event["at"])
        for action in frame_step(b, f, profile.classify(f), config):
            actions.append(action.kind)
            if action.kind == "schedule":
                b.pending.deadline = schedule(config, b.pending, 1, now_mono=f.monotonic_at,
                                               now_wall=f.wall_at, global_next=0, random_value=expected["jitter"])
    assert actions == expected["actions"]
    assert "send_due" not in actions
    assert b.state == expected["final_state"]
