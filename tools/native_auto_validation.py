"""Owned native test only: exercise production reducer/scheduler/Sender.

The release registry is intentionally NOT changed. A session/identity/epoch-bound
test certification gate is used only while the local fixture owns the target.
"""
import asyncio
from dataclasses import replace
import json

from cliretry.config import Config
from cliretry.daemon import Daemon
from cliretry.models import Composer, PAUSED, State, UiState
from cliretry.sender import Sender
from cliretry.store import Store


async def validate(adapter, sid, executable, root, release_error, result, *, config=None):
    config = config or Config()
    config = replace(config, daemon=replace(config.daemon, state_dir=str(root / "state")))
    daemon = Daemon(config, adapter=adapter)
    daemon.store = Store(root / "state", config.recovery)
    daemon.sender = Sender(adapter, daemon.store, config.recovery)
    original_gate = adapter.automatic_ready
    original_type, original_submit = adapter.guarded_type, adapter.guarded_submit
    counts = {"text": 0, "submit": 0}
    result["validation_gate"] = "owned_session_identity_epoch_only_not_release_certification"

    async def typed(*args):
        counts["text"] += 1
        await original_type(*args)

    async def submitted(*args):
        counts["submit"] += 1
        await original_submit(*args)

    try:
        async with asyncio.timeout(110):
            # The loopback server holds the initial request until AUTO has a
            # stable BUSY baseline. No error-token or stale-error bypass is used.
            while True:
                frame = await adapter.capture(sid)
                if adapter.profile.classify(frame).ui_state == UiState.BUSY:
                    break
                await asyncio.sleep(.25)
            await daemon.command("watch", {"session": sid, "executable": executable})
            b = daemon.bindings[sid]
            identity, epoch = b.identity, adapter.epoch
            while sid not in adapter.monitor_ready:
                task = daemon.monitors[sid]
                if task.done():
                    raise RuntimeError("Native keyboard monitor failed")
                await asyncio.sleep(.05)
            adapter.automatic_ready = lambda target: (
                target is b and target.session.session_id == sid
                and target.identity == identity and adapter.epoch == epoch
                and adapter.connected and sid in adapter.monitor_ready)
            adapter.guarded_type, adapter.guarded_submit = typed, submitted
            await daemon.command("enable", {"session": sid})
            for _ in range(2):
                await daemon.poll_binding(b)
                await asyncio.sleep(config.daemon.poll_interval_s)
            if b.state != State.OBSERVING or not b.observed_busy:
                raise RuntimeError("Native BUSY baseline was not established")
            result["auto_baseline_established"] = True
            result["native_identity"] = {"sha256": identity.exe_sha256,
                                         "executable": identity.exe_realpath}
            result["requests_before_auto"] = result["provider_requests"]
            release_error.set()
            with (root / "auto-timeline.jsonl").open("x") as timeline:
                while True:
                    if sid not in daemon.sends:
                        await daemon.poll_binding(b)
                    attempts = [dict(row) for row in daemon.store.db.execute(
                        "SELECT phase,outcome FROM attempts")]
                    timeline.write(json.dumps({"state": b.state, "reason": b.reason,
                                               "attempts": attempts}) + "\n")
                    timeline.flush()
                    if attempts and sid not in daemon.sends:
                        result["auto_attempts"] = attempts
                        if (len(attempts) != 1 or attempts[0]["outcome"] not in {"RESUMED", "REFAILED"}
                                or counts != {"text": 1, "submit": 1} or b.latch.activity_seq):
                            raise RuntimeError("Native AUTO attempt did not safely confirm recovery")
                        if result["provider_requests"] <= result["requests_before_auto"]:
                            raise RuntimeError("No new native provider request")
                        result["native_auto_complete"] = True
                        break
                    if b.state in PAUSED:
                        raise RuntimeError(f"Native AUTO paused: {b.reason}")
                    await asyncio.sleep(.25)
    finally:
        release_error.set()
        for b in daemon.bindings.values():
            b.pause(State.PAUSED_USER, "TEST_COMPLETE")
        tasks = list(daemon.sends.values()) + list(daemon.monitors.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        result["auto_guarded_calls"] = counts
        result["input_calls"] = sum(counts.values())
        result["auto_keyboard_events"] = sum(b.latch.activity_seq for b in daemon.bindings.values())
        adapter.automatic_ready = original_gate
        adapter.guarded_type, adapter.guarded_submit = original_type, original_submit
        daemon.store.close()


async def validate_idle(adapter, sid, executable, root, result, *, duration_s=12, config=None):
    """Verify native AUTO observes an empty idle session without sending input."""
    config = config or Config()
    config = replace(config, daemon=replace(config.daemon, state_dir=str(root / "idle-state")))
    daemon = Daemon(config, adapter=adapter)
    daemon.store = Store(root / "idle-state", config.recovery)
    daemon.sender = Sender(adapter, daemon.store, config.recovery)
    original_gate = adapter.automatic_ready
    original_type, original_submit = adapter.guarded_type, adapter.guarded_submit
    counts = {"text": 0, "submit": 0}
    result["auto_idle_gate"] = "owned_session_identity_epoch_only_not_release_certification"
    result["provider_source"] = "none_no_task_submitted"
    result["provider_requests"] = 0
    result["auto_idle_requested_duration_s"] = duration_s

    async def typed(*args):
        counts["text"] += 1
        await original_type(*args)

    async def submitted(*args):
        counts["submit"] += 1
        await original_submit(*args)

    try:
        stable_idle = 0
        async with asyncio.timeout(60 + duration_s):
            while stable_idle < 2:
                frame = await adapter.capture(sid)
                observation = adapter.profile.classify(frame)
                if (observation.ui_state == UiState.IDLE
                        and observation.composer_state in (Composer.EMPTY, Composer.PLACEHOLDER)):
                    stable_idle += 1
                else:
                    stable_idle = 0
                await asyncio.sleep(min(config.daemon.poll_interval_s, .25))
            result["auto_idle_preflight_stable_samples"] = stable_idle
            await daemon.command("watch", {"session": sid, "executable": executable})
            b = daemon.bindings[sid]
            identity, epoch = b.identity, adapter.epoch
            while sid not in adapter.monitor_ready:
                task = daemon.monitors[sid]
                if task.done():
                    raise RuntimeError("Native AUTO idle keyboard monitor failed")
                await asyncio.sleep(.05)
            adapter.automatic_ready = lambda target: (
                target is b and target.session.session_id == sid
                and target.identity == identity and adapter.epoch == epoch
                and adapter.connected and sid in adapter.monitor_ready)
            adapter.guarded_type, adapter.guarded_submit = typed, submitted
            await daemon.command("enable", {"session": sid})
            result["auto_idle_enabled"] = True

            samples = []
            timeline_path = root / "auto-idle-timeline.jsonl"
            with timeline_path.open("x", encoding="utf-8") as timeline:
                deadline = asyncio.get_running_loop().time() + duration_s
                while asyncio.get_running_loop().time() < deadline:
                    await daemon.poll_binding(b)
                    observation = b.previous
                    if (observation is None or observation.ui_state != UiState.IDLE
                            or observation.composer_state not in (Composer.EMPTY, Composer.PLACEHOLDER)):
                        raise RuntimeError("Native AUTO idle UI changed; zero-send validation stopped")
                    if b.state in PAUSED or b.pending or b.latch.activity_seq:
                        raise RuntimeError(f"Native AUTO idle validation paused: {b.reason or 'user activity'}")
                    attempts = int(daemon.store.db.execute("SELECT count(*) FROM attempts").fetchone()[0])
                    if attempts or sid in daemon.sends or counts != {"text": 0, "submit": 0}:
                        raise RuntimeError("Native AUTO sent input while the target was idle")
                    sample = {"ui_state": observation.ui_state, "composer_state": observation.composer_state,
                              "normal_completion": observation.normal_completion,
                              "attempts": attempts, "guarded_calls": dict(counts)}
                    samples.append(sample)
                    timeline.write(json.dumps(sample) + "\n")
                    timeline.flush()
                    await asyncio.sleep(min(config.daemon.poll_interval_s, .25))
            result["auto_idle_samples"] = len(samples)
            result["auto_idle_normal_completion"] = any(item["normal_completion"] for item in samples)
            result["auto_idle_zero_attempts"] = all(item["attempts"] == 0 for item in samples)
            result["auto_idle_complete"] = bool(samples) and result["auto_idle_zero_attempts"]
            if not result["auto_idle_complete"]:
                raise RuntimeError("Native AUTO idle zero-send validation produced no clean samples")
    finally:
        for b in daemon.bindings.values():
            b.pause(State.PAUSED_USER, "TEST_COMPLETE")
        tasks = list(daemon.sends.values()) + list(daemon.monitors.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        result["auto_idle_guarded_calls"] = counts
        result["input_calls"] = sum(counts.values())
        result["auto_idle_keyboard_events"] = sum(b.latch.activity_seq for b in daemon.bindings.values())
        adapter.automatic_ready = original_gate
        adapter.guarded_type, adapter.guarded_submit = original_type, original_submit
        daemon.store.close()


async def validate_completion(adapter, sid, executable, root, release_success, result,
                              *, duration_s=12, config=None):
    """Verify native AUTO sees a completed local response and sends nothing."""
    config = config or Config()
    config = replace(config, daemon=replace(config.daemon, state_dir=str(root / "completion-state")))
    daemon = Daemon(config, adapter=adapter)
    daemon.store = Store(root / "completion-state", config.recovery)
    daemon.sender = Sender(adapter, daemon.store, config.recovery)
    original_gate = adapter.automatic_ready
    original_type, original_submit = adapter.guarded_type, adapter.guarded_submit
    counts = {"text": 0, "submit": 0}
    result["auto_completion_gate"] = "owned_session_identity_epoch_only_not_release_certification"
    result["auto_completion_requested_duration_s"] = duration_s
    result["completion_provider_source"] = result.get("provider_source")

    async def typed(*args):
        counts["text"] += 1
        await original_type(*args)

    async def submitted(*args):
        counts["submit"] += 1
        await original_submit(*args)

    try:
        async with asyncio.timeout(120 + duration_s):
            # The successful loopback response stays held while AUTO records BUSY.
            while True:
                frame = await adapter.capture(sid)
                if adapter.profile.classify(frame).ui_state == UiState.BUSY:
                    break
                await asyncio.sleep(.25)
            await daemon.command("watch", {"session": sid, "executable": executable})
            b = daemon.bindings[sid]
            identity, epoch = b.identity, adapter.epoch
            while sid not in adapter.monitor_ready:
                task = daemon.monitors[sid]
                if task.done():
                    raise RuntimeError("Native AUTO completion keyboard monitor failed")
                await asyncio.sleep(.05)
            adapter.automatic_ready = lambda target: (
                target is b and target.session.session_id == sid
                and target.identity == identity and adapter.epoch == epoch
                and adapter.connected and sid in adapter.monitor_ready)
            adapter.guarded_type, adapter.guarded_submit = typed, submitted
            await daemon.command("enable", {"session": sid})
            for _ in range(2):
                await daemon.poll_binding(b)
                await asyncio.sleep(config.daemon.poll_interval_s)
            if b.state != State.OBSERVING or not b.observed_busy:
                raise RuntimeError("Native AUTO completion BUSY baseline was not established")
            result["auto_completion_baseline_established"] = True
            result["requests_before_release"] = result["provider_requests"]
            release_success.set()

            samples = []
            completion_seen_at = None
            timeline_path = root / "auto-completion-timeline.jsonl"
            with timeline_path.open("x", encoding="utf-8") as timeline:
                while True:
                    if sid not in daemon.sends:
                        await daemon.poll_binding(b)
                    observation = b.previous
                    attempts = int(daemon.store.db.execute("SELECT count(*) FROM attempts").fetchone()[0])
                    if b.state in PAUSED or b.pending or b.latch.activity_seq:
                        raise RuntimeError(f"Native AUTO completion validation paused: {b.reason or 'user activity'}")
                    if attempts or sid in daemon.sends or counts != {"text": 0, "submit": 0}:
                        raise RuntimeError("Native AUTO sent input after normal completion")
                    if observation is None:
                        raise RuntimeError("Native AUTO completion observation was missing")
                    if observation.normal_completion and (
                            observation.ui_state != UiState.IDLE or observation.category
                            or observation.ready
                            or observation.composer_state not in (Composer.EMPTY, Composer.PLACEHOLDER)):
                        raise RuntimeError("Native normal completion was misclassified as retryable or unverifiable")
                    if observation.normal_completion and "provider_requests_at_completion" not in result:
                        result["provider_requests_at_completion"] = result["provider_requests"]
                    sample = {"ui_state": observation.ui_state,
                              "composer_state": observation.composer_state,
                              "normal_completion": observation.normal_completion,
                              "category": observation.category, "attempts": attempts,
                              "guarded_calls": dict(counts)}
                    samples.append(sample)
                    timeline.write(json.dumps(sample) + "\n")
                    timeline.flush()
                    now = asyncio.get_running_loop().time()
                    if observation.normal_completion:
                        completion_seen_at = completion_seen_at or now
                    elif completion_seen_at is not None:
                        raise RuntimeError("Native normal-completion evidence did not remain stable")
                    if completion_seen_at is not None and now - completion_seen_at >= duration_s:
                        break
                    await asyncio.sleep(min(config.daemon.poll_interval_s, .25))
            result["auto_completion_samples"] = len(samples)
            result["auto_completion_stable_s"] = duration_s
            result["auto_completion_zero_attempts"] = all(item["attempts"] == 0 for item in samples)
            result["auto_completion_zero_input"] = counts == {"text": 0, "submit": 0}
            result["auto_completion_complete"] = bool(samples) and result["auto_completion_zero_attempts"]
            if not result["auto_completion_complete"]:
                raise RuntimeError("Native AUTO normal-completion zero-send validation failed")
    finally:
        release_success.set()
        for b in daemon.bindings.values():
            b.pause(State.PAUSED_USER, "TEST_COMPLETE")
        tasks = list(daemon.sends.values()) + list(daemon.monitors.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        result["auto_completion_guarded_calls"] = counts
        result["auto_completion_keyboard_events"] = sum(
            b.latch.activity_seq for b in daemon.bindings.values())
        result["input_calls"] = sum(counts.values())
        adapter.automatic_ready = original_gate
        adapter.guarded_type, adapter.guarded_submit = original_type, original_submit
        daemon.store.close()
