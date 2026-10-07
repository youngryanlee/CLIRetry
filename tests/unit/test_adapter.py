"""SDK boundaries with mocks; these do not certify live iTerm2 behavior."""
import asyncio
from types import SimpleNamespace

import pytest

from cliretry.adapters.iterm2_adapter import Iterm2Adapter
from cliretry.config import Config
from cliretry.models import RetryError
from tests.helpers import binding


@pytest.mark.parametrize("has_session", [True, False])
async def test_current_session_refreshed_and_missing_target_rejected(has_session):
    adapter = Iterm2Adapter(Config())
    adapter.connection = SimpleNamespace()
    adapter.dispatch = asyncio.get_running_loop().create_future()
    calls = []

    async def refresh():
        calls.append("refresh")

    adapter.app = SimpleNamespace(async_refresh=refresh, current_terminal_window=(
        SimpleNamespace(current_tab=SimpleNamespace(
            current_session=SimpleNamespace(session_id="pinned"))) if has_session else None))
    try:
        if has_session:
            assert await adapter.current_session_id() == "pinned"
        else:
            with pytest.raises(RetryError, match="No active"):
                await adapter.current_session_id()
        assert calls == ["refresh"]
    finally:
        adapter.dispatch.cancel()


def _iterm_session(session_id, title):
    async def get_variable(name):
        return {"name": title, "jobName": "codex"}.get(name, "")
    return SimpleNamespace(session_id=session_id, async_get_variable=get_variable)


@pytest.fixture
def connected_adapter():
    adapter = Iterm2Adapter(Config())
    adapter.connection = SimpleNamespace()
    adapter.dispatch = SimpleNamespace(done=lambda: False)
    yield adapter


async def test_resolve_tabs_uses_one_based_order_in_current_window(connected_adapter):
    calls = []

    async def refresh():
        calls.append("refresh")

    first = SimpleNamespace(tab_id="tab-a", sessions=[_iterm_session("s-a", "Codex A")])
    second = SimpleNamespace(tab_id="tab-b", sessions=[_iterm_session("s-b", "Codex B")])
    third = SimpleNamespace(tab_id="tab-c", sessions=[_iterm_session("s-c1", "Codex C1"),
                                                        _iterm_session("s-c2", "Codex C2")])
    active = SimpleNamespace(window_id="window-front", tabs=[first, second, third])
    inactive = SimpleNamespace(window_id="window-back", tabs=[
        SimpleNamespace(tab_id="other", sessions=[_iterm_session("other", "Other")])])
    connected_adapter.app = SimpleNamespace(async_refresh=refresh, current_terminal_window=active,
                                             windows=[inactive, active])

    result = await connected_adapter.resolve_tabs([3, 1])

    assert calls == ["refresh"]
    assert [(item.tab_number, item.window_id, item.tab_id) for item in result] == [
        (3, "window-front", "tab-c"), (1, "window-front", "tab-a")]
    assert [[session.session_id for session in item.sessions] for item in result] == [
        ["s-c1", "s-c2"], ["s-a"]]


@pytest.mark.parametrize("tab_numbers", [[], [0], [-1], [1, 1], [True]])
async def test_resolve_tabs_rejects_invalid_or_duplicate_numbers(connected_adapter, tab_numbers):
    with pytest.raises(RetryError) as caught:
        await connected_adapter.resolve_tabs(tab_numbers)
    assert caught.value.code == "INVALID_TAB_SELECTION"


async def test_resolve_tabs_rejects_missing_window_or_tab(connected_adapter):
    async def refresh():
        return None

    connected_adapter.app = SimpleNamespace(async_refresh=refresh, current_terminal_window=None)
    with pytest.raises(RetryError) as caught:
        await connected_adapter.resolve_tabs([1])
    assert caught.value.code == "NO_CURRENT_WINDOW"

    connected_adapter.app.current_terminal_window = SimpleNamespace(window_id="front", tabs=[])
    with pytest.raises(RetryError) as caught:
        await connected_adapter.resolve_tabs([1])
    assert caught.value.code == "TAB_NOT_FOUND"


