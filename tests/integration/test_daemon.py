import asyncio
from dataclasses import replace
import json
import tempfile
from pathlib import Path

import pytest

from cliretry.config import Config, DaemonConfig
from cliretry.daemon import Daemon
from cliretry.models import Mode, RetryError, SessionRef, State, TabSelection
from cliretry.store import Store
from tests.helpers import FakeAdapter, FakeClock, binding, frame


class ServiceAdapter(FakeAdapter):
    def __init__(self, clock):
        super().__init__(clock)
        self.tab_selections = []
        self.tab_resolutions = 0

    async def list_sessions(self):
        return [binding().session]

    async def resolve_tabs(self, numbers):
        self.tab_resolutions += 1
        by_number = {item.tab_number: item for item in self.tab_selections}
        return [by_number[number] for number in numbers if number in by_number]

    async def candidate_executable(self, session_id):
        return "/test/codex" if session_id.startswith("codex-") else None

    async def connect(self):
        self.connected = True

    async def close(self):
        self.connected = False

    async def activities(self, session_id):
        await asyncio.Event().wait()
        yield None

    async def probe(self, session_id):
        return {"session_found": True, "automatic_ready": True}


@pytest.fixture
def service(tmp_path):
    clock = FakeClock()
    config = Config(daemon=DaemonConfig(state_dir=str(tmp_path)))
    adapter = ServiceAdapter(clock)
    daemon = Daemon(config, adapter=adapter, clock=clock)
    daemon.store = Store(tmp_path, config.recovery)
    b = binding()
    daemon.bindings[b.session.session_id] = b
    daemon.store.save_binding(b)
    yield daemon
    for task in daemon.monitors.values():
        task.cancel()
    daemon.store.close()


async def test_control_observe_enable_pause_resume(service):
    d = service
    sid = "test-session"
    status = await d.command("status", {})
    assert status["sessions"][0]["mode"] == Mode.OBSERVE
    await d.command("enable", {"session": sid})
    assert d.bindings[sid].mode == Mode.AUTO
    await d.command("pause", {"all": True})
    assert d.bindings[sid].state == State.PAUSED_USER
    with pytest.raises(RetryError, match="Inspect"):
        await d.command("enable", {"session": sid})
    await d.command("resume", {"session": sid})
    assert d.bindings[sid].mode == Mode.OBSERVE


async def test_current_error_token_is_one_shot(service):
    d = service
    sid = "test-session"
    d.bindings[sid].state = State.OBSERVING
    result = await d.command("inspect", {"session": sid})
    token = result["evidence_token"]
    await d.command("retry-current", {"session": sid, "evidence": token})
    assert d.bindings[sid].pending.one_shot
    assert d.bindings[sid].mode == Mode.OBSERVE
    with pytest.raises(RetryError):
        await d.command("retry-current", {"session": sid, "evidence": token})


async def test_token_expiry_and_mutated_screen(service):
    d = service
    sid = "test-session"
    d.bindings[sid].state = State.OBSERVING
    token = (await d.command("inspect", {"session": sid}))["evidence_token"]
    d.clock.at += 61
    with pytest.raises(RetryError):
        await d.command("retry-current", {"session": sid, "evidence": token})
    token = (await d.command("inspect", {"session": sid}))["evidence_token"]
    d.bindings[sid].latch.cancel(activity=True)
    with pytest.raises(RetryError):
        await d.command("retry-current", {"session": sid, "evidence": token})


async def test_unknown_compatibility_cannot_enable(service):
    service.adapter.automatic_ready = lambda _: False
    with pytest.raises(RetryError, match="live validation"):
        await service.command("enable", {"session": "test-session"})
    assert service.bindings["test-session"].mode == Mode.OBSERVE


async def test_watch_current_resolves_once_and_pins_id(service):
    d = service
    d.bindings.clear()
    calls = []

    async def current():
        calls.append("resolve")
        return "test-session"

    d.adapter.current_session_id = current
    result = await d.command("watch", {"current": True})
    assert result["session_id"] == "test-session"
    assert calls == ["resolve"]
    assert set(d.bindings) == {"test-session"}
    await d.command("status", {})
    assert calls == ["resolve"]


