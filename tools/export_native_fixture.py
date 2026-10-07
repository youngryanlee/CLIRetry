"""Export explicitly captured native Codex sessions as replay fixture data."""
import argparse
import json
from pathlib import Path

from cliretry.models import ScreenFrame, digest
from cliretry.profiles import CodexProfile


def main(args):
    if args.kind == "approval":
        return export_approval(args)
    if args.kind == "tool-output":
        return export_tool_output(args)
    if args.kind == "stream-failed":
        return export_stream_failed(args)

    source = Path(args.source)
    result = json.loads((source / "result.json").read_text())
    rows = [json.loads(line) for line in (source / "frames.jsonl").read_text().splitlines()]
    if (not result.get("complete") or not result.get("manual_continue_submitted")
            or result.get("provider_source") != "local_simulated_503" or len(rows) != 12):
        raise SystemExit("Expected completed, owned native manual-continue capture")
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    frames = [rows[index] for index in (7, 8, 11)]
    names = ["native_error_placeholder_without_style", "native_typed_continue", "native_refailed_placeholder"]
    with (output / "frames.jsonl").open("x", encoding="utf-8") as stream:
        for frame in frames:
            stream.write(json.dumps(frame, ensure_ascii=False, separators=(",", ":")) + "\n")
    manifest = {"schema_version": 1, "source": "captured", "redacted": False,
                "terminal_program": "native_codex", "provider_source": "local_simulated_503",
                "codex_version": "0.156.0", "iterm_version": result["iterm_version"],
                "sdk_version": result["sdk_version"], "codex_sha256": result["identity"]["exe_sha256"],
                "normalization": "iTerm2 absolute cursor converted using returned buffer range",
                "style_available": False, "automatic_supported": False,
                "samples": [{"name": name, "frame_hash": digest(frame)}
                            for name, frame in zip(names, frames, strict=True)]}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (output / "expected.json").write_text(json.dumps([
        {"ui_state": "UNKNOWN", "composer_state": "UNKNOWN", "blocker": "COMPOSER_UNVERIFIABLE", "typed_verified": False},
        {"ui_state": "READY_ERROR", "composer_state": "USER_TEXT", "category": "UPSTREAM_TRANSIENT", "typed_verified": True},
        {"ui_state": "UNKNOWN", "composer_state": "UNKNOWN", "blocker": "COMPOSER_UNVERIFIABLE", "typed_verified": False},
    ], indent=2) + "\n")
    with (output / "events.jsonl").open("x") as stream:
        for event in ({"event": "native_local_503", "fixture": names[0]},
                      {"event": "explicit_manual_test_type", "text": "继续", "fixture": names[1]},
                      {"event": "explicit_manual_test_enter", "text": "\r", "fixture": names[2]}):
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
    print(json.dumps({"exported_frames": len(frames), "source": "captured", "automatic_supported": False}))


def export_tool_output(args):
    source = Path(args.source)
    result = json.loads((source / "result.json").read_text())
    rows = [json.loads(line) for line in (source / "frames.jsonl").read_text().splitlines()]
    observations = result.get("observations", [])
    marker = "CLIRetry native tool-output negative fixture"
    if (not result.get("complete") or result.get("provider_source") != "local_loopback_shell_tool_fixture"
            or result.get("input_calls") != 0 or result.get("keyboard_events") != 0
            or not result.get("tool_output_returned") or not result.get("tool_output_visible")
            or result.get("tool_output_requests", 0) < 1 or not result.get("window_closed")
            or len(rows) != len(observations)):
        raise SystemExit("Expected completed, zero-input native tool-output capture")
    profile = CodexProfile()
    captured = []
    expected = []
    for raw, wanted in zip(rows, observations, strict=True):
        frame = ScreenFrame.from_dict(raw)
        if marker not in "\n".join(frame.lines):
            continue
        observation = profile.classify(frame)
        if not observation.normal_completion:
            continue
        if observation.ready:
            raise SystemExit("Refusing to export a retry-ready tool-output frame")
        captured = [raw]
        expected = [{"ui_state": observation.ui_state.value,
                     "composer_state": observation.composer_state.value,
                     "normal_completion": observation.normal_completion,
                     "category": observation.category, "ready": observation.ready,
                     "blockers": list(observation.blockers)}]
    if not captured:
        raise SystemExit("No completed native frame contains the fixed tool-output marker")

    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    with (output / "frames.jsonl").open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(captured[0], ensure_ascii=False, separators=(",", ":")) + "\n")
    base = json.loads((output.parent / "manifest.json").read_text())
    manifest = {"schema_version": 1, "source": "captured", "redacted": False,
                "terminal_program": "native_codex", "codex_version": base["codex_version"],
                "iterm_version": result["iterm_version"], "sdk_version": result["sdk_version"],
                "codex_sha256": result["identity"]["exe_sha256"],
                "profile_revision": profile.revision,
                "provider_source": "local_loopback_shell_tool_fixture",
                "automatic_supported": False, "auto_attempts": 0,
                "input_calls": 0, "keyboard_events": 0,
                "provider_requests": result["provider_requests"],
                "tool_output_requests": result["tool_output_requests"],
                "samples": [{"name": "tool_output_with_transient_error_wording",
                             "source_artifact": source.name,
                             "frame_hash": digest(captured[0])}]}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (output / "expected.json").write_text(json.dumps(expected, indent=2) + "\n")
    print(json.dumps({"exported_frames": 1, "source": "captured",
                      "automatic_supported": False, "ready": False}))