async def test_candidate_executable_returns_only_native_codex_name(connected_adapter, tmp_path, monkeypatch):
    async def variables(_session_id):
        return {"jobPid": "123"}

    executable = tmp_path / "codex"
    executable.write_text("test executable")
    connected_adapter.variables = variables
    monkeypatch.setattr("cliretry.adapters.iterm2_adapter.psutil.Process",
                        lambda _pid: SimpleNamespace(exe=lambda: str(executable)))

    assert await connected_adapter.candidate_executable("session") == str(executable)

    shell = tmp_path / "zsh"
    shell.write_text("test shell")
    monkeypatch.setattr("cliretry.adapters.iterm2_adapter.psutil.Process",
                        lambda _pid: SimpleNamespace(exe=lambda: str(shell)))
    assert await connected_adapter.candidate_executable("session") is None


@pytest.mark.parametrize("initial_fresh,refresh_ok,statuses,expected_auth,expected_opens,success", [
    (False, True, [None], [False], 1, True),
    (False, True, [401, None], [False, True], 2, True),
    (True, True, [401], [False], 1, False),
    (False, False, [401], [False, True], 1, False),
    (False, True, [401, 401], [False, True], 2, False),
    (False, True, [403], [False], 1, False),
    (False, True, [406], [False], 1, False),
    (False, True, [503], [False], 1, False),
])
async def test_transport_refreshes_stale_cookie_once(
        tmp_path, initial_fresh, refresh_ok, statuses, expected_auth, expected_opens, success):
    from websockets.legacy.exceptions import InvalidStatusCode
    adapter = Iterm2Adapter(Config())
    auth_calls, opens = [], []
    websocket = object()

    def authenticate(force):
        auth_calls.append(force)
        return refresh_ok if force else initial_fresh

    async def open_socket():
        status = statuses[len(opens)]
        opens.append(status)
        if status:
            raise InvalidStatusCode(status, {})
        return websocket

    conn = SimpleNamespace(authenticate=authenticate, _get_connect_coro=open_socket,
                           _unix_domain_socket_path=lambda: tmp_path / "missing-socket")
    if success:
        assert await adapter._connect_transport(conn) is websocket
    else:
        with pytest.raises(InvalidStatusCode):
            await adapter._connect_transport(conn)
    assert auth_calls == expected_auth
    assert len(opens) == expected_opens
    assert adapter.transport == "tcp"


async def test_cancelled_auth_wait_keeps_one_worker_and_event_loop_responsive():
    import threading
    started, release = threading.Event(), threading.Event()
    auth_calls = []

    def authenticate(force):
        auth_calls.append(force)
        started.set()
        if not release.wait(2):
            raise TimeoutError("test worker was not released")
        return True

    adapter = Iterm2Adapter(Config())
    conn = SimpleNamespace(authenticate=authenticate)
    waiter = asyncio.create_task(adapter._authenticate(conn, False))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        worker = adapter.auth_task
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert not worker.cancelled()
        second_waiter = asyncio.create_task(adapter._authenticate(conn, False))
        await asyncio.sleep(0)
        assert adapter.auth_task is worker
        release.set()
        assert await second_waiter is True
        assert auth_calls == [False]
    finally:
        release.set()
        await asyncio.gather(waiter, adapter.auth_task, return_exceptions=True)


async def test_subscription_failure_never_allows_automatic_input(monkeypatch):
    adapter = Iterm2Adapter(Config())
    adapter._session = lambda sid: object()
    monkeypatch.setattr("cliretry.compatibility.verified", lambda *args: True)

    class BrokenMonitor:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            raise OSError("subscription denied")

    monkeypatch.setattr("iterm2.KeystrokeMonitor", BrokenMonitor)
    with pytest.raises(RetryError, match="OSError"):
        await anext(adapter.activities("test-session"))
    assert not adapter.automatic_ready(binding())


async def test_subscription_readiness_lasts_only_until_stream_closes(monkeypatch):
    adapter = Iterm2Adapter(Config())
    adapter._session = lambda sid: object()
    adapter.connection = SimpleNamespace()
    adapter.dispatch = asyncio.get_running_loop().create_future()
    monkeypatch.setattr("cliretry.compatibility.verified", lambda *args: True)

    class Monitor:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def async_get(self):
            return object()

    monkeypatch.setattr("iterm2.KeystrokeMonitor", Monitor)
    stream = adapter.activities("test-session")
    await anext(stream)
    assert adapter.automatic_ready(binding())
    await stream.aclose()
    assert not adapter.automatic_ready(binding())
    adapter.dispatch.cancel()


