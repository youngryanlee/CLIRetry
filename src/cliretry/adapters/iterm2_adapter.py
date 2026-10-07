from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
import importlib.metadata
from pathlib import Path
import plistlib
import time

import iterm2
import psutil
from websockets.legacy.exceptions import InvalidStatusCode

from .. import compatibility
from ..config import Config
from ..models import Cell, RetryError, ScreenFrame, SessionRef, TabSelection
from ..profiles import CodexProfile
from .process_macos import MacProcessProbe


VARIABLES = ("jobPid", "jobName", "pid", "tty", "iterm2.pid", "selectionLength",
             "tmuxRole", "sshIntegrationLevel")


class _SupervisedConnection(iterm2.Connection):
    """SDK notification tasks otherwise lose exceptions during garbage collection."""
    notification_error = None

    async def _async_dispatch_to_helper(self, message):
        try:
            await super()._async_dispatch_to_helper(message)
        except Exception as exc:
            # connected becomes false, so the daemon revokes pending work and
            # reconnects. Do not leave an unobserved background task exception.
            self.notification_error = type(exc).__name__


class Iterm2Adapter:
    def __init__(self, config: Config):
        self.config = config
        self.profile = CodexProfile(config.profile)
        self.process_probe = MacProcessProbe()
        self.connection = None
        self.app = None
        self.dispatch = None
        self.transaction_lock = asyncio.Lock()
        self.process_slots = asyncio.Semaphore(2)
        self.epoch = 0
        self.seq = 0
        self.activity_counts: dict[str, int] = {}
        self.monitor_ready: set[str] = set()
        self.sdk_version = importlib.metadata.version("iterm2")
        self.auth_task = None
        self._app_version = "unknown"
        self.transport = "unknown"

    @staticmethod
    def _version_for_bundle(bundle_path):
        try:
            with (Path(bundle_path) / "Contents/Info.plist").open("rb") as stream:
                return plistlib.load(stream)["CFBundleShortVersionString"]
        except (OSError, TypeError, KeyError, plistlib.InvalidFileException):
            return "unknown"

    @classmethod
    def _running_app_version(cls):
        # Older iTerm2 doesn't expose appBundlePath. Accept only a single
        # running iTerm2 bundle; ambiguity must never certify another install.
        bundles = set()
        for process in psutil.process_iter(["name", "exe"]):
            try:
                executable = Path(process.info.get("exe") or "")
                if process.info.get("name") == "iTerm2" and executable.parent.name == "MacOS":
                    bundle = executable.parent.parent.parent
                    if bundle.suffix == ".app":
                        bundles.add(bundle)
            except (psutil.Error, OSError):
                continue
        return cls._version_for_bundle(next(iter(bundles))) if len(bundles) == 1 else "unknown"

    @property
    def connected(self):
        return (self.connection is not None and self.dispatch is not None and not self.dispatch.done()
                and not getattr(self.connection, "notification_error", None))

    async def _authenticate(self, conn, force):
        # A timed-out AppleScript call may still be running in its worker.
        # Reuse it instead of accumulating authentication threads.
        if self.auth_task is None or self.auth_task.done():
            self.auth_task = asyncio.create_task(asyncio.to_thread(conn.authenticate, force))
            self.auth_task.add_done_callback(
                lambda task: task.exception() if not task.cancelled() else None)
        return await asyncio.wait_for(asyncio.shield(self.auth_task), 10)

    async def _connect_transport(self, conn):
        fresh_cookie = await self._authenticate(conn, False)
        self.transport = "unix" if Path(conn._unix_domain_socket_path()).exists() else "tcp"
        try:
            return await asyncio.wait_for(conn._get_connect_coro(), self.config.daemon.api_timeout_s)
        except InvalidStatusCode as exc:
            # Match SDK 2.25: an inherited cookie may become stale after host
            # restart. Refresh once only, and never retry a fresh rejection.
            if exc.status_code != 401 or fresh_cookie:
                raise
            if not await self._authenticate(conn, True):
                raise
            return await asyncio.wait_for(conn._get_connect_coro(), self.config.daemon.api_timeout_s)

    async def connect(self):
        if self.connected:
            return
        await self.close()
        conn = _SupervisedConnection()
        # SDK 2.25's async_create performs synchronous AppleScript authentication
        # on the event loop. Isolate authentication so control/pause stays usable.
        # These two private transport methods mirror async_create and are pinned
        # to the inspected SDK version. No credentials are printed or persisted.
        if self.sdk_version != "2.25":
            raise RetryError("CAPABILITY_NOT_VALIDATED", "This build requires iterm2 SDK 2.25", exit_code=6)
        try:
            conn.websocket = await self._connect_transport(conn)
            self.connection = conn
            self.dispatch = asyncio.create_task(conn._async_dispatch_forever(conn, asyncio.get_running_loop()))
            self.dispatch.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
            # Rebuild the singleton; the transport above owns disconnection cleanup.
            iterm2.app.App.instance = None
            self.app = await self.rpc(iterm2.async_get_app(conn))
            bundle_path = await self.rpc(self.app.async_get_variable("appBundlePath"))
            self._app_version = self._version_for_bundle(bundle_path)
            if self._app_version == "unknown":
                self._app_version = await asyncio.to_thread(self._running_app_version)
            self.epoch += 1
        except Exception as exc:
            await self.close()
            if isinstance(exc, RetryError):
                raise
            raise RetryError("API_DISCONNECTED", f"iTerm2 API handshake failed over {self.transport} ({type(exc).__name__}); check API settings and authorization",
                             exit_code=5) from exc

    async def rpc(self, coro):
        try:
            return await asyncio.wait_for(coro, self.config.daemon.api_timeout_s)
        except RetryError:
            raise
        except Exception as exc:
            raise RetryError("API_DISCONNECTED", type(exc).__name__, exit_code=5) from exc

    def _session(self, session_id):
        if not self.connected or self.app is None:
            raise RetryError("API_DISCONNECTED", "iTerm2 API is not connected", exit_code=5)
        session = self.app.get_session_by_id(session_id, include_buried=False)
        if session is None:
            raise RetryError("TARGET_GONE", "Session no longer exists")
        return session

    @asynccontextmanager
    async def transaction(self):
        async with self.transaction_lock:
            if not self.connected:
                raise RetryError("API_DISCONNECTED")
            try:
                async with asyncio.timeout(self.config.daemon.api_timeout_s):
                    async with iterm2.Transaction(self.connection):
                        yield
            except RetryError as exc:
                if exc.code == "API_DISCONNECTED":
                    await self.close()
                raise
            except Exception as exc:
                # A failed transaction must not be reused; close to release host state.
                await self.close()
                raise RetryError("API_DISCONNECTED", type(exc).__name__) from exc

    async def list_sessions(self):
        if not self.connected:
            raise RetryError("API_DISCONNECTED")
        await self.rpc(self.app.async_refresh())
        result = []
        for window in self.app.windows:
            for tab in window.tabs:
                for session in tab.sessions:
                    name = await self.rpc(session.async_get_variable("name"))
                    job = await self.rpc(session.async_get_variable("jobName"))
                    result.append(SessionRef(session.session_id, window.window_id, tab.tab_id,
                                             str(name), str(job)))
        return result

    async def resolve_tabs(self, tab_numbers):
        """Resolve 1-based tab positions in iTerm's current window exactly once."""
        if not self.connected:
            raise RetryError("API_DISCONNECTED")
        if (not isinstance(tab_numbers, (list, tuple)) or not tab_numbers
                or any(type(number) is not int or number < 1 for number in tab_numbers)
                or len(set(tab_numbers)) != len(tab_numbers)):
            raise RetryError("INVALID_TAB_SELECTION")
        await self.rpc(self.app.async_refresh())
        window = self.app.current_terminal_window
        if window is None:
            raise RetryError("NO_CURRENT_WINDOW", "Select an iTerm2 window before choosing tabs")
        tabs = window.tabs
        selections = []
        for number in tab_numbers:
            if number > len(tabs):
                raise RetryError("TAB_NOT_FOUND", f"Tab {number} does not exist in the current window")
            tab = tabs[number - 1]
            sessions = []
            for session in tab.sessions:
                name = await self.rpc(session.async_get_variable("name"))
                job = await self.rpc(session.async_get_variable("jobName"))
                sessions.append(SessionRef(session.session_id, window.window_id, tab.tab_id,
                                           str(name), str(job)))
            selections.append(TabSelection(number, window.window_id, tab.tab_id, tuple(sessions)))
        return selections

    @staticmethod
    def _candidate_executable(variables):
        try:
            pid = int(variables.get("jobPid") or 0)
            path = Path(psutil.Process(pid).exe()).resolve(strict=True)
        except (psutil.Error, OSError, TypeError, ValueError) as exc:
            raise RetryError("FOREGROUND_UNVERIFIABLE", "Cannot identify the tab's foreground process") from exc
        if path.name != "codex" or not path.is_file():
            return None
        return str(path)

    async def candidate_executable(self, session_id):
        """Return the live job process path only if it is a native Codex binary."""
        variables = await self.variables(session_id)
        return await self._process_call(self._candidate_executable, variables)

    async def current_session_id(self):
        if not self.connected:
            raise RetryError("API_DISCONNECTED")
        await self.rpc(self.app.async_refresh())
        window = self.app.current_terminal_window
        tab = window.current_tab if window is not None else None
        session = tab.current_session if tab is not None else None
        if session is None:
            raise RetryError("TARGET_GONE", "No active iTerm2 session; use --session explicitly")
        return session.session_id

    async def variables(self, session_id):
        session = self._session(session_id)
        result = {}
        for name in VARIABLES:
            result[name] = await self.rpc(session.async_get_variable(name))
        return result

    async def _capture_unlocked(self, session_id):
        session = self._session(session_id)
        screen = await self.rpc(session.async_get_screen_contents())
        li = await self.rpc(session.async_get_line_info())
        variables = await self.variables(session_id)
        width = session.grid_size.width
        height = screen.number_of_lines
        absolute_top = screen.windowed_coord_range.start.y
        expected_top = li.scrollback_buffer_height + li.overflow
        if (screen.windowed_coord_range.start.x != 0 or absolute_top != expected_top
                or height != li.mutable_area_height):
            raise RetryError("SNAPSHOT_INVALID", "Mutable screen range does not match line information")
        # SDK cursor coordinates are absolute buffer coordinates. The optional
        # num_lines_above_screen protobuf field is absent (zero) on iTerm2 3.4.
        # Normalize using the returned range, which the older host does provide.
        cursor_y = screen.cursor_coord.y - absolute_top
        lines, hard, cells = [], [], []
        for y in range(height):
            line = screen.line(y)
            lines.append(line.string)
            hard.append(line.hard_eol)
            row = []
            for x in range(width):
                try:
                    text = line.string_at(x)
                except (IndexError, ValueError):
                    text = " "
                style = line.style_at(x) if hasattr(line, "style_at") else None
                row.append(Cell(text=text, faint=style.faint if style is not None else None))
            cells.append(tuple(row))
        self.seq += 1
        return ScreenFrame(session_id, self.epoch, self.seq, time.monotonic(), time.time(),
                           width, height, screen.cursor_coord.x, cursor_y,
                           tuple(lines), tuple(hard), tuple(cells),
                           absolute_top, li.first_visible_line_number,
                           int(variables.get("selectionLength") or 0), variables,
                           self.activity_counts.get(session_id, 0))

    async def capture(self, session_id):
        async with self.transaction():
            return await self._capture_unlocked(session_id)

    async def identity(self, session_id, allowed_paths, expected=None):
        variables = await self.variables(session_id)
        return await self._process_call(self.process_probe.read, variables, allowed_paths, expected)

    async def check_liveness(self, expected):
        await self._process_call(self.process_probe.check_liveness, expected)

    async def _process_call(self, method, *args):
        # Hold a slot until the underlying worker actually finishes, even if its
        # caller times out. This bounds uninterruptible OS calls.
        try:
            await asyncio.wait_for(self.process_slots.acquire(), self.config.daemon.process_check_timeout_s)
        except TimeoutError as exc:
            raise RetryError("FOREGROUND_UNVERIFIABLE", "Process workers are busy") from exc
        task = asyncio.create_task(asyncio.to_thread(method, *args))
        task.add_done_callback(lambda t: (self.process_slots.release(),
                                         t.exception() if not t.cancelled() else None))
        try:
            return await asyncio.wait_for(asyncio.shield(task), self.config.daemon.process_check_timeout_s)
        except TimeoutError as exc:
            raise RetryError("FOREGROUND_UNVERIFIABLE", "Process check timed out") from exc

    async def activities(self, session_id):
        self._session(session_id)
        monitor = iterm2.KeystrokeMonitor(self.connection, session=session_id)
        await self.rpc(monitor.__aenter__())
        self.monitor_ready.add(session_id)
        try:
            while self.connected:
                await monitor.async_get()
                self.activity_counts[session_id] = self.activity_counts.get(session_id, 0) + 1
                yield None
        finally:
            self.monitor_ready.discard(session_id)
            with suppress(Exception):
                await self.rpc(monitor.__aexit__(None, None, None))
        raise RetryError("API_DISCONNECTED")

    def app_version(self):
        return self._app_version

    async def probe(self, session_id):
        frame = await self.capture(session_id)
        obs = self.profile.classify(frame)
        style = any(c.faint is not None for row in frame.cells for c in row if c.text.strip())
        capabilities = {"read_screen": True, "read_cursor": True, "read_line_info": True,
                        "read_cell_style": style, "read_job_pid": bool(frame.variables.get("jobPid")),
                        "read_tty": bool(frame.variables.get("tty")),
                        "keyboard_monitor": True if session_id in self.monitor_ready else "unverified",
                        "transaction_read": True, "suppress_broadcast": "unverified"}
        candidate_path = None
        try:
            # Explicit diagnostic only, never auto-add to executable allowlist.
            import psutil
            candidate_path = psutil.Process(int(frame.variables.get("jobPid") or 0)).exe()
        except (ValueError, psutil.Error):
            pass
        return {"schema_version": 1, "platform": "darwin", "iterm_app_version": self.app_version(),
                "iterm_sdk_version": self.sdk_version, "session_found": True,
                "capabilities": capabilities, "automatic_ready": False,
                "candidate_executable": candidate_path, "ui_state": obs.ui_state,
                "composer_state": obs.composer_state,
                "blocking_reasons": list(obs.blockers) + ["CAPABILITY_NOT_VALIDATED"]}

    def automatic_ready(self, binding):
        return (binding.session.session_id in self.monitor_ready
                and compatibility.verified(self.app_version(), self.sdk_version,
                                           binding.identity.exe_sha256, self.profile.revision))

    def _latch_guard(self, binding, expected):
        if binding.latch.token != expected:
            raise RetryError("USER_ACTIVITY", "Input authorization was revoked")

    async def _guarded(self, binding, event, text, latch_token, *, submit):
        self._latch_guard(binding, latch_token)
        if not self.automatic_ready(binding):
            raise RetryError("CAPABILITY_NOT_VALIDATED")
        await self.identity(binding.session.session_id, (binding.identity.exe_realpath,), binding.identity)
        self._latch_guard(binding, latch_token)
        async with self.transaction():
            frame = await self._capture_unlocked(binding.session.session_id)
            self._latch_guard(binding, latch_token)
            if frame.connection_epoch != binding.connection_epoch:
                raise RetryError("SNAPSHOT_DISCONTINUITY")
            if int(frame.variables.get("jobPid") or 0) != binding.identity.codex_pid:
                raise RetryError("IDENTITY_CHANGED")
            if frame.variables.get("tty") != binding.identity.tty_path:
                raise RetryError("IDENTITY_CHANGED")
            if frame.selection_length or frame.first_visible_line != frame.absolute_top:
                raise RetryError("USER_ACTIVITY")
            obs = self.profile.classify(frame)
            if obs.error_signature != event.signature:
                raise RetryError("STALE_EVENT")
            if submit:
                if not self.profile.verify_typed_text(frame, text):
                    raise RetryError("TEXT_NOT_CONFIRMED")
            elif not obs.ready:
                raise RetryError("COMPOSER_NOT_EMPTY")
            self._latch_guard(binding, latch_token)
            await self.rpc(self._session(binding.session.session_id).async_send_text(
                "\r" if submit else text, suppress_broadcast=True))

    async def guarded_type(self, binding, event, text, latch_token):
        await self._guarded(binding, event, text, latch_token, submit=False)

    async def guarded_submit(self, binding, event, text, latch_token):
        await self._guarded(binding, event, text, latch_token, submit=True)

    async def close(self):
        connection, dispatch, app = self.connection, self.dispatch, self.app
        self.connection, self.dispatch, self.app = None, None, None
        self.monitor_ready.clear()
        if connection:
            # SDK 2.25 stores App callbacks globally, without a connection key.
            # Remove only callbacks bound to this connection, including those
            # registered by an App whose construction failed part-way through.
            handlers = iterm2.notifications._get_handlers()
            for key, callbacks in list(handlers.items()):
                remaining = [callback for callback in callbacks
                             if getattr(getattr(callback, "__self__", None), "connection", None) is not connection]
                if remaining:
                    handlers[key] = remaining
                else:
                    handlers.pop(key, None)
        if dispatch:
            dispatch.cancel()
            with suppress(Exception, asyncio.CancelledError):
                await dispatch
        if connection:
            callbacks = list(getattr(connection, "_Connection__tasks", ()))
            for callback in callbacks:
                callback.cancel()
            await asyncio.gather(*callbacks, return_exceptions=True)
        if connection and connection.websocket:
            with suppress(Exception):
                await connection.websocket.close()
        if iterm2.app.App.instance is app:
            iterm2.app.App.instance = None
