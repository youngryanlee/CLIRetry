"""One Sender attempt in an owned synthetic TUI, using real iTerm2 RPCs.

The identity/certification substitution is confined to this test harness.
Production still rejects Python and keeps its compatibility gate.
"""
import argparse
import asyncio
from contextlib import suppress
import hashlib
import json
import os
from pathlib import Path
import shlex
import sys

import iterm2
import psutil

from cliretry.adapters.iterm2_adapter import Iterm2Adapter
from cliretry.config import Config
from cliretry.engine import new_event
from cliretry.models import Binding, Mode, ProcessIdentity, RetryError, SessionRef, State, as_json
from cliretry.sender import Sender
from cliretry.store import Store


class OwnedTestAdapter(Iterm2Adapter):
    def __init__(self, config):
        super().__init__(config)
        self.owned = {}
        self.fault = None
        self.human_gate = None
        self.input_calls = {"text": 0, "submit": 0}

    async def guarded_type(self, *args):
        if self.human_gate:
            await self.human_gate("before-type")
        # Count calls that reach the production guarded method, even if its
        # checks reject them. The byte log separately proves actual delivery.
        self.input_calls["text"] += 1
        await super().guarded_type(*args)
        if self.human_gate:
            await self.human_gate("after-type")
        if self.fault == "type_reply":
            raise OSError("Injected loss after real text delivery")

    async def guarded_submit(self, *args):
        self.input_calls["submit"] += 1
        await super().guarded_submit(*args)
        if self.fault == "submit_reply":
            raise OSError("Injected loss after real Enter delivery")

    def automatic_ready(self, b):
        return b.session.session_id in self.owned and b.session.session_id in self.monitor_ready

    async def identity(self, sid, paths, expected=None):
        if sid not in self.owned:
            raise AssertionError("Test adapter cannot access an unowned target")
        original = self.owned[sid]
        values = await self.variables(sid)
        if int(values["jobPid"]) != original.codex_pid or values["tty"] != original.tty_path:
            raise RetryError("IDENTITY_CHANGED")
        await self.check_liveness(original)
        if expected is not None and expected != original:
            raise RetryError("IDENTITY_CHANGED")
        return original


def test_identity(values):
    process = psutil.Process(int(values["jobPid"]))
    iterm = psutil.Process(int(values["iterm2.pid"]))
    root = psutil.Process(int(values["pid"]))
    path = Path(process.exe())
    st, terminal = path.stat(), os.stat(values["tty"])
    with path.open("rb") as stream:
        fingerprint = hashlib.file_digest(stream, "sha256").hexdigest()
    return ProcessIdentity(iterm.pid, iterm.create_time(), root.pid, root.create_time(),
                           process.pid, process.create_time(), str(path), st.st_dev, st.st_ino,
                           fingerprint, st.st_size, st.st_mtime_ns, st.st_ctime_ns,
                           values["tty"], terminal.st_rdev, os.getpgid(process.pid), os.getuid())


