from __future__ import annotations

import asyncio

from .models import RetryError, State, UiState
from .scheduler import Clock
from .engine import new_event


class Sender:
    """One attempt, at most one text call and one submit call. Never retries RPCs."""

    def __init__(self, adapter, store, config, clock=None):
        self.adapter = adapter
        self.store = store
        self.config = config
        self.clock = clock or Clock()

    async def execute(self, binding):
        event = binding.pending
        if event is None:
            return
        token = binding.latch.token
        attempt = None
        phase = "BEFORE_RESERVE"
        last_tick, last_wall = self.clock.monotonic(), self.clock.wall()

        def check():
            nonlocal last_tick, last_wall
            tick, wall = self.clock.monotonic(), self.clock.wall()
            elapsed = tick - last_tick
            if (elapsed < 0 or elapsed > self.config.sleep_gap_threshold_s
                    or abs(wall - last_wall - elapsed) > self.config.sleep_gap_threshold_s):
                raise RetryError("CLOCK_DISCONTINUITY")
            last_tick, last_wall = tick, wall
            if binding.latch.token != token:
                raise RetryError("USER_ACTIVITY")

        try:
            await self.adapter.identity(binding.session.session_id,
                                        (binding.identity.exe_realpath,), binding.identity)
            check()
            initial = await self.adapter.capture(binding.session.session_id)
            check()
            observation = self.adapter.profile.classify(initial)
            if not observation.ready or observation.error_signature != event.signature:
                raise RetryError("STALE_EVENT")
            if not self.adapter.automatic_ready(binding):
                raise RetryError("CAPABILITY_NOT_VALIDATED")
            if initial.connection_epoch != binding.connection_epoch:
                raise RetryError("SNAPSHOT_DISCONTINUITY")
            initial_markers = self.adapter.profile.submission_markers(initial, self.config.prompt)
            earliest_new_marker = initial.absolute_top + observation.composer_row
            attempt = self.store.reserve(binding, self.clock.wall())
            binding.inflight_attempt = attempt
            binding.state = State.PREPARING
            phase = "TYPE_CALL"
            await self.adapter.guarded_type(binding, event, self.config.prompt, token)
            check()
            self.store.phase(attempt, "TYPED", self.clock.wall())
            phase = "TYPED"
            binding.state = State.VERIFYING_TEXT
            typed_at = self.clock.monotonic()
            confirmed_at = None
            await self.clock.sleep(self.config.text_min_settle_s)
            check()
            while self.clock.monotonic() - typed_at <= self.config.text_confirm_timeout_s:
                frame = await self.adapter.capture(binding.session.session_id)
                check()
                o = self.adapter.profile.classify(frame)
                if (o.error_signature != event.signature or frame.connection_epoch != binding.connection_epoch
                    or (frame.width, frame.height) != (initial.width, initial.height)
                    or frame.absolute_top < initial.absolute_top):
                    raise RetryError("STALE_EVENT")
                if self.adapter.profile.verify_typed_text(frame, self.config.prompt):
                    if confirmed_at is not None and self.clock.monotonic() - confirmed_at >= .25:
                        break
                    confirmed_at = self.clock.monotonic()
                else:
                    confirmed_at = None
                await self.clock.sleep(.25)
                check()
            else:
                raise RetryError("TEXT_NOT_CONFIRMED")
            self.store.phase(attempt, "SUBMITTING", self.clock.wall())
            phase = "SUBMIT_CALL"
            await self.adapter.guarded_submit(binding, event, self.config.prompt, token)
            check()
            self.store.phase(attempt, "SUBMITTED", self.clock.wall())
            phase = "SUBMITTED"
            binding.state = State.WAIT_ACK
            ack_start = self.clock.monotonic()
            while self.clock.monotonic() - ack_start <= self.config.recovery_ack_timeout_s:
                await self.clock.sleep(.25)
                check()
                frame = await self.adapter.capture(binding.session.session_id)
                check()
                if (frame.connection_epoch != binding.connection_epoch
                    or (frame.width, frame.height) != (initial.width, initial.height)
                    or frame.absolute_top < initial.absolute_top):
                    raise RetryError("SNAPSHOT_DISCONTINUITY")
                o = self.adapter.profile.classify(frame)
                if o.ui_state == UiState.BUSY and o.busy_evidence:
                    self.store.phase(attempt, "FINISHED", self.clock.wall(), outcome="RESUMED")
                    binding.pending = None
                    binding.state = State.OBSERVING
                    binding.generation += 1
                    binding.observed_busy = True
                    binding.previous, binding.previous_frame = o, frame
                    binding.last_resumed_at = self.clock.wall()
                    return
                markers = self.adapter.profile.submission_markers(frame, self.config.prompt) - initial_markers
                if o.ready and any(earliest_new_marker <= marker < frame.absolute_top + o.error_row
                                   for marker in markers):
                    self.store.phase(attempt, "FINISHED", self.clock.wall(), outcome="REFAILED")
                    binding.generation += 1
                    binding.pending = new_event(binding, frame, o)
                    binding.state = State.CANDIDATE
                    binding.observed_busy = False
                    binding.previous, binding.previous_frame = o, frame
                    return
            raise RetryError("ACK_TIMEOUT")
        except (Exception, asyncio.CancelledError) as exc:
            code = exc.code if isinstance(exc, RetryError) else "SEND_OUTCOME_UNKNOWN"
            if code == "GLOBAL_BACKOFF" and attempt is None and binding.pending is event:
                budget = self.store.budget(binding.identity.key, self.clock.wall())
                binding.pending.deadline = self.clock.monotonic() + max(
                    .01, budget["global_next_at"] - self.clock.wall())
                binding.state = State.BACKOFF
                return
            if attempt:
                try:
                    self.store.phase(attempt, "UNKNOWN", self.clock.wall(), reason=code)
                except RetryError:
                    code = "STORE_UNAVAILABLE"
            target_state = State.PAUSED_UNCONFIRMED if phase != "BEFORE_RESERVE" else State.PAUSED_BLOCKED
            if code == "USER_ACTIVITY":
                target_state = State.PAUSED_USER
            if code in {"IDENTITY_CHANGED", "TARGET_GONE"}:
                target_state = State.TARGET_GONE
            binding.pause(target_state, code)
            if isinstance(exc, asyncio.CancelledError):
                raise
        finally:
            binding.inflight_attempt = None
            self.store.save_binding(binding)