def export_stream_failed(args):
    source = Path(args.source)
    result = json.loads((source / "result.json").read_text())
    rows = [json.loads(line) for line in (source / "frames.jsonl").read_text().splitlines()]
    observations = result.get("observations", [])
    required_text = "stream disconnected before completion: Internal streaming error, please retry."
    if (not result.get("complete") or result.get("provider_source") != "local_simulated_response_failed"
            or not result.get("stream_failed_text_visible") or result.get("input_calls") != 0
            or result.get("keyboard_events") != 0 or not result.get("window_closed")
            or len(rows) != len(observations)):
        raise SystemExit("Expected completed, zero-input native response.failed capture")
    profile = CodexProfile()
    captured = []
    expected = []
    for raw in rows:
        frame = ScreenFrame.from_dict(raw)
        text = " ".join(" ".join(frame.lines).split())
        if required_text.lower() not in text.lower():
            continue
        observation = profile.classify(frame)
        captured = [raw]
        expected = [{"ui_state": observation.ui_state.value,
                     "composer_state": observation.composer_state.value,
                     "category": observation.category,
                     "ready": observation.ready,
                     "normal_completion": observation.normal_completion,
                     "blockers": list(observation.blockers)}]
    if not captured:
        raise SystemExit("No native frame contains the response.failed stream-error wording")

    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    with (output / "frames.jsonl").open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(captured[0], ensure_ascii=False, separators=(",", ":")) + "\n")
    base = json.loads((output.parent / "manifest.json").read_text())
    manifest = {"schema_version": 1, "source": "captured", "redacted": False,
                "terminal_program": "native_codex", "codex_version": base["codex_version"],
                "iterm_version": result["iterm_version"], "sdk_version": result["sdk_version"],
                "codex_sha256": result["identity"]["exe_sha256"],
                "profile_revision": profile.revision,
                "provider_source": "local_simulated_response_failed",
                "automatic_supported": False, "auto_attempts": 0,
                "input_calls": 0, "keyboard_events": 0,
                "provider_requests": result["provider_requests"],
                "samples": [{"name": "response_failed_rendered_as_connection_transient",
                             "source_artifact": source.name,
                             "frame_hash": digest(captured[0])}]}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (output / "expected.json").write_text(json.dumps(expected, indent=2) + "\n")
    print(json.dumps({"exported_frames": 1, "source": "captured",
                      "category": expected[0]["category"], "automatic_supported": False}))


