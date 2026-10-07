import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.helpers import frame, identity
from tools import capture_fixture


MARKER = "CLIRetry-Approval-Fixture-20261005-K7M4Q9"
_UNSET = object()


def approval_frame(text=MARKER):
    result = frame("idle")
    lines = list(result.lines)
    lines[result.cursor_y - 2] = "Would you like to run this command?"
    lines[result.cursor_y - 1] = f"printf '{text}'"
    return replace(result, lines=tuple(lines))


class FakeAdapter:
    def __init__(self, frames, *, identity_result=_UNSET):
        self.config = SimpleNamespace(profile=SimpleNamespace(executable_paths=()))
        self.profile = SimpleNamespace(revision="test-profile")
        self.sdk_version = "2.25"
        self.frames = list(frames)
        self.identity_result = identity() if identity_result is _UNSET else identity_result
        self.capture_calls = 0
        self.closed = False

    async def connect(self):
        return None

    async def identity(self, session_id, paths, expected=None):
        if self.identity_result is None:
            raise capture_fixture.RetryError("TARGET_GONE")
        return expected or self.identity_result

    async def capture(self, session_id):
        self.capture_calls += 1
        return replace(self.frames.pop(0), session_id=session_id)

    def app_version(self):
        return "3.7.3"

    async def close(self):
        self.closed = True


def args_for(path, *, count=1, marker=MARKER, executable="/native/codex"):
    return SimpleNamespace(config=None, output=str(path), session="native-test-session",
                           executable=executable, approval_marker=marker,
                           count=count, interval=0)


def install_fake_adapter(monkeypatch, adapter):
    monkeypatch.setattr(capture_fixture, "load_config", lambda _: adapter.config)
    monkeypatch.setattr(capture_fixture, "Iterm2Adapter", lambda _: adapter)


def test_approval_capture_requires_marker_and_prompt_on_same_frame():
    assert capture_fixture.is_approval_capture_candidate(approval_frame(), MARKER)
    assert not capture_fixture.is_approval_capture_candidate(approval_frame("unrelated text"), MARKER)

    ordinary_with_marker = frame("idle")
    lines = list(ordinary_with_marker.lines)
    lines[ordinary_with_marker.cursor_y - 1] = MARKER
    ordinary_with_marker = replace(ordinary_with_marker, lines=tuple(lines))
    assert not capture_fixture.is_approval_capture_candidate(ordinary_with_marker, MARKER)


@pytest.mark.parametrize("marker", ["short", " marker-with-leading-space",
                                    "marker-with-newline\nline", "marker-with ' shell"])
def test_approval_marker_must_be_distinctive_and_single_line(marker):
    with pytest.raises(ValueError):
        capture_fixture.validate_approval_marker(marker)


async def test_approval_capture_miss_leaves_no_files_and_never_saves_plain_frames(tmp_path, monkeypatch):
    marked_ordinary = frame("idle")
    lines = list(marked_ordinary.lines)
    lines[marked_ordinary.cursor_y - 1] = MARKER
    marked_ordinary = replace(marked_ordinary, lines=tuple(lines))
    prompt_without_marker = approval_frame("different-marker-20261005")
    adapter = FakeAdapter([marked_ordinary, prompt_without_marker])
    install_fake_adapter(monkeypatch, adapter)
    output = tmp_path / "approval.jsonl"

    with pytest.raises(SystemExit, match="No captured frame"):
        await capture_fixture.capture(args_for(output, count=2))

    assert not output.exists()
    assert not Path(f"{output}.manifest.json").exists()
    assert adapter.capture_calls == 2
    assert adapter.closed


async def test_approval_capture_writes_only_dual_gate_frames(tmp_path, monkeypatch):
    regular = frame("idle")
    approved = approval_frame()
    adapter = FakeAdapter([regular, approved])
    install_fake_adapter(monkeypatch, adapter)
    output = tmp_path / "approval.jsonl"

    await capture_fixture.capture(args_for(output, count=2))

    frames = [json.loads(line) for line in output.read_text().splitlines()]
    manifest = json.loads(Path(f"{output}.manifest.json").read_text())
    assert len(frames) == manifest["frame_count"] == 1
    assert frames[0]["lines"] == list(approved.lines)
    assert manifest["complete"]
    assert manifest["approval_gate"]["prompt_cue"] == "allowlisted_text_on_same_frame"
    assert adapter.closed


async def test_approval_capture_refuses_unverified_session_before_reading(tmp_path, monkeypatch):
    adapter = FakeAdapter([approval_frame()], identity_result=None)
    install_fake_adapter(monkeypatch, adapter)
    output = tmp_path / "approval.jsonl"

    with pytest.raises(SystemExit, match="requires verified native Codex identity"):
        await capture_fixture.capture(args_for(output))

    assert adapter.capture_calls == 0
    assert not output.exists()
    assert not Path(f"{output}.manifest.json").exists()
    assert adapter.closed