@pytest.mark.parametrize("args", [
    {"current": "true"}, {"current": True, "session": "test-session"},
    {"current": False},
])
async def test_watch_current_invalid_selection_rejected(service, args):
    with pytest.raises(RetryError) as caught:
        await service.command("watch", args)
    assert caught.value.code == "INVALID_REQUEST"


async def test_watch_tabs_pins_all_selected_sessions_and_reports_numbers(service):
    d = service
    d.bindings.clear()
    sessions = [SessionRef("codex-a", "window-front", "tab-a", "Codex A", "codex"),
                SessionRef("codex-b", "window-front", "tab-b", "Codex B", "codex")]
    d.adapter.tab_selections = [TabSelection(1, "window-front", "tab-a", (sessions[0],)),
                                TabSelection(3, "window-front", "tab-b", (sessions[1],))]

    result = await d.command("watch", {"tabs": [3, 1]})

    assert d.adapter.tab_resolutions == 2  # resolve, identity-check, then verify order did not change
    assert [item["tab_number"] for item in result["bindings"]] == [3, 1]
    assert [item["session_id"] for item in result["bindings"]] == ["codex-b", "codex-a"]
    assert all(item["mode"] == Mode.OBSERVE for item in result["bindings"])
    assert set(d.bindings) == {"codex-a", "codex-b"}
    assert {b.session.session_id for b in d.store.load_bindings()} >= {"codex-a", "codex-b"}


async def test_watch_tabs_rejects_multiple_codex_panes_without_partial_binding(service):
    d = service
    d.bindings.clear()
    split = (SessionRef("codex-a", "window-front", "tab-a", "Codex A", "codex"),
             SessionRef("codex-b", "window-front", "tab-a", "Codex B", "codex"))
    d.adapter.tab_selections = [TabSelection(1, "window-front", "tab-a", split)]

    with pytest.raises(RetryError) as caught:
        await d.command("watch", {"tabs": [1]})

    assert caught.value.code == "AMBIGUOUS_TAB"
    assert not d.bindings
    assert {b.session.session_id for b in d.store.load_bindings()} == {"test-session"}


async def test_watch_tabs_rejects_changed_mapping_and_does_not_bind(service):
    d = service
    d.bindings.clear()
    original = SessionRef("codex-a", "window-front", "tab-a", "Codex A", "codex")
    changed = SessionRef("codex-other", "window-front", "tab-a", "Codex other", "codex")
    d.adapter.tab_selections = [TabSelection(1, "window-front", "tab-a", (original,))]
    resolutions = 0

    async def resolve_tabs(_numbers):
        nonlocal resolutions
        resolutions += 1
        session = original if resolutions == 1 else changed
        return [TabSelection(1, "window-front", "tab-a", (session,))]

    d.adapter.resolve_tabs = resolve_tabs

    with pytest.raises(RetryError) as caught:
        await d.command("watch", {"tabs": [1]})

    assert caught.value.code == "TAB_SELECTION_CHANGED"
    assert not d.bindings
    assert {b.session.session_id for b in d.store.load_bindings()} == {"test-session"}


async def test_enable_cannot_override_concurrent_user_activity(service):
    b = service.bindings["test-session"]
    service.adapter.revocation = lambda: b.pause(State.PAUSED_USER, "USER_ACTIVITY")
    with pytest.raises(RetryError, match="Activity changed"):
        await service.command("enable", {"session": b.session.session_id})
    assert b.mode == Mode.OBSERVE and b.state == State.PAUSED_USER


@pytest.mark.parametrize("failure", ["exception", "eof"])
async def test_keyboard_monitor_failure_pauses(service, failure):
    async def broken(session):
        if failure == "exception":
            raise RuntimeError("subscription failed")
        if False:
            yield None
    service.adapter.activities = broken
    b = service.bindings["test-session"]
    service.start_monitor(b)
    await service.monitors[b.session.session_id]
    assert b.state == State.PAUSED_BLOCKED and b.reason == "MONITOR_FAILED"
    assert service.adapter.calls == []