def export_approval(args):
    source = Path(args.source)
    capture_manifest_path = source.with_name(source.name + ".manifest.json")
    if not source.is_file() or not capture_manifest_path.is_file():
        raise SystemExit("Expected a captured frame and its manifest")
    capture = json.loads(capture_manifest_path.read_text(encoding="utf-8"))
    rows = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines() if line]
    identity = capture.get("process_identity") or {}
    if (capture.get("source") != "captured" or not capture.get("complete")
            or capture.get("frame_count") != 1 or len(rows) != 1
            or not capture.get("approval_gate") or capture.get("identity_error")
            or not identity.get("exe_sha256")):
        raise SystemExit("Expected one identity-verified, marker-gated native approval frame")
    if (not args.approval_marker
            or digest(args.approval_marker) != capture["approval_gate"].get("marker_digest")):
        raise SystemExit("The supplied approval marker does not match the captured gate")
    if (not args.codex_version or not args.target_state or not args.redact_prefix
            or not args.approval_reviewer):
        raise SystemExit("Approval export requires --codex-version, --target-state, --redact-prefix, and --approval-reviewer")

    source_frame = rows[0]
    source_hash = digest(source_frame)
    frame = json.loads(json.dumps(source_frame))
    visible = "\n".join(frame.get("lines", []))
    if (args.approval_marker not in visible
            or "Would you like to run the following command?" not in visible
            or "Do you want to allow me" not in visible):
        raise SystemExit("Captured frame does not contain the expected native approval prompt")

    prefix = args.redact_prefix
    if prefix not in visible:
        raise SystemExit("The requested redaction prefix is not visible in the captured frame")
    replacement = "X" * len(prefix)
    frame["lines"] = [line.replace(prefix, replacement) for line in frame["lines"]]
    replaced_cells = 0
    for row in frame["cells"]:
        joined = "".join(cell["text"] for cell in row)
        redacted = joined.replace(prefix, replacement)
        replaced_cells += joined.count(prefix)
        if len(redacted) != len(joined):
            raise SystemExit("Path redaction must preserve terminal cell geometry")
        offset = 0
        for cell in row:
            size = len(cell["text"])
            cell["text"] = redacted[offset:offset + size]
            offset += size
    if replaced_cells == 0:
        raise SystemExit("Could not redact the target path from terminal cells")

    # Run-specific identifiers and timestamps are not needed to replay the UI.
    frame.update({"session_id": "fixture-session", "connection_epoch": 0, "seq": 0,
                  "monotonic_at": 0.0, "wall_at": 0.0, "activity_seq": 0,
                  "variables": {}})
    sanitized = ScreenFrame.from_dict(frame)
    profile = CodexProfile()
    observation = profile.classify(sanitized)
    if observation.ready or observation.category:
        raise SystemExit("Refusing to export an approval card that the profile considers retryable")

    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    with (output / "frames.jsonl").open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(frame, ensure_ascii=False, separators=(",", ":")) + "\n")
    manifest = {
        "schema_version": 1, "source": "captured", "redacted": True,
        "terminal_program": "native_codex", "codex_version": args.codex_version,
        "iterm_version": capture["iterm_version"], "sdk_version": capture["sdk_version"],
        "codex_sha256": identity["exe_sha256"], "profile_revision": profile.revision,
        "provider_source": "native_manual_approval_prompt",
        "automatic_supported": False, "approval_state": "pending_at_capture",
        "approval_reviewer": args.approval_reviewer, "approval_decisions": 0,
        "target_state": args.target_state, "source_artifact": source.name,
        "source_frame_hash": source_hash,
        "redaction_method": "equal-length X mask for absolute target path; transient IDs and timestamps removed",
        "samples": [{"name": "workspace_write_approval_pending", "frame_hash": digest(frame)}],
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    expected = [{"ui_state": observation.ui_state.value,
                 "composer_state": observation.composer_state.value,
                 "blockers": list(observation.blockers), "category": observation.category,
                 "ready": observation.ready,
                 "approval_marker_visible": True, "approval_prompt_visible": True}]
    (output / "expected.json").write_text(json.dumps(expected, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"exported_frames": 1, "source": "captured", "redacted": True,
                      "approval_state": "pending_at_capture", "ready": observation.ready,
                      "automatic_supported": False}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--kind", choices=["manual-continue", "tool-output", "stream-failed", "approval"],
                        default="manual-continue")
    parser.add_argument("--approval-marker", help="Required marker for exporting a captured approval frame")
    parser.add_argument("--redact-prefix", help="Absolute path prefix to mask without changing cell width")
    parser.add_argument("--codex-version", help="Native Codex version for an approval fixture")
    parser.add_argument("--target-state", choices=["new", "existing", "unknown"],
                        help="Whether the target existed when the approval request was captured")
    parser.add_argument("--approval-reviewer", choices=["user", "auto_review", "unknown"],
                        help="Reviewer policy verified from the native Codex process arguments")
    main(parser.parse_args())
