import json
from dataclasses import replace
from pathlib import Path

from cliretry.models import ScreenFrame, digest
from cliretry.profiles import CodexProfile
from tools.capture_fixture import is_approval_capture_candidate


def test_actual_native_ui_without_styles_never_passes_empty_composer_guard():
    root = Path(__file__).resolve().parents[1] / "fixtures/native_codex_0_156_iterm_3_4_23"
    manifest = json.loads((root / "manifest.json").read_text())
    expected = json.loads((root / "expected.json").read_text())
    raw_frames = [json.loads(line) for line in (root / "frames.jsonl").read_text().splitlines()]
    assert manifest["source"] == "captured"
    assert manifest["provider_source"] == "local_simulated_503"
    assert not manifest["automatic_supported"]
    profile = CodexProfile()
    for raw, metadata, wanted in zip(raw_frames, manifest["samples"], expected, strict=True):
        assert digest(raw) == metadata["frame_hash"]
        frame = ScreenFrame.from_dict(raw)
        observation = profile.classify(frame)
        assert observation.ui_state == wanted["ui_state"]
        assert observation.composer_state == wanted["composer_state"]
        assert not observation.ready
        if "blocker" in wanted:
            assert wanted["blocker"] in observation.blockers
        if "category" in wanted:
            assert observation.category == wanted["category"]
        assert profile.verify_typed_text(frame, "继续") == wanted["typed_verified"]


def test_native_error_layout_with_synthetic_style_metadata():
    """Style is injected test data here; this is NOT newer-host live evidence."""
    root = Path(__file__).resolve().parents[1] / "fixtures/native_codex_0_156_iterm_3_4_23"
    raw = json.loads((root / "frames.jsonl").read_text().splitlines()[0])
    frame = ScreenFrame.from_dict(raw)
    row = frame.cursor_y
    cells = list(frame.cells)
    cells[row] = tuple(replace(cell, faint=True) if index >= 2 else cell
                       for index, cell in enumerate(cells[row]))
    observation = CodexProfile().classify(replace(frame, cells=tuple(cells)))
    assert observation.ready
    assert observation.category == "UPSTREAM_TRANSIENT"
    assert observation.error_row < observation.composer_row


def test_native_codex_0160_slash_menu_is_captured_and_never_retryable():
    root = Path(__file__).resolve().parents[1] / "fixtures/native_codex_0_160_iterm_3_7_3"
    manifest = json.loads((root / "manifest.json").read_text())
    expected = json.loads((root / "expected.json").read_text())
    raw_frames = [json.loads(line) for line in (root / "frames.jsonl").read_text().splitlines()]
    profile = CodexProfile()

    assert manifest["source"] == "captured"
    assert manifest["codex_version"] == "0.160.0"
    assert manifest["iterm_version"] == "3.7.3"
    assert manifest["sdk_version"] == "2.25"
    assert manifest["profile_revision"] == profile.revision
    assert not manifest["automatic_supported"]
    assert manifest["input_action"] == {
        "text": "/", "input_calls": 1, "enter_sent": False,
        "physical_keyboard_events": 0,
    }

    for raw, metadata, wanted in zip(raw_frames, manifest["samples"], expected, strict=True):
        assert digest(raw) == metadata["frame_hash"], metadata["name"]
        observation = profile.classify(ScreenFrame.from_dict(raw))
        assert observation.ui_state.value == wanted["ui_state"], metadata["name"]
        assert observation.composer_state.value == wanted["composer_state"], metadata["name"]
        assert observation.ready == wanted["ready"], metadata["name"]
        if "blocker" in wanted:
            assert wanted["blocker"] in observation.blockers
        if "composer_text" in wanted:
            assert observation.composer_text == wanted["composer_text"]


def test_native_codex_0160_local_error_layouts_match_captured_fixtures():
    root = (Path(__file__).resolve().parents[1]
            / "fixtures/native_codex_0_160_iterm_3_7_3/error_cases")
    manifest = json.loads((root / "manifest.json").read_text())
    expected = json.loads((root / "expected.json").read_text())
    raw_frames = [json.loads(line) for line in (root / "frames.jsonl").read_text().splitlines()]
    profile = CodexProfile()

    assert manifest["source"] == "captured"
    assert manifest["provider_source"] == "multiple_local_loopback_fixtures"
    assert manifest["codex_version"] == "0.160.0"
    assert manifest["profile_revision"] == profile.revision
    assert not manifest["automatic_supported"]
    assert manifest["input_calls"] == 0
    assert {item["category"] for item in expected} == {
        "CAPACITY", "RATE_LIMIT_TRANSIENT", "UPSTREAM_TRANSIENT",
        "CONNECTION_TRANSIENT", "AUTH",
    }

    for raw, metadata, wanted in zip(raw_frames, manifest["samples"], expected, strict=True):
        assert digest(raw) == metadata["frame_hash"], metadata["name"]
        observation = profile.classify(ScreenFrame.from_dict(raw))
        assert observation.ui_state.value == wanted["ui_state"], metadata["name"]
        assert observation.composer_state.value == wanted["composer_state"], metadata["name"]
        assert observation.category == wanted["category"], metadata["name"]
        assert observation.ready == wanted["ready"], metadata["name"]
        assert list(observation.blockers) == wanted["blockers"], metadata["name"]