async def test_close_stops_dispatch_before_websocket_close():
    adapter = Iterm2Adapter(Config())
    order = []

    async def dispatch():
        try:
            await asyncio.Event().wait()
        finally:
            order.append("dispatch_stopped")

    async def close():
        order.append("websocket_closed")

    adapter.connection = SimpleNamespace(websocket=SimpleNamespace(close=close))
    adapter.dispatch = asyncio.create_task(dispatch())
    await asyncio.sleep(0)
    await adapter.close()
    assert order == ["dispatch_stopped", "websocket_closed"]


def test_running_bundle_version_fallback_is_unambiguous(tmp_path, monkeypatch):
    import plistlib
    bundle = tmp_path / "iTerm.app"
    (bundle / "Contents/MacOS").mkdir(parents=True)
    (bundle / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleShortVersionString": "3.4.23"}))
    processes = [SimpleNamespace(info={"name": "iTerm2", "exe": str(bundle / "Contents/MacOS/iTerm2")})]
    monkeypatch.setattr("psutil.process_iter", lambda attrs: iter(processes))
    assert Iterm2Adapter._running_app_version() == "3.4.23"
    processes.append(SimpleNamespace(info={"name": "iTerm2", "exe": "/different/iTerm.app/Contents/MacOS/iTerm2"}))
    assert Iterm2Adapter._running_app_version() == "unknown"


async def test_buffer_absolute_cursor_and_old_host_missing_above_screen_field():
    from tests.helpers import frame
    f = frame("capacity")
    adapter = Iterm2Adapter(Config())

    class Line:
        def __init__(self, index):
            self.string = f.lines[index]
            self.hard_eol = True
            self.cells = f.cells[index]

        def string_at(self, x):
            return self.cells[x].text

        def style_at(self, x):
            return None

    screen = SimpleNamespace(number_of_lines=24, number_of_lines_above_screen=0,
                             windowed_coord_range=SimpleNamespace(start=SimpleNamespace(x=0, y=100)),
                             cursor_coord=SimpleNamespace(x=2, y=120), line=Line)
    info = SimpleNamespace(scrollback_buffer_height=70, overflow=30,
                           first_visible_line_number=100, mutable_area_height=24)

    async def get_screen():
        return screen

    async def get_info():
        return info

    async def variables(sid):
        return {}

    adapter._session = lambda sid: SimpleNamespace(
        grid_size=SimpleNamespace(width=100), async_get_screen_contents=get_screen,
        async_get_line_info=get_info)
    adapter.variables = variables
    captured = await adapter._capture_unlocked("test-session")
    assert captured.absolute_top == captured.first_visible_line == 100
    assert captured.cursor_y == 20
    assert adapter.profile.classify(captured).ready


async def test_close_removes_only_own_app_callbacks_and_awaits_helpers():
    import iterm2
    adapter = Iterm2Adapter(Config())
    finished = []

    async def close():
        pass

    async def pending_helper():
        try:
            await asyncio.Event().wait()
        finally:
            finished.append(True)

    connection = SimpleNamespace(websocket=SimpleNamespace(close=close))
    task = asyncio.create_task(pending_helper())
    await asyncio.sleep(0)
    connection._Connection__tasks = [task]

    class CallbackOwner:
        def __init__(self, conn):
            self.connection = conn

        async def callback(self, *args):
            pass

    own, other = CallbackOwner(connection), CallbackOwner(object())
    key = ("isolated-test-handler", 999)
    handlers = iterm2.notifications._get_handlers()
    handlers[key] = [own.callback, other.callback]
    adapter.connection = connection
    try:
        await adapter.close()
        assert handlers[key] == [other.callback]
        assert task.done() and finished == [True]
    finally:
        handlers.pop(key, None)


async def test_sdk_helper_failure_invalidates_connection(monkeypatch):
    import iterm2
    from cliretry.adapters.iterm2_adapter import _SupervisedConnection

    async def broken(self, message):
        raise ValueError("bad notification")

    monkeypatch.setattr(iterm2.Connection, "_async_dispatch_to_helper", broken)
    adapter = Iterm2Adapter(Config())
    adapter.connection = _SupervisedConnection()
    adapter.dispatch = asyncio.get_running_loop().create_future()
    assert adapter.connected
    await adapter.connection._async_dispatch_to_helper(None)
    assert not adapter.connected
    assert adapter.connection.notification_error == "ValueError"
    await adapter.close()
