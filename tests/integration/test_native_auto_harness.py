"""Harness regression with fake terminal; not live certification evidence."""
import asyncio
from dataclasses import replace
import sys
import threading

import pytest

from cliretry.config import Config, DaemonConfig, RecoveryConfig
from cliretry.scheduler import Clock
from tests.helpers import FakeAdapter, binding, frame
from tools.native_auto_validation import validate, validate_completion, validate_idle


@pytest.mark.parametrize("fail_at", [None, "type"])
async def test_native_auto_harness_complete_or_fail_closed(tmp_path, fail_at):
    release = threading.Event()
    result = {"provider_requests": 1}

    class Adapter(FakeAdapter):
        monitor_ready = set()

        async def list_sessions(self):
            return [binding().session]

        async def activities(self, sid):
            self.monitor_ready.add(sid)
            try:
                await asyncio.Event().wait()
                yield None
            finally:
                self.monitor_ready.discard(sid)

        async def capture(self, sid):
            if not release.is_set():
                return replace(frame("busy", at=self.clock.monotonic()), session_id=sid)
            return await super().capture(sid)

        async def guarded_submit(self, *args):
            await super().guarded_submit(*args)
            result["provider_requests"] += 1

    adapter = Adapter(Clock(), fail_at=fail_at)
    original_gate = adapter.automatic_ready
    config = Config(daemon=DaemonConfig(poll_interval_s=.01), recovery=RecoveryConfig(
        stable_error_s=.01, builtin_retry_grace_s=.01, backoff_base_s=.01))
    operation = validate(adapter, "test-session", sys.executable, tmp_path, release,
                         result, config=config)
    if fail_at:
        with pytest.raises(RuntimeError, match="did not safely confirm"):
            await operation
        assert not result.get("native_auto_complete")
        assert adapter.calls == ["继续"]
    else:
        await operation
        assert result["native_auto_complete"]
        assert result["auto_attempts"] == [{"phase": "FINISHED", "outcome": "RESUMED"}]
        assert adapter.calls == ["继续", "\r"]
    assert result["auto_baseline_established"]
    assert release.is_set()
    assert not adapter.monitor_ready
    assert adapter.automatic_ready == original_gate


async def test_native_auto_idle_harness_observes_without_sending(tmp_path):
    class IdleAdapter(FakeAdapter):
        monitor_ready = set()

        async def list_sessions(self):
            return [binding().session]

        async def activities(self, sid):
            self.monitor_ready.add(sid)
            try:
                await asyncio.Event().wait()
                yield None
            finally:
                self.monitor_ready.discard(sid)

        async def capture(self, sid):
            return replace(frame("idle", at=self.clock.monotonic()), session_id=sid)

    adapter = IdleAdapter(Clock())
    original_gate = adapter.automatic_ready
    config = Config(daemon=DaemonConfig(poll_interval_s=.01))
    result = {}

    await validate_idle(adapter, "test-session", sys.executable, tmp_path, result,
                        duration_s=.05, config=config)

    assert result["auto_idle_enabled"]
    assert result["auto_idle_complete"]
    assert result["auto_idle_zero_attempts"]
    assert result["auto_idle_samples"] > 0
    assert result["auto_idle_guarded_calls"] == {"text": 0, "submit": 0}
    assert result["input_calls"] == 0
    assert result["auto_idle_keyboard_events"] == 0
    assert not adapter.monitor_ready
    assert adapter.calls == []
    assert adapter.automatic_ready == original_gate


async def test_native_auto_completion_harness_observes_without_sending(tmp_path):
    release = threading.Event()

    class CompletionAdapter(FakeAdapter):
        monitor_ready = set()

        async def list_sessions(self):
            return [binding().session]

        async def activities(self, sid):
            self.monitor_ready.add(sid)
            try:
                await asyncio.Event().wait()
                yield None
            finally:
                self.monitor_ready.discard(sid)

        async def capture(self, sid):
            kind = "complete" if release.is_set() else "busy"
            return replace(frame(kind, at=self.clock.monotonic()), session_id=sid)

    adapter = CompletionAdapter(Clock())
    original_gate = adapter.automatic_ready
    config = Config(daemon=DaemonConfig(poll_interval_s=.01))
    result = {"provider_requests": 1, "provider_source": "local_loopback_success_sse"}

    await validate_completion(adapter, "test-session", sys.executable, tmp_path,
                              release, result, duration_s=.05, config=config)

    assert release.is_set()
    assert result["auto_completion_baseline_established"]
    assert result["auto_completion_complete"]
    assert result["auto_completion_zero_attempts"]
    assert result["auto_completion_zero_input"]
    assert result["auto_completion_samples"] > 0
    assert result["auto_completion_guarded_calls"] == {"text": 0, "submit": 0}
    assert result["auto_completion_keyboard_events"] == 0
    assert not adapter.monitor_ready
    assert adapter.calls == []
    assert adapter.automatic_ready == original_gate
