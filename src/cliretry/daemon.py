from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import stat
import uuid

from .adapters.iterm2_adapter import Iterm2Adapter
from .config import Config
from .engine import frame_step, new_event
from .logging_setup import setup_logging
from .models import Binding, Mode, PAUSED, RetryError, State, as_json, digest
from .scheduler import Clock, schedule
from .sender import Sender
from .store import InstanceLock, Store, private_dir


COMMAND_ARGS = {
    "sessions": set(), "status": {"session"}, "doctor": {"session"},
    "inspect": {"session"}, "watch": {"session", "current", "tabs", "profile", "executable"},
    "enable": {"session"}, "pause": {"session", "all"}, "resume": {"session"},
    "unwatch": {"session", "all"}, "shutdown": set(), "logs": {"limit"},
    "reset-budget": {"session"}, "retry-current": {"session", "evidence"},
    "request-result": {"id"},
}
MUTATING = {"watch", "enable", "pause", "resume", "unwatch", "shutdown", "reset-budget", "retry-current"}


class Daemon:
    def __init__(self, config: Config, *, adapter=None, clock=None):
        self.config = config
        self.clock = clock or Clock()
        self.adapter = adapter or Iterm2Adapter(config)
        self.bindings: dict[str, Binding] = {}
        self.monitors: dict[str, asyncio.Task] = {}
        self.sends: dict[str, asyncio.Task] = {}
        self.tokens = {}
        self.stop = asyncio.Event()
        self.store = None
        self.lock = None
        self.server = None
        self.logger = None
        self.connection_error = "API_DISCONNECTED"
        self.command_lock = asyncio.Lock()
        self.last_log = {}
        self.sender = None
        self.stopping = False

    def log(self, action, reason="", binding=None):
        key = (action, reason, binding.binding_id if binding else None)
        now = self.clock.monotonic()
        if now - self.last_log.get(key, float("-inf")) < 60:
            return
        self.last_log[key] = now
        if self.logger:
            self.logger.info(action, extra={"metadata": {
                "reason_code": reason, "session_id": binding.session.session_id if binding else None,
                "state": binding.state if binding else None}})

    async def run(self):
        os.umask(0o077)
        self.lock = InstanceLock(self.config.state_dir)
        try:
            self.store = Store(self.config.state_dir, self.config.recovery)
            self.store.cleanup(self.clock.wall())
            self.logger = setup_logging(self.config)
            self.bindings = {b.session.session_id: b for b in self.store.load_bindings()}
            self.sender = Sender(self.adapter, self.store, self.config.recovery, self.clock)
            await self._start_socket()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                with suppress(NotImplementedError, RuntimeError):
                    loop.add_signal_handler(sig, self.request_stop)
            connect_task = asyncio.create_task(self.connection_loop())
            poll_task = asyncio.create_task(self.poll_loop())
            for task in (connect_task, poll_task):
                task.add_done_callback(self._supervise_global)
            self.log("daemon_started")
            await self.stop.wait()
            self.stopping = True
            for binding in self.bindings.values():
                binding.latch.cancel()
            for task in (connect_task, poll_task, *self.monitors.values()):
                task.cancel()
            await asyncio.gather(connect_task, poll_task, *self.monitors.values(), return_exceptions=True)
            for task in self.sends.values():
                task.cancel()
            await asyncio.gather(*self.sends.values(), return_exceptions=True)
        finally:
            if self.server:
                self.server.close()
                await self.server.wait_closed()
                with suppress(FileNotFoundError):
                    self.config.socket_path.unlink()
            await self.adapter.close()
            if self.store:
                with suppress(Exception):
                    for binding in self.bindings.values():
                        self.store.save_binding(binding)
                self.store.close()
            if self.lock:
                self.lock.close()

    def request_stop(self):
        for binding in self.bindings.values():
            binding.latch.cancel()
        self.stop.set()

    def _supervise_global(self, task):
        if self.stopping or task.cancelled():
            return
        exc = task.exception()
        if exc:
            self.log("task_failed", type(exc).__name__)
            for b in self.bindings.values():
                b.pause(State.PAUSED_BLOCKED, "STORE_UNAVAILABLE" if self.store.failed else "MONITOR_FAILED")
            self.connection_error = "MONITOR_FAILED"
            # Keep the control socket available for diagnostics and shutdown.

    async def _start_socket(self):
        path = self.config.socket_path
        private_dir(path.parent)
        if path.exists() or path.is_symlink():
            st = path.lstat()
            if not stat.S_ISSOCK(st.st_mode) or st.st_uid != os.getuid():
                raise RetryError("UNSAFE_SOCKET_PATH", str(path))
            try:
                _, writer = await asyncio.wait_for(asyncio.open_unix_connection(path), 1)
            except (ConnectionRefusedError, FileNotFoundError):
                path.unlink()
            else:
                writer.close()
                await writer.wait_closed()
                raise RetryError("DAEMON_ALREADY_RUNNING")
        self.server = await asyncio.start_unix_server(self.handle_client, path=path, limit=65536)
        os.chmod(path, 0o600)

    async def connection_loop(self):
        delays = (1, 2, 5, 10, 30)
        failures = 0
        while not self.stop.is_set():
            if self.adapter.connected:
                await asyncio.sleep(1)
                continue
            for b in self.bindings.values():
                b.latch.cancel()
                if not b.inflight_attempt and b.state not in PAUSED:
                    b.invalidate()
                    b.state = State.DISCONNECTED
            for task in self.monitors.values():
                task.cancel()
            await asyncio.gather(*self.monitors.values(), return_exceptions=True)
            self.monitors.clear()
            try:
                await self.adapter.connect()
                self.connection_error = ""
                failures = 0
                for b in self.bindings.values():
                    if b.state not in PAUSED and not b.inflight_attempt:
                        try:
                            # A running tool may own the foreground after reconnect.
                            # Check lifetime now; full TTY identity is rechecked on ready/send.
                            await self.adapter.check_liveness(b.identity)
                            b.baseline()
                            b.connection_epoch = self.adapter.epoch
                        except RetryError as exc:
                            b.pause(State.TARGET_GONE, exc.code)
                    self.start_monitor(b)
                self.log("api_connected")
            except RetryError as exc:
                self.connection_error = exc.code
                self.log("api_disconnected", exc.code)
                await asyncio.sleep(delays[min(failures, len(delays) - 1)])
                failures += 1

    def start_monitor(self, b):
        if b.session.session_id in self.monitors or b.state == State.TARGET_GONE:
            return

        async def monitor():
            try:
                async for _ in self.adapter.activities(b.session.session_id):
                    # Revocation happens before other event-loop tasks can submit.
                    b.latch.cancel(activity=True)
                    b.pause(State.PAUSED_USER, "USER_ACTIVITY")
                    self.store.save_binding(b)
                raise RetryError("MONITOR_FAILED", "Keyboard event stream ended")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self.adapter.connected or (isinstance(exc, RetryError) and exc.code == "API_DISCONNECTED"):
                    b.latch.cancel()
                    if not b.inflight_attempt and b.state not in PAUSED:
                        b.invalidate()
                        b.state = State.DISCONNECTED
                else:
                    b.pause(State.PAUSED_BLOCKED, "MONITOR_FAILED")
                if not self.store.failed:
                    self.store.save_binding(b)
                self.log("monitor_failed", type(exc).__name__, b)

        self.monitors[b.session.session_id] = asyncio.create_task(monitor())

    async def poll_loop(self):
        while not self.stop.is_set():
            if self.store.failed:
                for b in self.bindings.values():
                    b.pause(State.PAUSED_BLOCKED, "STORE_UNAVAILABLE")
                await asyncio.sleep(1)
                continue
            if self.adapter.connected:
                # Sort by deadline for fair global scheduling; one send task per Session.
                ordered = sorted(self.bindings.values(), key=lambda b: (
                    b.pending.deadline if b.pending and b.pending.deadline else float("inf"), b.binding_id))
                for b in ordered:
                    if b.inflight_attempt or b.session.session_id in self.sends or b.state in PAUSED:
                        continue
                    try:
                        await self.poll_binding(b)
                    except RetryError as exc:
                        if exc.code == "API_DISCONNECTED":
                            await self.adapter.close()
                            break
                        if exc.code in {"IDENTITY_CHANGED", "TARGET_GONE"}:
                            b.pause(State.TARGET_GONE, exc.code)
                        elif exc.code == "FOREGROUND_UNVERIFIABLE":
                            b.baseline()
                            b.reason = exc.code
                        else:
                            b.pause(State.PAUSED_BLOCKED, exc.code)
                        if not self.store.failed:
                            self.store.save_binding(b)
                        self.log("binding_blocked", exc.code, b)
                    except Exception as exc:
                        b.pause(State.PAUSED_BLOCKED, "MONITOR_FAILED")
                        self.store.save_binding(b)
                        self.log("binding_failed", type(exc).__name__, b)
            await asyncio.sleep(self.config.daemon.poll_interval_s)

    async def poll_binding(self, b):
        await self.adapter.check_liveness(b.identity)
        frame = await self.adapter.capture(b.session.session_id)
        o = self.adapter.profile.classify(frame)
        if o.ready:
            await self.adapter.identity(b.session.session_id, (b.identity.exe_realpath,), b.identity)
        if b.mode == Mode.AUTO:
            budget = self.store.budget(b.identity.key, self.clock.wall())
            if budget["reason"]:
                raise RetryError(budget["reason"])
        actions = frame_step(b, frame, o, self.config.recovery)
        b.connection_epoch = frame.connection_epoch
        for action in actions:
            if action.kind == "new_failure":
                self.store.ensure_chain(b.identity.key, self.clock.wall())
            elif action.kind == "schedule":
                budget = self.store.budget(b.identity.key, self.clock.wall())
                b.pending.deadline = schedule(self.config.recovery, b.pending,
                                              budget["chain_attempts"] + 1,
                                              now_mono=self.clock.monotonic(), now_wall=self.clock.wall(),
                                              global_next=budget["global_next_at"])
                future_wall = self.clock.wall() + max(0, b.pending.deadline - self.clock.monotonic())
                future_budget = self.store.budget(b.identity.key, self.clock.wall(), prospective_at=future_wall)
                if future_budget["reason"] and (b.mode == Mode.AUTO or b.pending.one_shot):
                    raise RetryError(future_budget["reason"])
                self.store.audit("retry_scheduled", b.pending.category, binding_id=b.binding_id,
                                 event_id=b.pending.event_id)
            elif action.kind == "send_due":
                budget = self.store.budget(b.identity.key, self.clock.wall())
                if budget["global_next_at"] > self.clock.wall():
                    b.pending.deadline = self.clock.monotonic() + budget["global_next_at"] - self.clock.wall()
                else:
                    self.start_send(b)
            elif action.kind == "close_chain":
                self.store.close_chain(b.identity.key)
            elif action.kind == "would_retry":
                self.store.audit("would_retry", action.reason, binding_id=b.binding_id)
            self.log(action.kind, action.reason, b)
        self.store.save_binding(b)

    def start_send(self, b):
        if b.session.session_id in self.sends:
            return
        task = asyncio.create_task(self.sender.execute(b))
        self.sends[b.session.session_id] = task

        def done(t):
            self.sends.pop(b.session.session_id, None)
            if not t.cancelled() and t.exception():
                b.pause(State.PAUSED_BLOCKED, "STORE_UNAVAILABLE" if self.store.failed else "MONITOR_FAILED")
                self.log("send_task_failed", type(t.exception()).__name__, b)
        task.add_done_callback(done)

    def binding(self, args):
        session_id = args.get("session")
        if session_id not in self.bindings:
            raise RetryError("NOT_WATCHED", "Use watch with the exact Session ID first")
        return self.bindings[session_id]

    async def command(self, command, args):
        self.validate_command(command, args)
        return await self._command(command, args)

    @staticmethod
    def validate_command(command, args):
        if command not in COMMAND_ARGS or not isinstance(args, dict) or args.keys() - COMMAND_ARGS[command]:
            raise RetryError("INVALID_REQUEST", "Unknown command or argument", exit_code=2)
        for name, value in args.items():
            if name in {"all", "current"}:
                valid = type(value) is bool
            elif name == "tabs":
                valid = (type(value) is list and 1 <= len(value) <= 32
                         and all(type(number) is int and 1 <= number <= 1000 for number in value)
                         and len(value) == len(set(value)))
            elif name == "limit":
                valid = type(value) is int and 1 <= value <= 200
            else:
                valid = isinstance(value, str) and 0 < len(value) <= 4096 and "\0" not in value
            if not valid:
                raise RetryError("INVALID_REQUEST", f"Invalid {name}", exit_code=2)
        if args.get("all") and args.get("session"):
            raise RetryError("INVALID_REQUEST", "Choose --all or --session", exit_code=2)
        if command == "watch":
            selections = sum((bool(args.get("session")), args.get("current", False),
                              "tabs" in args))
            if selections != 1:
                raise RetryError("INVALID_REQUEST", "Choose exactly one of --session, --current or --tabs",
                                 exit_code=2)

    async def _watch_tabs(self, args):
        if args.get("profile", "codex-local-v1") != "codex-local-v1":
            raise RetryError("PROFILE_MISMATCH")
        paths = self.config.profile.executable_paths
        if args.get("executable"):
            path = Path(args["executable"])
            if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
                raise RetryError("UNSUPPORTED_PROCESS", "An absolute executable path is required")
            paths = (str(path.resolve()),)

        tab_numbers = args["tabs"]
        if len(self.bindings) + len(tab_numbers) > self.config.daemon.max_sessions:
            raise RetryError("MAX_SESSIONS")
        selections = await self.adapter.resolve_tabs(tab_numbers)
        if [item.tab_number for item in selections] != tab_numbers:
            raise RetryError("TAB_NOT_FOUND", "One or more selected tabs are no longer available")
        plans = []
        for selection in selections:
            native_candidates = []
            for session in selection.sessions:
                executable = await self.adapter.candidate_executable(session.session_id)
                if executable is not None:
                    native_candidates.append((session, executable))
            if not native_candidates:
                raise RetryError("UNSUPPORTED_PROCESS", f"Tab {selection.tab_number} has no native Codex session")
            if len(native_candidates) != 1:
                raise RetryError("AMBIGUOUS_TAB", f"Tab {selection.tab_number} contains multiple Codex sessions")
            session, executable = native_candidates[0]
            allowed_paths = paths or (executable,)
            identity = await self.adapter.identity(session.session_id, allowed_paths)
            plans.append((selection, session, identity))

        # Ensure the ordinal still names the same tab after process checks. Once
        # committed, monitoring remains pinned to the resolved Session UUID.
        refreshed = await self.adapter.resolve_tabs(tab_numbers)
        before = {item.tab_number: (item.tab_id, frozenset(s.session_id for s in item.sessions))
                  for item in selections}
        after = {item.tab_number: (item.tab_id, frozenset(s.session_id for s in item.sessions))
                 for item in refreshed}
        if before != after:
            raise RetryError("TAB_SELECTION_CHANGED", "Tab order or contents changed; retry the selection")

        session_ids = [session.session_id for _, session, _ in plans]
        if len(session_ids) != len(set(session_ids)):
            raise RetryError("INVALID_TAB_SELECTION", "The selected tabs resolve to the same session")
        if any(session_id in self.bindings for session_id in session_ids):
            raise RetryError("ALREADY_WATCHED")

        bindings = [Binding(str(uuid.uuid4()), session, identity, connection_epoch=self.adapter.epoch)
                    for _, session, identity in plans]
        # Persist all bindings in one transaction: a bad/missing tab must never
        # leave only part of a multi-tab request active.
        self.store.save_bindings(bindings)
        for binding in bindings:
            self.bindings[binding.session.session_id] = binding
            self.start_monitor(binding)
        return {"mode": Mode.OBSERVE,
                "bindings": [{"tab_number": selection.tab_number, "window_id": selection.window_id,
                              "tab_id": selection.tab_id, **self.binding_status(binding)}
                             for (selection, _, _), binding in zip(plans, bindings, strict=True)]}

    async def _command(self, command, args):
        if command == "status":
            selected = [self.binding(args)] if args.get("session") else self.bindings.values()
            return {"connected": self.adapter.connected, "connection_error": self.connection_error,
                    "store_failed": self.store.failed, "sessions": [self.binding_status(b) for b in selected]}
        if command == "sessions":
            return as_json(await self.adapter.list_sessions())
        if command == "doctor":
            if not args.get("session"):
                raise RetryError("INVALID_REQUEST", "--session is required", exit_code=2)
            return await self.adapter.probe(args["session"])
        if command == "logs":
            return self.store.logs(args.get("limit", 30))
        if command == "request-result":
            return self.store.request_result(args.get("id", ""))
        if command == "shutdown":
            for b in self.bindings.values():
                b.latch.cancel()
            return {"shutting_down": True}
        if command == "watch":
            if "tabs" in args:
                return await self._watch_tabs(args)
            if args.get("current"):
                if args.get("session"):
                    raise RetryError("INVALID_REQUEST", "Choose --current or --session", exit_code=2)
                session_id = await self.adapter.current_session_id()
            else:
                session_id = args["session"]
            if args.get("profile", "codex-local-v1") != "codex-local-v1":
                raise RetryError("PROFILE_MISMATCH")
            if session_id in self.bindings:
                raise RetryError("ALREADY_WATCHED")
            if len(self.bindings) >= self.config.daemon.max_sessions:
                raise RetryError("MAX_SESSIONS")
            paths = self.config.profile.executable_paths
            if args.get("executable"):
                path = Path(args["executable"])
                if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
                    raise RetryError("UNSUPPORTED_PROCESS", "An absolute executable path is required")
                paths = (str(path.resolve()),)
            identity = await self.adapter.identity(session_id, paths)
            sessions = await self.adapter.list_sessions()
            session = next((s for s in sessions if s.session_id == session_id), None)
            if not session:
                raise RetryError("TARGET_GONE")
            b = Binding(str(uuid.uuid4()), session, identity, connection_epoch=self.adapter.epoch)
            self.bindings[session.session_id] = b
            self.store.save_binding(b)
            self.start_monitor(b)
            return self.binding_status(b)
        targets = list(self.bindings.values()) if args.get("all") else [self.binding(args)]
        if command in {"pause", "unwatch"}:
            for b in targets:
                b.pause(State.PAUSED_USER, "USER_ACTIVITY")
                if not self.store.failed:
                    self.store.save_binding(b, active=command != "unwatch")
                if command == "unwatch":
                    task = self.sends.get(b.session.session_id)
                    if task:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                        if not self.store.failed:
                            self.store.save_binding(b, active=False)
                    monitor = self.monitors.pop(b.session.session_id, None)
                    if monitor:
                        monitor.cancel()
                        await asyncio.gather(monitor, return_exceptions=True)
                    del self.bindings[b.session.session_id]
            return [self.binding_status(b) for b in targets]
        b = targets[0]
        if b.inflight_attempt or b.session.session_id in self.sends:
            raise RetryError("ATTEMPT_IN_FLIGHT", "Pause and inspect the current attempt first")
        token = b.latch.token

        def check_control_activity():
            if b.latch.token != token:
                raise RetryError("USER_ACTIVITY", "Activity changed during the control operation")

        if command == "resume":
            await self.adapter.identity(b.session.session_id, (b.identity.exe_realpath,), b.identity)
            check_control_activity()
            b.mode = Mode.OBSERVE
            b.baseline()
            b.connection_epoch = self.adapter.epoch
            # Manual resume explicitly acknowledges previous uncertain sends;
            # consumed events and budget remain intact.
            with self.store.transaction():
                self.store.db.execute("UPDATE attempts SET outcome='USER_REVIEWED' WHERE binding_id=? AND outcome=''",
                                      (b.binding_id,))
                self.store._audit("manual_resume", binding_id=b.binding_id)
            old = self.monitors.pop(b.session.session_id, None)
            if old:
                old.cancel()
                await asyncio.gather(old, return_exceptions=True)
            self.start_monitor(b)
        elif command == "enable":
            if b.state in PAUSED:
                raise RetryError("NEEDS_RESUME", "Inspect the target, then resume before enabling")
            await self.adapter.identity(b.session.session_id, (b.identity.exe_realpath,), b.identity)
            frame = await self.adapter.capture(b.session.session_id)
            check_control_activity()
            observation = self.adapter.profile.classify(frame)
            if observation.blockers or observation.ui_state == "MENU" or observation.composer_state == "USER_TEXT":
                raise RetryError(observation.blockers[0] if observation.blockers else "COMPOSER_NOT_EMPTY")
            if not self.adapter.automatic_ready(b):
                raise RetryError("CAPABILITY_NOT_VALIDATED", "This iTerm2/SDK/Codex combination has not passed live validation", exit_code=6)
            self.store.ensure_run(b.identity.key, self.clock.wall())
            b.mode = Mode.AUTO
            b.baseline()
            b.connection_epoch = self.adapter.epoch
        elif command == "reset-budget":
            self.store.reset_budget(b, self.clock.wall())
            b.mode = Mode.OBSERVE
            b.baseline()
        elif command == "inspect":
            await self.adapter.identity(b.session.session_id, (b.identity.exe_realpath,), b.identity)
            frame = await self.adapter.capture(b.session.session_id)
            check_control_activity()
            o = self.adapter.profile.classify(frame)
            result = {"observation": as_json(o), "automatic_ready": self.adapter.automatic_ready(b),
                      "binding": self.binding_status(b)}
            if o.ready and b.state not in PAUSED:
                token = secrets.token_urlsafe(24)
                now = self.clock.monotonic()
                self.tokens = {k: v for k, v in self.tokens.items() if v["expires"] > now}
                if len(self.tokens) > 1024:
                    self.tokens.clear()
                self.tokens[token] = {"expires": now + 60, "binding_id": b.binding_id,
                                      "latch": b.latch.token, "mode": b.mode,
                                      "epoch": frame.connection_epoch, "signature": o.error_signature,
                                      "anchor": frame.absolute_top, "hash": o.relevant_hash}
                result["evidence_token"] = token
            return result
        elif command == "retry-current":
            proof = self.tokens.pop(args.get("evidence", ""), None)
            if not proof or proof["expires"] < self.clock.monotonic():
                raise RetryError("STALE_EVENT", "Missing, expired or consumed evidence token")
            if b.state in PAUSED or proof["binding_id"] != b.binding_id or proof["latch"] != b.latch.token or proof["mode"] != b.mode:
                raise RetryError("STALE_EVENT")
            if not self.adapter.automatic_ready(b):
                raise RetryError("CAPABILITY_NOT_VALIDATED", exit_code=6)
            await self.adapter.identity(b.session.session_id, (b.identity.exe_realpath,), b.identity)
            frame = await self.adapter.capture(b.session.session_id)
            check_control_activity()
            o = self.adapter.profile.classify(frame)
            if (not o.ready or o.error_signature != proof["signature"]
                or frame.connection_epoch != proof["epoch"] or frame.absolute_top != proof["anchor"]
                or o.relevant_hash != proof["hash"]):
                raise RetryError("STALE_EVENT")
            self.store.ensure_run(b.identity.key, self.clock.wall())
            self.store.ensure_chain(b.identity.key, self.clock.wall())
            b.pending = new_event(b, frame, o, one_shot=True)
            if b.pending.event_id in b.consumed:
                b.pending = None
                raise RetryError("ALREADY_CONSUMED")
            b.state = State.CANDIDATE
            b.connection_epoch = frame.connection_epoch
        else:
            raise RetryError("INVALID_REQUEST")
        self.store.save_binding(b)
        return self.binding_status(b)

    def binding_status(self, b):
        try:
            budget = self.store.budget(b.identity.key, self.clock.wall())
        except Exception:
            budget = {"reason": "STORE_UNAVAILABLE"}
        return {"session_id": b.session.session_id, "title": b.session.title,
                "binding_id": b.binding_id, "pid": b.identity.codex_pid,
                "executable": b.identity.exe_realpath, "mode": b.mode, "state": b.state,
                "reason_code": b.reason, "profile": b.profile_id,
                "next_retry_in_s": max(0, b.pending.deadline - self.clock.monotonic()) if b.pending and b.pending.deadline else None,
                "category": b.pending.category if b.pending else None,
                "in_flight_attempt_id": b.inflight_attempt, "last_resumed_at": b.last_resumed_at,
                "budget": budget}

    async def handle_client(self, reader, writer):
        request_id = None
        command = None
        try:
            peer = writer.get_extra_info("socket")
            if hasattr(peer, "getpeereid"):
                if peer.getpeereid()[0] != os.getuid():
                    raise RetryError("UNAUTHORIZED_CLIENT")
            elif hasattr(socket, "SO_PEERCRED"):
                import struct
                _, uid, _ = struct.unpack("3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                if uid != os.getuid():
                    raise RetryError("UNAUTHORIZED_CLIENT")
            raw = await asyncio.wait_for(reader.readline(), 5)
            if not raw or len(raw) > 65536:
                raise RetryError("INVALID_REQUEST")
            request = json.loads(raw)
            if (not isinstance(request, dict) or set(request) != {"v", "request_id", "command", "args"}
                    or type(request["v"]) is not int or request["v"] != 1):
                raise RetryError("INVALID_REQUEST", exit_code=2)
            request_id, command, args = request["request_id"], request["command"], request["args"]
            if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128 or command not in COMMAND_ARGS:
                raise RetryError("INVALID_REQUEST", exit_code=2)
            self.validate_command(command, args)
            # Revoke immediately, before awaiting the serialized control operation.
            if command in {"pause", "unwatch", "shutdown"} and isinstance(args, dict):
                for b in self.bindings.values():
                    if command == "shutdown" or args.get("all") is True or args.get("session") == b.session.session_id:
                        b.latch.cancel()
            async with self.command_lock:
                cached = None
                record_request = command in MUTATING and not (
                    self.store.failed and command in {"pause", "unwatch", "shutdown"})
                if record_request:
                    cached = self.store.request_begin(request_id, digest([command, args]), self.clock.wall())
                if cached is not None:
                    response = cached
                else:
                    try:
                        result = await self.command(command, args)
                        response = {"v": 1, "request_id": request_id, "ok": True, "result": result, "error": None}
                    except RetryError as exc:
                        response = self.error_response(request_id, exc)
                    if record_request and not self.store.failed:
                        self.store.request_finish(request_id, response)
            writer.write(json.dumps(response, ensure_ascii=False).encode() + b"\n")
            await writer.drain()
            if command == "shutdown" and response["ok"]:
                self.request_stop()
        except Exception as exc:
            error = exc if isinstance(exc, RetryError) else RetryError("INVALID_REQUEST", type(exc).__name__, exit_code=2)
            with suppress(Exception):
                writer.write(json.dumps(self.error_response(request_id, error)).encode() + b"\n")
                await writer.drain()
        finally:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()

    @staticmethod
    def error_response(request_id, exc):
        return {"v": 1, "request_id": request_id, "ok": False, "result": None,
                "error": {"code": exc.code, "message": str(exc), "exit_code": exc.exit_code}}