async def run(args):
    root = Path(args.output_dir).absolute()
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    config = Config()
    adapter = OwnedTestAdapter(config)
    adapter.fault = args.fault
    window, witness_window, monitor, store = None, None, None, None
    broadcast_installed = False
    owned_ids = set()
    result = {"source": "real_iterm2_synthetic_tui", "complete": False}
    try:
        await adapter.connect()
        custom = iterm2.LocalWriteOnlyProfile()
        custom.set_name("CLIRetry isolated Sender test")
        custom.set_use_custom_command(iterm2.Profile.USE_CUSTOM_COMMAND_ENABLED)
        custom.set_command(shlex.join([sys.executable, str(Path(__file__).with_name("fake_codex_tui.py").absolute()),
                                        "--output", str(root / "stdin.jsonl"), "--duration", "90"]))
        window = await asyncio.wait_for(iterm2.Window.async_create(
            adapter.connection, profile_customizations=custom), 10)
        if window is None:
            raise RuntimeError("Test session exited")
        sid = window.tabs[0].sessions[0].session_id
        owned_ids.add(sid)
        result.update({"window_id": window.window_id, "session_id": sid})
        (root / "ownership.json").write_text(json.dumps(result))
        await adapter.rpc(adapter.app.async_refresh())
        async with asyncio.timeout(10):
            while not (root / "stdin.jsonl").exists():
                await asyncio.sleep(.1)
        await asyncio.sleep(.5)
        initial = await adapter.capture(sid)
        (root / "initial.json").write_text(json.dumps(as_json(initial), ensure_ascii=False))
        if not adapter.profile.classify(initial).ready:
            raise RuntimeError("Initial synthetic test composer is not ready")
        identity = await asyncio.to_thread(test_identity, initial.variables)
        adapter.owned[sid] = identity
        # Exercise the real production rejection before enabling test substitution.
        try:
            await Iterm2Adapter.identity(adapter, sid, (identity.exe_realpath,))
        except RetryError as exc:
            result["production_identity_rejected"] = exc.code
        else:
            raise AssertionError("Production probe accepted Python")
        b = Binding("isolated-sender", SessionRef(sid), identity, connection_epoch=adapter.epoch,
                    mode=Mode.AUTO, state=State.BACKOFF)
        result["keyboard_events"] = 0
        human_key = asyncio.Event()

        async def watch_keys():
            async for _ in adapter.activities(sid):
                result["keyboard_events"] += 1
                b.latch.cancel(activity=True)
                human_key.set()

        async def human_gate(phase):
            if phase != args.human_activity:
                return
            # This wait occurs after Sender captured its authorization token.
            # A real SDK keyboard notification must revoke that exact token.
            if human_key.is_set():
                raise RetryError("USER_ACTIVITY", "Activity occurred before the requested test phase")
            notice = {"human_input_required": True, "phase": phase, "session_id": sid,
                      "window_id": window.window_id, "key": "x", "press_enter": False,
                      "timeout_s": args.human_timeout}
            (root / "human-input-ready.json").write_text(json.dumps(notice, indent=2))
            print(json.dumps(notice), flush=True)
            try:
                await asyncio.wait_for(human_key.wait(), args.human_timeout)
            except TimeoutError as exc:
                raise RetryError("HUMAN_INPUT_TIMEOUT", "No keyboard event; never continue sending") from exc
            # Return normally: the actual adapter/Sender guards must detect
            # the monitor's latch change. Do not inject a synthetic failure.

        if args.human_activity:
            adapter.human_gate = human_gate

        monitor = asyncio.create_task(watch_keys())
        async with asyncio.timeout(5):
            while sid not in adapter.monitor_ready:
                if monitor.done():
                    await monitor
                    raise RuntimeError("Monitor exited")
                await asyncio.sleep(.05)
        if args.broadcast:
            witness_profile = iterm2.LocalWriteOnlyProfile()
            witness_profile.set_name("CLIRetry broadcast witness")
            witness_profile.set_use_custom_command(iterm2.Profile.USE_CUSTOM_COMMAND_ENABLED)
            witness_profile.set_command(shlex.join([
                sys.executable, str(Path(__file__).with_name("fake_codex_tui.py").absolute()),
                "--output", str(root / "witness-stdin.jsonl"), "--duration", "90"]))
            witness_window = await asyncio.wait_for(window.async_create_tab(
                profile_customizations=witness_profile, select=False), 10)
            if witness_window is None:
                raise RuntimeError("Witness session exited")
            witness = witness_window.sessions[0]
            owned_ids.add(witness.session_id)
            result["witness_session_id"] = witness.session_id
            (root / "ownership.json").write_text(json.dumps(result))
            await adapter.rpc(adapter.app.async_refresh())
            domain = iterm2.BroadcastDomain()
            domain.add_session(adapter.app.get_session_by_id(sid))
            domain.add_session(witness)
            before = list(adapter.app.broadcast_domains)
            if any(owned_ids & {s.session_id for s in d.sessions} for d in before):
                raise RuntimeError("Unexpected existing broadcast on test sessions")
            await adapter.rpc(iterm2.async_set_broadcast_domains(adapter.connection, before + [domain]))
            broadcast_installed = True
            async with asyncio.timeout(5):
                while not any({s.session_id for s in d.sessions} == owned_ids
                              for d in adapter.app.broadcast_domains):
                    await asyncio.sleep(.05)
            result["broadcast_domain_verified"] = True
            async with asyncio.timeout(5):
                while not (root / "witness-stdin.jsonl").exists():
                    await asyncio.sleep(.05)
        store = Store(root / "state", config.recovery)
        store.save_binding(b)
        store.ensure_chain(identity.key, initial.wall_at)
        b.pending = new_event(b, initial, adapter.profile.classify(initial))
        await Sender(adapter, store, config.recovery).execute(b)
        result.update({"state": b.state, "reason": b.reason, "transport": adapter.transport,
                       "iterm_version": adapter.app_version(), "sdk_version": adapter.sdk_version,
                       "guarded_input_calls": dict(adapter.input_calls),
                       "attempts": [dict(r) for r in store.db.execute("SELECT phase,outcome FROM attempts")]})
        if args.human_activity:
            # Let the raw TUI flush the physical key to its private byte log.
            await asyncio.sleep(.25)
        final = await adapter.capture(sid)
        (root / "final.json").write_text(json.dumps(as_json(final), ensure_ascii=False))
        received = b"".join(bytes.fromhex(json.loads(line)["hex"]) for line in (root / "stdin.jsonl").read_text().splitlines())
        result["received_hex"] = received.hex()
        result["complete"] = (received == "继续\r".encode() and b.state == State.OBSERVING
                              and result["attempts"] == [{"phase": "FINISHED", "outcome": "RESUMED"}])
        if args.fault:
            store.close()
            store = Store(root / "state", config.recovery)
            restored = store.load_bindings()[0]
            expected_bytes = ("继续" if args.fault == "type_reply" else "继续\r").encode()
            result.update({"fault": args.fault, "restart_state": restored.state,
                           "restart_mode": restored.mode})
            result["complete"] = (received == expected_bytes and b.state == State.PAUSED_UNCONFIRMED
                                  and restored.state == State.PAUSED_UNCONFIRMED
                                  and restored.mode == Mode.OBSERVE
                                  and result["attempts"] == [{"phase": "UNKNOWN", "outcome": ""}])
        if args.human_activity:
            prefix = "继续" if args.human_activity == "after-type" else ""
            result["human_activity_phase"] = args.human_activity
            result["activity_source"] = "external_keyboard_notification_requires_operator_attestation"
            result["complete"] = (
                received == (prefix + "x").encode()
                and result["keyboard_events"] > 0
                and b.state == State.PAUSED_USER and b.reason == "USER_ACTIVITY"
                and adapter.input_calls == {"text": 1, "submit": 0}
                and result["attempts"] == [{"phase": "UNKNOWN", "outcome": ""}])
        if args.broadcast:
            witness_data = (root / "witness-stdin.jsonl").read_bytes()
            result["witness_input_bytes"] = len(witness_data)
            result["complete"] = result["complete"] and not witness_data
        if not result["complete"]:
            raise RuntimeError("Live Sender validation failed")
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        if broadcast_installed and adapter.connected:
            # Remove only this harness's sessions, preserving current user domains.
            domains = []
            for existing in adapter.app.broadcast_domains:
                remaining = [s for s in existing.sessions if s.session_id not in owned_ids]
                if remaining:
                    domain = iterm2.BroadcastDomain()
                    for session in remaining:
                        domain.add_session(session)
                    domains.append(domain)
            try:
                await adapter.rpc(iterm2.async_set_broadcast_domains(adapter.connection, domains))
                result["test_broadcast_removed"] = True
            except Exception as exc:
                result["broadcast_cleanup_error"] = type(exc).__name__
        if monitor:
            monitor.cancel()
            with suppress(Exception, asyncio.CancelledError):
                await monitor
        if witness_window:
            try:
                await asyncio.wait_for(witness_window.async_close(force=True), 5)
                result["witness_tab_closed"] = True
            except Exception as exc:
                result["witness_cleanup_error"] = type(exc).__name__
        if window:
            try:
                await adapter.rpc(adapter.app.async_refresh())
                current = next((w for w in adapter.app.windows if w.window_id == window.window_id), None)
                if current:
                    current_ids = {s.session_id for tab in current.tabs for s in tab.sessions}
                    if not current_ids <= owned_ids:
                        raise RuntimeError("Test window contains unowned sessions; leaving it open")
                    await asyncio.wait_for(current.async_close(force=True), 5)
                result["window_closed"] = True
            except Exception as exc:
                result["cleanup_error"] = type(exc).__name__
        if store:
            store.close()
        await adapter.close()
        (root / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--broadcast", action="store_true", help="Add a second owned session and test broadcast suppression")
    parser.add_argument("--fault", choices=["type_reply", "submit_reply"], help="Inject a lost reply after a real SDK input call")
    parser.add_argument("--human-activity", choices=["before-type", "after-type"],
                        help="Wait for a real operator to type x without Enter in the owned test tab")
    parser.add_argument("--human-timeout", type=float, default=10,
                        help="Seconds to wait for the operator; timeout fails without further input")
    args = parser.parse_args()
    if args.human_activity and (args.broadcast or args.fault):
        parser.error("Human activity tests must run separately from broadcast/fault tests")
    if not 1 <= args.human_timeout <= 10:
        parser.error("Human timeout must be between 1 and 10 seconds (below the sleep-gap guard)")
    asyncio.run(run(args))
