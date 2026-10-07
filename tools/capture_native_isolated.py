"""Capture only a newly created native Codex test window, then close it.

Default mode submits no input. Explicit draft/mock/manual options act only in
the owned test window. Mock requests go to a loopback fixture HTTP server;
approval probes are cancelled without approving the requested command.
"""
import argparse
import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import iterm2

from cliretry.adapters.iterm2_adapter import Iterm2Adapter
from cliretry.config import Config
from cliretry.models import RetryError, as_json


async def run(args):
    root = Path(args.output_dir).absolute()
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    executable = str(Path(args.executable).resolve(strict=True))
    adapter = Iterm2Adapter(Config())
    window = None
    server = None
    monitor = None
    release_error = threading.Event()
    result = {"source": "captured_native_codex", "complete": False, "input_calls": 0}
    try:
        await adapter.connect()
        profile = iterm2.LocalWriteOnlyProfile()
        profile.set_name("CLIRetry isolated native Codex capture")
        profile.set_use_custom_command(iterm2.Profile.USE_CUSTOM_COMMAND_ENABLED)
        tool_probe = args.mock_tool_output
        command = [executable, "--no-daemon", "--no-alt-screen", "--sandbox", "read-only",
                   "--ask-for-approval", "never",
                   "-c", "check_for_update_on_startup=false",
                   "-C", str(root if tool_probe else Path(args.working_dir).expanduser().absolute())]
        if (args.mock_error or args.mock_disconnect or args.mock_stream_drop or args.mock_stream_failed
                or args.mock_success or tool_probe):
            result["provider_source"] = (
                ("local_simulated_503" if args.mock_status == 503
                else f"local_simulated_http_{args.mock_status}")
                if args.mock_error else
                "local_loopback_shell_tool_fixture" if tool_probe else
                "local_simulated_success_sse" if args.mock_success else
                "local_simulated_response_failed" if args.mock_stream_failed else
                "local_simulated_stream_drop" if args.mock_stream_drop
                else "local_simulated_tcp_disconnect")
            if args.mock_error:
                result["provider_status"] = args.mock_status
                result["provider_message"] = args.mock_message
            result["provider_requests"] = 0

            class Handler(BaseHTTPRequestHandler):
                def send_sse(self, events):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    for sequence, (event_name, payload) in enumerate(events):
                        payload["sequence_number"] = sequence
                        chunk = f"event: {event_name}\ndata: {json.dumps(payload)}\n\n"
                        self.wfile.write(chunk.encode())
                        self.wfile.flush()

                def send_function_call(self, tool_name, arguments):
                    now = int(time.time())
                    response_id = f"resp_cliretry_tool_{result['provider_requests']}"
                    item_id = f"fc_cliretry_tool_{result['provider_requests']}"
                    call_id = f"call_cliretry_tool_{result['provider_requests']}"
                    encoded_arguments = json.dumps(arguments, ensure_ascii=False)
                    item = {"id": item_id, "type": "function_call", "status": "completed",
                            "call_id": call_id, "name": tool_name,
                            "arguments": encoded_arguments}
                    response = {"id": response_id, "object": "response", "created_at": now,
                                "status": "completed", "completed_at": now, "error": None,
                                "incomplete_details": None, "input": [], "instructions": None,
                                "max_output_tokens": None, "model": "cliretry-fixture",
                                "output": [item], "previous_response_id": None,
                                "reasoning_effort": None, "store": False, "temperature": 1,
                                "text": {"format": {"type": "text"}}, "tool_choice": "auto",
                                "tools": [], "top_p": 1, "truncation": "disabled",
                                "usage": {"input_tokens": 8, "output_tokens": 10,
                                          "output_tokens_details": {"reasoning_tokens": 0},
                                          "total_tokens": 18}, "user": None, "metadata": {}}
                    events = [
                        ("response.created", {"type": "response.created", "response": {
                            "id": response_id, "object": "response", "created_at": now,
                            "status": "in_progress", "model": "cliretry-fixture", "output": []}}),
                        ("response.in_progress", {"type": "response.in_progress", "response": {
                            "id": response_id, "object": "response", "created_at": now,
                            "status": "in_progress", "model": "cliretry-fixture", "output": []}}),
                        ("response.output_item.added", {"type": "response.output_item.added",
                            "output_index": 0, "item": {**item, "status": "in_progress",
                                                             "arguments": ""}}),
                        ("response.function_call_arguments.delta", {
                            "type": "response.function_call_arguments.delta", "item_id": item_id,
                            "output_index": 0, "delta": encoded_arguments}),
                        ("response.function_call_arguments.done", {
                            "type": "response.function_call_arguments.done", "item_id": item_id,
                            "output_index": 0, "arguments": encoded_arguments}),
                        ("response.output_item.done", {"type": "response.output_item.done",
                            "output_index": 0, "item": item}),
                        ("response.completed", {"type": "response.completed", "response": response}),
                    ]
                    self.send_sse(events)

                def do_POST(self):
                    raw_request = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                    result["provider_requests"] += 1
                    request = {}
                    if tool_probe:
                        try:
                            request = json.loads(raw_request)
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            result["tool_request_parse_error"] = True
                        definitions = request.get("tools", [])
                        if not isinstance(definitions, list):
                            definitions = []
                        names = [item.get("name", "") for item in definitions
                                 if isinstance(item, dict) and item.get("name")]
                        result["tool_names"] = sorted(set(result.get("tool_names", [])) | set(names))
                        if "shell_tool_schema" not in result:
                            shell_definition = next((item for item in definitions
                                                     if isinstance(item, dict)
                                                     and item.get("name") in {"exec_command", "shell_command"}), None)
                            if shell_definition:
                                result["shell_tool_schema"] = {
                                    "name": shell_definition.get("name"),
                                    "parameters": shell_definition.get("parameters",
                                                                       shell_definition.get("input_schema")),
                                }
                        tool_output_present = (
                            "CLIRetry native tool-output negative fixture"
                            in raw_request.decode("utf-8", errors="replace"))
                        result["tool_output_returned"] = (
                            result.get("tool_output_returned", False) or tool_output_present)
                        if tool_output_present:
                            result["tool_output_requests"] = result.get("tool_output_requests", 0) + 1
                    if tool_probe and result["provider_requests"] == 1:
                        candidates = {"exec_command", "shell_command"}
                        tool_name = next((name for name in result["tool_names"] if name in candidates), "")
                        if not tool_name:
                            result["tool_call_unavailable"] = True
                            self.send_error(501, "No supported Codex shell tool in local request")
                            return
                        command_text = (
                            "printf '%s\\n' '■ unexpected status 503 Service Unavailable: "
                            "Selected model is at capacity. Please try a different model.'; "
                            "printf '%s\\n' 'CLIRetry native tool-output negative fixture'"
                        )
                        arguments = {"cmd": command_text, "workdir": str(root),
                                     "yield_time_ms": 1000, "max_output_tokens": 500}
                        result["tool_call"] = {"name": tool_name, "arguments": arguments}
                        self.send_function_call(tool_name, arguments)
                        return
                    if args.auto_recover or args.auto_complete:
                        if not release_error.wait(90):
                            return
                        time.sleep(1)  # Keep the native transition observable after release.
                    if args.mock_success or (args.mock_tool_output and result["provider_requests"] > 1):
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream")
                        self.send_header("Cache-Control", "no-cache")
                        self.end_headers()
                        now = int(time.time())
                        response_id = f"resp_cliretry_fixture_{result['provider_requests']}"
                        message_id = f"msg_cliretry_fixture_{result['provider_requests']}"
                        if args.mock_success_kind == "quoted-error":
                            response_text = (
                                "Quoted terminal output (not a new failure):\n\n"
                                "```text\n"
                                "■ unexpected status 503 Service Unavailable: "
                                "Selected model is at capacity. Please try a different model.\n"
                                "```"
                            )
                        else:
                            response_text = "OK — local fixture completed normally."
                        message = {"id": message_id, "type": "message", "role": "assistant",
                                   "status": "completed", "content": [
                                       {"type": "output_text", "text": response_text, "annotations": []}]}
                        completed = {"id": response_id, "object": "response", "created_at": now,
                                     "status": "completed", "completed_at": now, "error": None,
                                     "incomplete_details": None, "input": [], "instructions": None,
                                     "max_output_tokens": None, "model": "cliretry-fixture",
                                     "output": [message], "previous_response_id": None,
                                     "reasoning_effort": None, "store": False, "temperature": 1,
                                     "text": {"format": {"type": "text"}}, "tool_choice": "auto",
                                     "tools": [], "top_p": 1, "truncation": "disabled",
                                     "usage": {"input_tokens": 8, "output_tokens": 10,
                                               "output_tokens_details": {"reasoning_tokens": 0},
                                               "total_tokens": 18}, "user": None, "metadata": {}}
                        events = [
                            ("response.created", {"type": "response.created", "response": {
                                "id": response_id, "object": "response", "created_at": now,
                                "status": "in_progress", "model": "cliretry-fixture", "output": []}}),
                            ("response.in_progress", {"type": "response.in_progress", "response": {
                                "id": response_id, "object": "response", "created_at": now,
                                "status": "in_progress", "model": "cliretry-fixture", "output": []}}),
                            ("response.output_item.added", {"type": "response.output_item.added",
                                "output_index": 0, "item": {"id": message_id, "status": "in_progress",
                                "type": "message", "role": "assistant", "content": []}}),
                            ("response.content_part.added", {"type": "response.content_part.added",
                                "item_id": message_id, "output_index": 0, "content_index": 0,
                                "part": {"type": "output_text", "text": "", "annotations": []}}),
                            ("response.output_text.delta", {"type": "response.output_text.delta",
                                "item_id": message_id, "output_index": 0, "content_index": 0,
                                "delta": response_text}),
                            ("response.output_text.done", {"type": "response.output_text.done",
                                "item_id": message_id, "output_index": 0, "content_index": 0,
                                "text": response_text}),
                            ("response.content_part.done", {"type": "response.content_part.done",
                                "item_id": message_id, "output_index": 0, "content_index": 0,
                                "part": {"type": "output_text", "text": response_text,
                                         "annotations": []}}),
                            ("response.output_item.done", {"type": "response.output_item.done",
                                "output_index": 0, "item": message}),
                            ("response.completed", {"type": "response.completed", "response": completed}),
                        ]
                        for sequence, (event_name, payload) in enumerate(events):
                            payload["sequence_number"] = sequence
                            chunk = f"event: {event_name}\ndata: {json.dumps(payload)}\n\n"
                            self.wfile.write(chunk.encode())
                            self.wfile.flush()
                        return
                    if args.mock_disconnect:
                        try:
                            self.connection.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                        self.connection.close()
                        return
                    if args.mock_stream_drop:
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream")
                        self.send_header("Cache-Control", "no-cache")
                        self.end_headers()
                        event = {"type": "response.created", "response": {
                            "id": "resp_cliretry_fixture", "object": "response",
                            "created_at": 0, "status": "in_progress", "model": "cliretry-fixture",
                            "output": []}}
                        self.wfile.write(("event: response.created\ndata: "
                                         + json.dumps(event) + "\n\n").encode())
                        self.wfile.flush()
                        time.sleep(.25)
                        try:
                            self.connection.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                        self.connection.close()
                        return
                    if args.mock_stream_failed:
                        now = int(time.time())
                        response_id = f"resp_cliretry_failed_{result['provider_requests']}"
                        failed = {"id": response_id, "object": "response", "created_at": now,
                                  "status": "failed", "completed_at": None,
                                  "error": {"code": "server_error",
                                            "message": "Internal streaming error, please retry."},
                                  "incomplete_details": None, "instructions": None,
                                  "max_output_tokens": None, "model": "cliretry-fixture",
                                  "output": [], "previous_response_id": None,
                                  "reasoning_effort": None, "store": False, "temperature": 1,
                                  "text": {"format": {"type": "text"}}, "tool_choice": "auto",
                                  "tools": [], "top_p": 1, "truncation": "disabled",
                                  "usage": None, "user": None, "metadata": {}}
                        self.send_sse([("response.failed", {"type": "response.failed",
                                                              "response": failed})])
                        return
                    body = json.dumps({"error": {"message": args.mock_message,
                                                  "type": "server_error", "code": "fixture_error"}}).encode()
                    self.send_response(args.mock_status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                def log_message(self, *args):
                    pass

            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            overrides = {"model_provider": "cliretry_fixture", "model": "cliretry-fixture",
                         "model_providers.cliretry_fixture.name": "CLIRetry local fixture",
                         "model_providers.cliretry_fixture.base_url": f"http://127.0.0.1:{server.server_port}/v1",
                         "model_providers.cliretry_fixture.wire_api": "responses",
                         "model_providers.cliretry_fixture.requires_openai_auth": False,
                         "model_providers.cliretry_fixture.request_max_retries": 0,
                         "model_providers.cliretry_fixture.stream_max_retries": 0}
            for key, value in overrides.items():
                command.extend(["-c", f"{key}={json.dumps(value)}"])
            command.append("CLIRetry isolated local fixture test. Do not use tools or read files.")
        profile.set_command(shlex.join(command))
        window = await asyncio.wait_for(iterm2.Window.async_create(
            adapter.connection, profile_customizations=profile), 15)
        if window is None:
            raise RuntimeError("Native test session ended immediately")
        session = window.tabs[0].sessions[0]
        result.update({"window_id": window.window_id, "session_id": session.session_id,
                       "command": command, "iterm_version": adapter.app_version(), "sdk_version": adapter.sdk_version})
        (root / "ownership.json").write_text(json.dumps(result))
        await adapter.rpc(adapter.app.async_refresh())
        result["keyboard_events"] = 0

        if args.auto_recover:
            from native_auto_validation import validate
            await validate(adapter, session.session_id, executable, root, release_error, result)
        if args.auto_complete:
            from native_auto_validation import validate_completion
            await validate_completion(adapter, session.session_id, executable, root,
                                      release_error, result,
                                      duration_s=args.auto_complete_duration)
        if args.auto_idle:
            from native_auto_validation import validate_idle
            await validate_idle(adapter, session.session_id, executable, root, result,
                                duration_s=args.auto_idle_duration)

        async def watch_keys():
            async for _ in adapter.activities(session.session_id):
                result["keyboard_events"] += 1

        monitor = asyncio.create_task(watch_keys())
        async with asyncio.timeout(5):
            while session.session_id not in adapter.monitor_ready:
                if monitor.done():
                    await monitor
                    raise RuntimeError("Keyboard monitor stopped")
                await asyncio.sleep(.05)
        observations = []

        async def record_frame(output):
            frame = await adapter.capture(session.session_id)
            output.write(json.dumps(as_json(frame), ensure_ascii=False) + "\n")
            output.flush()
            observation = adapter.profile.classify(frame)
            observations.append(as_json(observation))
            return frame, observation

        slash_menu_requested = False
        with (root / "frames.jsonl").open("x", encoding="utf-8") as output:
            if args.mock_stream_failed:
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    await asyncio.sleep(.5)
                    frame, observation = await record_frame(output)
                    screen_text = "\n".join(frame.lines)
                    result["stream_failed_text_visible"] = (
                        "internal streaming" in screen_text.lower()
                        and "please retry" in screen_text.lower())
                    result["stream_failed_ui_state"] = observation.ui_state.value
                    result["stream_failed_composer_state"] = observation.composer_state.value
                    result["stream_failed_category"] = observation.category
                    result["stream_failed_ready"] = observation.ready
                    if result["provider_requests"] and observation.ui_state.value != "BUSY":
                        break
                result["observations"] = observations
                if not result["provider_requests"]:
                    raise RuntimeError("Native Codex did not request the local response.failed fixture")
                if result["keyboard_events"]:
                    raise RuntimeError("Physical keyboard activity was detected during stream-failed probe")
            elif args.mock_tool_output:
                deadline = time.monotonic() + 90
                while time.monotonic() < deadline:
                    await asyncio.sleep(.5)
                    frame, observation = await record_frame(output)
                    screen_text = "\n".join(frame.lines)
                    output_seen = "CLIRetry native tool-output negative fixture" in screen_text
                    if observation.ready:
                        raise RuntimeError("Tool output was misclassified as a retryable terminal error")
                    if observation.normal_completion and output_seen:
                        result["tool_output_visible"] = True
                        result["tool_output_completion_state"] = observation.ui_state.value
                        result["tool_output_completion_category"] = observation.category
                        break
                result["observations"] = observations
                if not result.get("tool_output_visible"):
                    raise RuntimeError("Native fixture tool output and normal completion were not both observed")
                if not result.get("tool_output_returned") or result.get("provider_requests", 0) < 2:
                    raise RuntimeError("Loopback fixture did not receive the completed native tool output")
                if result["keyboard_events"]:
                    raise RuntimeError("Physical keyboard activity was detected during the tool-output probe")
            elif args.slash_menu:
                from cliretry.models import Composer, UiState

                stable_idle_samples = 0
                for _ in range(60):
                    await asyncio.sleep(1)
                    frame = await adapter.capture(session.session_id)
                    output.write(json.dumps(as_json(frame), ensure_ascii=False) + "\n")
                    output.flush()
                    observation = adapter.profile.classify(frame)
                    observations.append(as_json(observation))
                    if (observation.ui_state == UiState.IDLE
                            and observation.composer_state in (Composer.EMPTY, Composer.PLACEHOLDER)):
                        stable_idle_samples += 1
                    else:
                        stable_idle_samples = 0
                    result["stable_idle_samples"] = stable_idle_samples
                    if stable_idle_samples >= 2:
                        if result["keyboard_events"]:
                            raise RuntimeError("Physical keyboard activity detected; slash-menu input cancelled")
                        await adapter.rpc(session.async_send_text("/", suppress_broadcast=True))
                        result["input_calls"] += 1
                        slash_menu_requested = True
                        break
                if not slash_menu_requested:
                    raise RuntimeError("Native Codex did not reach a stable idle empty composer within 60 seconds")
                for _ in range(30):
                    await asyncio.sleep(.5)
                    frame = await adapter.capture(session.session_id)
                    output.write(json.dumps(as_json(frame), ensure_ascii=False) + "\n")
                    output.flush()
                    observation = adapter.profile.classify(frame)
                    observations.append(as_json(observation))
                    if observation.ui_state == UiState.MENU:
                        break
            else:
                for index in range(10):
                    await asyncio.sleep(1)
                    frame = await adapter.capture(session.session_id)
                    output.write(json.dumps(as_json(frame), ensure_ascii=False) + "\n")
                    output.flush()
                    observations.append(as_json(adapter.profile.classify(frame)))
                    if args.draft and index == 7:
                        if not any("Ask Codex to do anything" in line for line in frame.lines):
                            raise RuntimeError("Expected native test composer was not found; no draft sent")
                        await adapter.rpc(session.async_send_text("CLIRetry fixture draft 继续", suppress_broadcast=True))
                        result["input_calls"] += 1
                    if args.manual_continue and index == 7:
                        if (result["keyboard_events"] or not any("■ unexpected status 503" in line for line in frame.lines)
                                or not any(line.startswith("› Ask Codex to do anything") for line in frame.lines)):
                            raise RuntimeError("Owned native test UI changed; manual test input cancelled")
                        result["requests_before_continue"] = result["provider_requests"]
                        await adapter.rpc(session.async_send_text("继续", suppress_broadcast=True))
                        result["input_calls"] += 1
                        for delay in (.5, .25):
                            await asyncio.sleep(delay)
                            typed = await adapter.capture(session.session_id)
                            output.write(json.dumps(as_json(typed), ensure_ascii=False) + "\n")
                            if result["keyboard_events"] or not adapter.profile.verify_typed_text(typed, "继续"):
                                raise RuntimeError("Exact native composer text not verified; no Enter sent")
                        await adapter.rpc(session.async_send_text("\r", suppress_broadcast=True))
                        result["input_calls"] += 1
                        result["manual_continue_submitted"] = True
        result["observations"] = observations
        if args.slash_menu:
            result["slash_menu_seen"] = any(item["ui_state"] == "MENU" for item in observations)
            if not slash_menu_requested or not result["slash_menu_seen"]:
                raise RuntimeError("Native Codex slash menu was not observed")
        async with adapter.transaction():
            screen = await adapter.rpc(session.async_get_screen_contents())
            li = await adapter.rpc(session.async_get_line_info())
            result["raw_geometry"] = {
                "range_start_y": screen.windowed_coord_range.start.y,
                "range_end_y": screen.windowed_coord_range.end.y,
                "cursor_y": screen.cursor_coord.y, "returned_rows": screen.number_of_lines,
                "sdk_above_screen": screen.number_of_lines_above_screen,
                "history": li.scrollback_buffer_height, "overflow": li.overflow,
                "visible_top": li.first_visible_line_number, "grid_height": li.mutable_area_height,
            }
        values = frame.variables
        pid = int(values["jobPid"])
        result["process_group_diagnostic"] = subprocess.run(
            ["/bin/ps", "-p", str(pid), "-o", "pid=,pgid=,tpgid=,sess=,tty="],
            capture_output=True, text=True, timeout=2).stdout.strip()
        fd = None
        try:
            fd = os.open(values["tty"], os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
            result["tcgetpgrp"] = os.tcgetpgrp(fd)
        except OSError as exc:
            result["tcgetpgrp_error"] = str(exc)
        finally:
            if fd is not None:
                os.close(fd)
        try:
            identity = await adapter.identity(session.session_id, (executable,))
            result["identity"] = as_json(identity)
        except RetryError as exc:
            result["identity_error"] = exc.code
            result["identity_error_message"] = str(exc)
        result["complete"] = True
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        release_error.set()
        if monitor:
            monitor.cancel()
            with suppress(Exception, asyncio.CancelledError):
                await monitor
        if window:
            try:
                await asyncio.wait_for(window.async_close(force=True), 5)
                result["window_closed"] = True
            except Exception as exc:
                result["cleanup_error"] = type(exc).__name__
        await adapter.close()
        if server:
            server.shutdown()
            server.server_close()
        (root / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps({k: v for k, v in result.items() if k not in {"observations", "identity", "command"}}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--working-dir", default=str(Path.home()), help="An already trusted folder; no tasks or input are submitted")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--draft", action="store_true", help="Type a fixed draft in this new test session without submitting")
    group.add_argument("--slash-menu", action="store_true", help="Open the native slash command menu in the owned session; never press Enter")
    group.add_argument("--auto-idle", action="store_true", help="Enable AUTO only behind an owned-session test gate and verify idle sends nothing")
    group.add_argument("--mock-error", action="store_true", help="Exercise native error UI against a local fixture server")
    group.add_argument("--mock-disconnect", action="store_true", help="Drop the local fixture connection before an HTTP response")
    group.add_argument("--mock-stream-drop", action="store_true", help="Close a local SSE response after response.created")
    group.add_argument("--mock-stream-failed", action="store_true",
                       help="Return a local Responses response.failed event with the Codex stream-error wording")
    group.add_argument("--mock-success", action="store_true", help="Return a fixed successful native Responses stream from the loopback fixture")
    group.add_argument("--mock-tool-output", action="store_true",
                       help="Run a fixed read-only shell fixture through native Codex and verify error-looking output is not retryable")
    parser.add_argument("--mock-status", type=int, choices=[429, 502, 503, 504], default=503,
                        help="HTTP status returned by the owned loopback fixture server")
    parser.add_argument("--mock-message", default="Service temporarily unavailable",
                        help="Error message returned by the owned loopback fixture server")
    parser.add_argument("--mock-success-kind", choices=["normal", "quoted-error"], default="normal",
                        help="Fixed assistant text returned by --mock-success; never invokes tools")
    parser.add_argument("--manual-continue", action="store_true", help="Explicitly test Chinese plus CR in the new mock-error session; does not certify AUTO")
    parser.add_argument("--auto-recover", action="store_true", help="Owned local-503 native AUTO validation; does not change release certification")
    parser.add_argument("--auto-complete", action="store_true",
                        help="Hold a local success stream while AUTO establishes BUSY, then verify normal completion sends nothing")
    parser.add_argument("--auto-idle-duration", type=float, default=12,
                        help="Seconds to observe an empty native session in the isolated AUTO idle test")
    parser.add_argument("--auto-complete-duration", type=float, default=12,
                        help="Seconds to observe a completed native response in the isolated AUTO negative test")
    args = parser.parse_args()
    if args.manual_continue and not args.mock_error:
        parser.error("--manual-continue requires --mock-error")
    if args.auto_recover and (not args.mock_error or args.manual_continue):
        parser.error("--auto-recover requires --mock-error and excludes --manual-continue")
    if args.auto_recover and args.mock_status != 503:
        parser.error("--auto-recover validation is restricted to the verified 503 scenario")
    if args.auto_recover and (args.mock_disconnect or args.mock_stream_drop or args.mock_stream_failed):
        parser.error("--auto-recover cannot use a transport fault fixture")
    if args.auto_recover and args.auto_idle:
        parser.error("--auto-recover and --auto-idle are separate owned-session validations")
    if args.auto_complete and not args.mock_success:
        parser.error("--auto-complete requires --mock-success")
    if args.mock_tool_output and (args.auto_recover or args.auto_complete or args.manual_continue):
        parser.error("--mock-tool-output is a separate, zero-user-input local fixture")
    if not .5 <= args.auto_complete_duration <= 120:
        parser.error("--auto-complete-duration must be between 0.5 and 120 seconds")
    if not .5 <= args.auto_idle_duration <= 120:
        parser.error("--auto-idle-duration must be between 0.5 and 120 seconds")
    if len(args.mock_message) > 512 or any(ord(c) < 32 and c != "\t" for c in args.mock_message):
        parser.error("--mock-message must be at most 512 printable characters")
    asyncio.run(run(args))