def test_native_codex_0160_completion_and_quoted_error_are_not_retryable():
    root = (Path(__file__).resolve().parents[1]
            / "fixtures/native_codex_0_160_iterm_3_7_3/response_cases")
    manifest = json.loads((root / "manifest.json").read_text())
    expected = json.loads((root / "expected.json").read_text())
    raw_frames = [json.loads(line) for line in (root / "frames.jsonl").read_text().splitlines()]
    profile = CodexProfile()

    assert manifest["source"] == "captured"
    assert manifest["provider_source"] == "local_loopback_success_sse"
    assert manifest["codex_version"] == "0.160.0"
    assert manifest["profile_revision"] == profile.revision
    assert not manifest["automatic_supported"]
    assert manifest["input_calls"] == manifest["auto_attempts"] == 0

    for raw, metadata, wanted in zip(raw_frames, manifest["samples"], expected, strict=True):
        assert digest(raw) == metadata["frame_hash"], metadata["name"]
        frame = ScreenFrame.from_dict(raw)
        observation = profile.classify(frame)
        assert observation.ui_state.value == wanted["ui_state"] == "IDLE"
        assert observation.composer_state.value == wanted["composer_state"] == "PLACEHOLDER"
        assert observation.normal_completion == wanted["normal_completion"] is True
        assert observation.category == wanted["category"] == ""
        assert observation.ready == wanted["ready"] is False
        assert list(observation.blockers) == wanted["blockers"] == []
        if metadata["name"] == "quoted_error_completion":
            quoted_error = next(line for line in frame.lines if "unexpected status 503" in line)
            assert quoted_error.startswith("  ■")


def test_native_codex_0160_tool_output_with_error_wording_is_not_retryable():
    root = (Path(__file__).resolve().parents[1]
            / "fixtures/native_codex_0_160_iterm_3_7_3/tool_output_cases")
    manifest = json.loads((root / "manifest.json").read_text())
    expected = json.loads((root / "expected.json").read_text())
    raw_frames = [json.loads(line) for line in (root / "frames.jsonl").read_text().splitlines()]
    profile = CodexProfile()

    assert manifest["source"] == "captured"
    assert manifest["provider_source"] == "local_loopback_shell_tool_fixture"
    assert manifest["codex_version"] == "0.160.0"
    assert manifest["iterm_version"] == "3.7.3"
    assert manifest["sdk_version"] == "2.25"
    assert manifest["profile_revision"] == profile.revision
    assert not manifest["automatic_supported"]
    assert manifest["auto_attempts"] == 0
    assert manifest["input_calls"] == manifest["keyboard_events"] == 0
    assert manifest["tool_output_requests"] == 1
    assert len(raw_frames) == len(expected) == 1

    raw, metadata, wanted = raw_frames[0], manifest["samples"][0], expected[0]
    assert digest(raw) == metadata["frame_hash"]
    frame = ScreenFrame.from_dict(raw)
    text = "\n".join(frame.lines)
    assert "■ unexpected status 503 Service Unavailable" in text
    assert "CLIRetry native tool-output negative fixture" in text
    assert "• Ran printf" in text
    observation = profile.classify(frame)
    assert observation.ui_state.value == wanted["ui_state"] == "IDLE"
    assert observation.composer_state.value == wanted["composer_state"] == "PLACEHOLDER"
    assert observation.normal_completion == wanted["normal_completion"] is True
    assert observation.category == wanted["category"] == ""
    assert observation.ready == wanted["ready"] is False
    assert list(observation.blockers) == wanted["blockers"] == []


def test_native_codex_0160_response_failed_event_renders_as_connection_transient():
    root = (Path(__file__).resolve().parents[1]
            / "fixtures/native_codex_0_160_iterm_3_7_3/stream_failed_cases")
    manifest = json.loads((root / "manifest.json").read_text())
    expected = json.loads((root / "expected.json").read_text())
    raw_frames = [json.loads(line) for line in (root / "frames.jsonl").read_text().splitlines()]
    profile = CodexProfile()

    assert manifest["source"] == "captured"
    assert manifest["provider_source"] == "local_simulated_response_failed"
    assert manifest["codex_version"] == "0.160.0"
    assert manifest["profile_revision"] == profile.revision
    assert not manifest["automatic_supported"]
    assert manifest["auto_attempts"] == manifest["input_calls"] == manifest["keyboard_events"] == 0
    assert manifest["provider_requests"] == 1
    assert len(raw_frames) == len(expected) == 1

    raw, wanted = raw_frames[0], expected[0]
    assert digest(raw) == manifest["samples"][0]["frame_hash"]
    frame = ScreenFrame.from_dict(raw)
    text = "\n".join(frame.lines)
    assert "stream disconnected before completion:" in text
    assert "Internal streaming error, please retry." in text
    observation = profile.classify(frame)
    assert observation.ui_state.value == wanted["ui_state"] == "READY_ERROR"
    assert observation.composer_state.value == wanted["composer_state"] == "PLACEHOLDER"
    assert observation.category == wanted["category"] == "CONNECTION_TRANSIENT"
    assert observation.ready == wanted["ready"] is True
    assert observation.normal_completion == wanted["normal_completion"] is False
    assert list(observation.blockers) == wanted["blockers"] == []


