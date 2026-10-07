from dataclasses import replace

import pytest

from cliretry.config import RecoveryConfig
from cliretry.engine import frame_step
from cliretry.models import Mode, State
from cliretry.profiles import CodexProfile
from cliretry.scheduler import schedule
from tests.helpers import binding, frame


def step(b, f):
    return frame_step(b, f, CodexProfile().classify(f), RecoveryConfig())


def armed(mode=Mode.AUTO):
    b = binding()
    b.mode = mode
    step(b, frame(at=0))
    step(b, frame(at=2))
    return b


def failure(b):
    step(b, frame("busy", at=4))
    step(b, frame("capacity", at=8))
    actions = step(b, frame("capacity", at=14))
    assert any(a.kind == "schedule" for a in actions)
    b.pending.deadline = 38


def test_new_failure_schedule_once():
    b = armed()
    failure(b)
    assert b.state == State.BACKOFF
    for t in (16, 18, 20, 22, 24, 26, 28, 30, 32, 34, 36):
        assert not any(a.kind == "send_due" for a in step(b, frame("capacity", at=t)))
    assert [a.kind for a in step(b, frame("capacity", at=38))] == ["send_due"]


def test_startup_error_is_history():
    b = binding()
    b.mode = Mode.AUTO
    for t in range(0, 600, 2):
        assert not step(b, frame("capacity", at=t))
    assert b.pending is None


def test_observe_only_reports_once():
    b = armed(Mode.OBSERVE)
    failure(b)
    reports = []
    for t in range(16, 600, 2):
        reports.extend(a.kind for a in step(b, frame("capacity", at=t)))
    assert reports.count("would_retry") == 1
    assert "send_due" not in reports


def test_busy_cancels_pending_and_same_error_can_recur():
    b = armed()
    failure(b)
    first = b.pending.event_id
    step(b, frame("busy", at=16))
    assert b.pending is None
    step(b, frame("capacity", at=18))
    assert b.pending.event_id != first


@pytest.mark.parametrize("change", ["resize", "gap", "epoch", "scroll", "selection"])
def test_discontinuity_never_sends_stale(change):
    b = armed()
    failure(b)
    f = frame("capacity", at=16)
    if change == "resize":
        f = frame("capacity", at=16, width=120)
    elif change == "gap":
        f = frame("capacity", at=1000)
    elif change == "epoch":
        f = replace(f, connection_epoch=2)
    elif change == "scroll":
        f = replace(f, first_visible_line=-10)
    else:
        f = replace(f, selection_length=1)
    assert not any(a.kind == "send_due" for a in step(b, f))
    assert b.pending is None


def test_user_input_and_menu_cancel():
    for f in [frame("capacity", at=16, user_text="hello"), frame("menu", at=16)]:
        b = armed()
        failure(b)
        step(b, f)
        assert b.state in {State.PAUSED_USER, State.PAUSED_BLOCKED}
        assert b.pending is None


def test_provider_delay_not_clamped():
    b = armed()
    failure(b)
    b.pending.retry_after_s = 1800
    deadline = schedule(RecoveryConfig(), b.pending, 1, now_mono=14, now_wall=1014,
                        global_next=0, random_value=0)
    assert deadline == 1808


def test_auto_normal_completion_never_sends():
    b = armed(Mode.AUTO)
    for t in range(4, 3600, 2):
        actions = step(b, frame("complete", at=t))
        assert all(a.kind not in {"schedule", "send_due"} for a in actions)


def test_wrong_session_frame_rejected():
    b = armed()
    step(b, replace(frame("capacity", at=4), session_id="another-session"))
    assert b.state == State.PAUSED_BLOCKED
    assert b.reason == "IDENTITY_CHANGED"


def test_changing_busy_output_can_establish_baseline():
    b = binding()
    b.mode = Mode.AUTO
    first = frame("busy", at=0)
    lines = tuple(line.replace("1s", "3s") for line in first.lines)
    step(b, first)
    step(b, replace(frame("busy", at=2), lines=lines))
    assert b.state == State.OBSERVING
    assert b.observed_busy
    assert [a.kind for a in step(b, frame("capacity", at=4))] == ["new_failure"]


def test_chain_close_requires_stable_normal_completion():
    b = armed()
    failure(b)
    for t in (16, 18, 20):
        assert not any(a.kind == "close_chain" for a in step(b, frame("busy", at=t)))
    for t in (22, 24, 26):
        assert not any(a.kind == "close_chain" for a in step(b, frame("idle", at=t)))
    closed = []
    for t in range(28, 60, 2):
        closed.extend(a.kind for a in step(b, frame("complete", at=t)))
    assert closed.count("close_chain") == 1


def test_historical_completion_marker_cannot_reset_new_failure_chain():
    b = armed()
    old_marker = "─ Worked for 1m ─"
    for kind, at in (("busy", 4), ("capacity", 6), ("idle", 8), ("idle", 12), ("idle", 20)):
        f = frame(kind, at=at)
        lines = list(f.lines)
        lines[16] = old_marker
        actions = step(b, replace(f, lines=tuple(lines)))
        assert not any(a.kind == "close_chain" for a in actions)
