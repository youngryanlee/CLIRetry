"""Deterministic screen-to-action reducer; no SDK, I/O, randomness or wall clock."""
from __future__ import annotations

from dataclasses import dataclass

from .config import RecoveryConfig
from .models import (Binding, Composer, FailureEvent, Mode, Observation, PAUSED,
                     RETRYABLE, ScreenFrame, State, UiState, digest)


@dataclass(frozen=True)
class Action:
    kind: str
    reason: str = ""


def new_event(b: Binding, frame: ScreenFrame, o: Observation, *, one_shot=False) -> FailureEvent:
    key = digest([b.binding_id, b.identity.key, b.generation, o.error_signature,
                  frame.absolute_top])
    return FailureEvent(key, b.generation, o.category, o.error_signature,
                        frame.monotonic_at, frame.monotonic_at, o.retry_after_s,
                        frame.seq, b.identity.key, b.schedule_version, one_shot=one_shot)


def frame_step(b: Binding, frame: ScreenFrame, o: Observation, config: RecoveryConfig) -> list[Action]:
    actions = []
    if frame.session_id != b.session.session_id:
        b.pause(State.PAUSED_BLOCKED, "IDENTITY_CHANGED")
        return [Action("paused", b.reason)]
    if b.state in PAUSED or b.inflight_attempt:
        return actions
    old_frame, old = b.previous_frame, b.previous
    if old_frame:
        gap = frame.monotonic_at - old_frame.monotonic_at
        wall_gap = frame.wall_at - old_frame.wall_at
        discontinuous = (frame.connection_epoch != old_frame.connection_epoch
                         or (frame.width, frame.height) != (old_frame.width, old_frame.height)
                         or frame.absolute_top < old_frame.absolute_top
                         or gap < 0 or gap > config.sleep_gap_threshold_s
                         or abs(wall_gap - gap) > config.sleep_gap_threshold_s)
        if discontinuous:
            b.baseline()
            old = None
            actions.append(Action("baseline", "SNAPSHOT_DISCONTINUITY"))
    if frame.selection_length or frame.first_visible_line != frame.absolute_top:
        b.pause(State.PAUSED_USER, "USER_ACTIVITY")
        return [Action("paused", b.reason)]
    if o.composer_state == Composer.USER_TEXT:
        b.pause(State.PAUSED_USER, "COMPOSER_NOT_EMPTY")
        return [Action("paused", b.reason)]
    if o.ui_state == UiState.MENU:
        b.pause(State.PAUSED_BLOCKED, "MENU_PRESENT")
        return [Action("paused", b.reason)]
    if o.category and o.category not in RETRYABLE:
        b.pause(State.PAUSED_BLOCKED, o.category)
        return [Action("paused", b.reason)]
    if o.ui_state == UiState.UNKNOWN or o.blockers:
        # An unknown frame may hide an entire execution: it invalidates the baseline.
        b.baseline()
        b.reason = o.blockers[0] if o.blockers else "PROFILE_MISMATCH"
        b.previous_frame = frame
        return [Action("unverifiable", b.reason)]
    if b.state == State.DISCONNECTED:
        b.baseline()
    if b.state == State.BASELINING:
        baseline_key = digest([o.profile_revision, o.ui_state, o.composer_state,
                               o.category, o.error_signature, frame.width, frame.height,
                               o.composer_row, o.composer_start_x])
        if b.baseline_hash == baseline_key:
            b.baseline_samples += 1
        else:
            b.baseline_hash, b.baseline_samples = baseline_key, 1
        if b.baseline_samples >= 2:
            b.state = State.OBSERVING
            b.reason = ""
            # A running initial frame can establish a real execution edge later.
            b.observed_busy = o.ui_state == UiState.BUSY
        b.previous, b.previous_frame = o, frame
        return actions
    if o.ui_state == UiState.BUSY:
        if not b.observed_busy:
            b.generation += 1
        b.observed_busy = True
        b.pending = None
        b.state = State.OBSERVING
        b.idle_since = None
    elif o.ready:
        b.idle_since = None
        if b.pending and b.pending.signature != o.error_signature:
            b.pending = None
            b.state = State.OBSERVING
        if b.pending is None:
            fresh = b.observed_busy or (old is not None and old.ui_state == UiState.IDLE)
            if fresh:
                if not b.observed_busy:
                    b.generation += 1
                candidate = new_event(b, frame, o)
                if candidate.event_id not in b.consumed:
                    b.pending = candidate
                    b.state = State.CANDIDATE
                    actions.append(Action("new_failure", o.category))
            b.observed_busy = False
        else:
            b.pending.last_seen_mono = frame.monotonic_at
            b.pending.samples += 1
        if b.pending and b.pending.samples >= 2 and (
            frame.monotonic_at - b.pending.first_seen_mono >= config.stable_error_s
        ):
            if b.state == State.CANDIDATE:
                b.state = State.BACKOFF
                actions.append(Action("schedule"))
            elif (b.state == State.BACKOFF and b.pending.deadline
                  and frame.monotonic_at >= b.pending.deadline):
                if b.mode == Mode.OBSERVE and not b.pending.one_shot:
                    if not b.pending.reported:
                        b.pending.reported = True
                        actions.append(Action("would_retry", o.category))
                    b.consumed.add(b.pending.event_id)
                    b.pending = None
                    b.state = State.OBSERVING
                else:
                    actions.append(Action("send_due"))
    else:
        b.pending = None
        b.observed_busy = False
        b.state = State.OBSERVING
        fresh_completion = (old is not None and o.completion_signature
                            and o.completion_signature != old.completion_signature)
        if o.normal_completion and (b.idle_since is not None or fresh_completion):
            if b.idle_since is None:
                b.idle_since = frame.monotonic_at
            elif frame.monotonic_at - b.idle_since >= 10:
                actions.append(Action("close_chain"))
                b.idle_since = float("inf")
        else:
            b.idle_since = None
    b.previous, b.previous_frame = o, frame
    return actions