def test_native_codex_0160_approval_card_is_captured_but_never_retryable():
    root = (Path(__file__).resolve().parents[1]
            / "fixtures/native_codex_0_160_iterm_3_7_3/approval_cases")
    manifest = json.loads((root / "manifest.json").read_text())
    expected = json.loads((root / "expected.json").read_text())
    raw_frames = [json.loads(line) for line in (root / "frames.jsonl").read_text().splitlines()]
    marker = "CLIRetryApproval20261005R9W4X2"
    assert manifest["source"] == "captured"
    assert manifest["redacted"] is True
    assert manifest["codex_version"] == "0.160.0"
    assert manifest["iterm_version"] == "3.7.3"
    assert manifest["sdk_version"] == "2.25"
    assert manifest["profile_revision"] == CodexProfile().revision
    assert manifest["provider_source"] == "native_manual_approval_prompt"
    assert manifest["approval_state"] == "pending_at_capture"
    assert manifest["approval_reviewer"] == "user"
    assert manifest["approval_decisions"] == 0
    assert manifest["target_state"] == "existing"
    assert manifest["automatic_supported"] is False
    assert len(raw_frames) == len(expected) == 1

    raw, wanted = raw_frames[0], expected[0]
    assert digest(raw) == manifest["samples"][0]["frame_hash"]
    frame = ScreenFrame.from_dict(raw)
    text = "\n".join(frame.lines)
    assert marker in text
    assert "Would you like to run the following command?" in text
    assert "Do you want to allow me" in text
    assert "/Users/young/" not in text
    assert is_approval_capture_candidate(frame, marker)

    observation = CodexProfile().classify(frame)
    assert observation.ui_state.value == wanted["ui_state"] == "UNKNOWN"
    assert observation.composer_state.value == wanted["composer_state"] == "UNKNOWN"
    assert list(observation.blockers) == wanted["blockers"] == ["GEOMETRY_UNSUPPORTED"]
    assert observation.category == wanted["category"] == ""
    assert observation.ready == wanted["ready"] is False
    assert wanted["approval_marker_visible"] is True
    assert wanted["approval_prompt_visible"] is True


def test_native_codex_0160_fresh_target_approval_is_captured_but_never_retryable():
    root = (Path(__file__).resolve().parents[1]
            / "fixtures/native_codex_0_160_iterm_3_7_3/approval_cases/fresh_target_probe")
    manifest = json.loads((root / "manifest.json").read_text())
    expected = json.loads((root / "expected.json").read_text())
    raw_frames = [json.loads(line) for line in (root / "frames.jsonl").read_text().splitlines()]
    marker = "CLIRetryApproval20261005R9W4X2-1"

    assert manifest["source"] == "captured"
    assert manifest["redacted"] is True
    assert manifest["codex_version"] == "0.160.0"
    assert manifest["iterm_version"] == "3.7.3"
    assert manifest["sdk_version"] == "2.25"
    assert manifest["profile_revision"] == CodexProfile().revision
    assert manifest["provider_source"] == "native_manual_approval_prompt"
    assert manifest["approval_state"] == "pending_at_capture"
    assert manifest["approval_reviewer"] == "user"
    assert manifest["approval_decisions"] == 0
    assert manifest["target_state"] == "new"
    assert manifest["automatic_supported"] is False
    assert len(raw_frames) == len(expected) == 1

    raw, wanted = raw_frames[0], expected[0]
    assert digest(raw) == manifest["samples"][0]["frame_hash"]
    frame = ScreenFrame.from_dict(raw)
    text = "\n".join(frame.lines)
    assert marker in text
    assert "Would you like to run the following command?" in text
    assert "Do you want to allow me" in text
    assert "/Users/young/" not in text
    assert is_approval_capture_candidate(frame, marker)

    observation = CodexProfile().classify(frame)
    assert observation.ui_state.value == wanted["ui_state"] == "UNKNOWN"
    assert observation.composer_state.value == wanted["composer_state"] == "UNKNOWN"
    assert list(observation.blockers) == wanted["blockers"] == ["GEOMETRY_UNSUPPORTED"]
    assert observation.category == wanted["category"] == ""
    assert observation.ready == wanted["ready"] is False
    assert wanted["approval_marker_visible"] is True
    assert wanted["approval_prompt_visible"] is True