async def test_poll_checks_target_even_on_unknown_shell_screen(service):
    async def gone(expected):
        raise RetryError("TARGET_GONE")
    service.adapter.check_liveness = gone
    with pytest.raises(RetryError, match="TARGET_GONE"):
        await service.poll_binding(service.bindings["test-session"])
    assert service.adapter.calls == []


async def test_invalid_control_request_has_no_effect(service):
    original = service.bindings["test-session"].latch.token
    with pytest.raises(RetryError):
        await service.command("pause", {"session": "test-session", "execute": "rm -rf anything"})
    assert service.bindings["test-session"].latch.token == original


async def test_emergency_pause_and_status_when_store_failed(service):
    service.store.failed = True
    await service.command("pause", {"all": True})
    result = await service.command("status", {})
    assert result["sessions"][0]["state"] == State.PAUSED_USER
    assert result["store_failed"]
    assert (await service.command("shutdown", {}))["shutting_down"]


async def test_real_unix_control_socket(service):
    d = service
    # pytest's nested macOS temporary path may exceed sockaddr_un's 104 bytes.
    socket_dir = tempfile.TemporaryDirectory(prefix="clr-socket-")
    d.config = replace(d.config, daemon=replace(d.config.daemon,
                       socket_path=str(Path(socket_dir.name) / "control.sock")))
    await d._start_socket()
    try:
        async def request(command, args, rid="a"):
            reader, writer = await asyncio.open_unix_connection(d.config.socket_path)
            writer.write(json.dumps({"v": 1, "request_id": rid, "command": command, "args": args}).encode() + b"\n")
            await writer.drain()
            response = json.loads(await reader.readline())
            writer.close()
            await writer.wait_closed()
            return response
        first = await request("pause", {"all": True})
        assert first["ok"]
        assert await request("pause", {"all": True}) == first
        assert not (await request("unwatch", {"all": True}))["ok"]  # conflicting request ID
        assert (await request("status", {}, "status"))["ok"]
        d.store.failed = True
        assert (await request("shutdown", {}, "stop"))["ok"]
    finally:
        d.server.close()
        await d.server.wait_closed()
        socket_dir.cleanup()


async def test_8_hour_virtual_monitoring(service):
    """Four observed sessions, scheduled errors/normal completion; no actual sleeps."""
    from cliretry.engine import frame_step
    d = service
    bindings = [replace(binding(), binding_id=f"b{i}", session=replace(binding().session, session_id=f"s{i}"))
                for i in range(4)]
    templates = {kind: frame(kind) for kind in ("busy", "capacity", "complete")}
    observations = {kind: d.adapter.profile.classify(f) for kind, f in templates.items()}
    reports = 0
    for second in range(0, 8 * 3600, 2):
        minute = second % 600
        kind = "busy" if 10 <= minute < 100 else "capacity" if 100 <= minute < 300 else "complete"
        for b in bindings:
            f = replace(templates[kind], session_id=b.session.session_id,
                        monotonic_at=second, wall_at=1000 + second, seq=second)
            obs = observations[kind]
            actions = frame_step(b, f, obs, d.config.recovery)
            for action in actions:
                assert action.kind != "send_due"
                if action.kind == "schedule":
                    b.pending.deadline = second + 30
                if action.kind == "would_retry":
                    reports += 1
    assert reports == 48 * 4
    assert d.adapter.calls == []


async def test_observe_all_fixture_cases_have_zero_input(service):
    from cliretry.models import ScreenFrame
    path = Path(__file__).resolve().parents[1] / "fixtures/codex_local_v1/frames.jsonl"
    for raw in path.read_text().splitlines():
        target = ScreenFrame.from_dict(json.loads(raw))
        b = binding()
        b.connection_epoch = target.connection_epoch

        async def capture(session_id):
            return replace(current, monotonic_at=service.clock.monotonic(),
                           wall_at=service.clock.wall(), seq=int(service.clock.at * 10))

        service.adapter.capture = capture
        for current in (frame(), frame(), frame("busy"), target, target, target, target):
            service.clock.at += 10
            await service.poll_binding(b)
            assert b.mode == Mode.OBSERVE
        assert not service.sends
    assert service.adapter.calls == []
