import pytest
from dataclasses import replace

from cliretry.config import RecoveryConfig
from cliretry.engine import new_event
from cliretry.models import Mode, RetryError, State
from cliretry.sender import Sender
from cliretry.store import InstanceLock, Store
from tests.helpers import FakeAdapter, FakeClock, binding, frame


def setup(tmp_path, **adapter_kwargs):
    clock = FakeClock()
    config = RecoveryConfig()
    store = Store(tmp_path, config)
    b = binding()
    b.mode = Mode.AUTO
    b.state = State.BACKOFF
    adapter = FakeAdapter(clock, **adapter_kwargs)
    f = frame("capacity", at=clock.monotonic())
    b.pending = new_event(b, f, adapter.profile.classify(f))
    store.save_binding(b)
    store.ensure_chain(b.identity.key, clock.wall())
    return b, adapter, store, clock, config


async def test_sender_delivers_exactly_once(tmp_path):
    b, adapter, store, clock, config = setup(tmp_path)
    await Sender(adapter, store, config, clock).execute(b)
    assert adapter.calls == ["继续", "\r"]
    assert b.state == State.OBSERVING
    assert store.db.execute("SELECT outcome FROM attempts").fetchone()[0] == "RESUMED"
    assert store.budget(b.identity.key, clock.wall())["chain_attempts"] == 1
    store.close()


@pytest.mark.parametrize("failure,expected", [("type", ["继续"]), ("submit", ["继续", "\r"]), ("identity", [])])
async def test_unknown_delivery_never_replayed(tmp_path, failure, expected):
    b, adapter, store, clock, config = setup(tmp_path, fail_at=failure)
    await Sender(adapter, store, config, clock).execute(b)
    assert adapter.calls == expected
    assert b.state in {State.PAUSED_UNCONFIRMED, State.TARGET_GONE}
    store.close()
    restored = Store(tmp_path, config)
    loaded = restored.load_bindings()[0]
    assert loaded.mode == Mode.OBSERVE
    if failure != "identity":
        assert loaded.state == State.PAUSED_UNCONFIRMED
        assert restored.budget(b.identity.key, clock.wall())["chain_attempts"] == 1
    restored.close()


async def test_edited_input_never_gets_enter(tmp_path):
    b, adapter, store, clock, config = setup(tmp_path, user_edit=True)
    await Sender(adapter, store, config, clock).execute(b)
    assert adapter.calls == ["继续"]
    assert b.reason == "TEXT_NOT_CONFIRMED"
    store.close()


async def test_empty_composer_after_submit_is_not_ack(tmp_path):
    b, adapter, store, clock, config = setup(tmp_path, after_submit="idle")
    await Sender(adapter, store, config, clock).execute(b)
    assert adapter.calls == ["继续", "\r"]
    assert b.reason == "ACK_TIMEOUT"
    store.close()


async def test_user_revocation_before_typing(tmp_path):
    b, adapter, store, clock, config = setup(tmp_path)
    adapter.revocation = lambda: b.latch.cancel(activity=True)
    await Sender(adapter, store, config, clock).execute(b)
    assert adapter.calls == []
    assert b.state == State.PAUSED_USER
    store.close()


def test_event_reservation_is_unique_and_budget_survives_restart(tmp_path):
    b, adapter, store, clock, config = setup(tmp_path)
    store.reserve(b, clock.wall())
    with pytest.raises(RetryError):
        store.reserve(b, clock.wall() + 30)
    assert store.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 1
    store.close()
    store = Store(tmp_path, config)
    assert store.budget(b.identity.key, clock.wall() + 60)["run_attempts"] == 1
    store.close()


def test_reset_preserves_rolling_hour_and_consumed_events(tmp_path):
    b, adapter, store, clock, config = setup(tmp_path)
    store.reserve(b, clock.wall())
    store.reset_budget(b, clock.wall() + 30)
    budget = store.budget(b.identity.key, clock.wall() + 30)
    assert budget["hour_attempts"] == 1
    assert budget["run_attempts"] == 0
    with pytest.raises(RetryError, match="ALREADY_CONSUMED|CHAIN_MISSING"):
        store.reserve(b, clock.wall() + 60)
    store.close()


def test_clock_rollback_blocks_reservation(tmp_path):
    b, adapter, store, clock, config = setup(tmp_path)
    store.ensure_run(b.identity.key, clock.wall())
    with pytest.raises(RetryError, match="Wall clock"):
        store.reserve(b, clock.wall() - 100)
    store.close()


def test_instance_lock(tmp_path):
    first = InstanceLock(tmp_path)
    try:
        with pytest.raises(RetryError, match="locked"):
            InstanceLock(tmp_path)
    finally:
        first.close()


async def test_global_gap_reschedules_instead_of_pausing(tmp_path):
    b, adapter, store, clock, config = setup(tmp_path)
    store.reserve(b, clock.wall())
    second = replace(b, binding_id="second", identity=replace(b.identity, codex_pid=4),
                     session=replace(b.session, session_id="other"), consumed=set())
    f = frame("capacity", at=clock.monotonic())
    second.pending = new_event(second, f, adapter.profile.classify(f))
    store.save_binding(second)
    store.ensure_chain(second.identity.key, clock.wall())
    await Sender(adapter, store, config, clock).execute(second)
    assert second.state == State.BACKOFF
    assert second.pending.deadline == clock.monotonic() + config.global_min_attempt_gap_s
    assert adapter.calls == []
    store.close()


async def test_new_submission_then_immediate_failure_is_a_new_event(tmp_path):
    b, adapter, store, clock, config = setup(tmp_path, after_submit="capacity")
    original_capture = adapter.capture
    old_event = b.pending.event_id

    async def capture(session_id):
        result = await original_capture(session_id)
        if adapter.phase == "submitted":
            lines = list(result.lines)
            lines[16] = "› 继续"
            result = replace(result, lines=tuple(lines), absolute_top=5, first_visible_line=5)
        return result

    adapter.capture = capture
    await Sender(adapter, store, config, clock).execute(b)
    assert adapter.calls == ["继续", "\r"]
    assert b.state == State.CANDIDATE
    assert b.pending.event_id != old_event
    assert store.db.execute("SELECT outcome FROM attempts").fetchone()[0] == "REFAILED"
    assert store.budget(b.identity.key, clock.wall())["chain_attempts"] == 1
    store.close()


async def test_same_failure_without_new_submission_pauses(tmp_path):
    b, adapter, store, clock, config = setup(tmp_path, after_submit="capacity")
    await Sender(adapter, store, config, clock).execute(b)
    assert adapter.calls == ["继续", "\r"]
    assert b.state == State.PAUSED_UNCONFIRMED
    assert b.reason == "ACK_TIMEOUT"
    store.close()


@pytest.mark.parametrize("kind", ["run", "hour", "chain", "duration"])
def test_each_budget_gate(tmp_path, kind):
    b, adapter, store, clock, config = setup(tmp_path)
    config = replace(config, max_attempts_per_chain=1 if kind == "chain" else 12,
                     max_attempts_per_run=1 if kind == "run" else 100,
                     max_attempts_per_session_hour=1 if kind == "hour" else 30,
                     max_enabled_duration_s=10 if kind == "duration" else 43200)
    store.recovery = config
    store.reserve(b, clock.wall())
    reason = store.budget(b.identity.key, clock.wall() + 20)["reason"]
    assert reason == {"run": "RUN_BUDGET_EXHAUSTED", "hour": "HOURLY_BUDGET_EXHAUSTED",
                      "chain": "CHAIN_BUDGET_EXHAUSTED", "duration": "RUN_EXPIRED"}[kind]
    store.close()


async def test_sqlite_write_failure_prevents_all_input(tmp_path):
    b, adapter, store, clock, config = setup(tmp_path)
    store.db.execute("PRAGMA query_only=ON")
    with pytest.raises(RetryError, match="Store is disabled"):
        await Sender(adapter, store, config, clock).execute(b)
    assert adapter.calls == []
    assert store.failed
    store.close()


def test_corrupt_database_is_not_replaced(tmp_path):
    target = tmp_path / "state.sqlite3"
    original = b"not a sqlite database"
    target.write_bytes(original)
    target.chmod(0o600)
    with pytest.raises(RetryError):
        Store(tmp_path, RecoveryConfig())
    assert target.read_bytes() == original


async def test_revocation_between_text_and_enter(tmp_path):
    b, adapter, store, clock, config = setup(tmp_path)
    original_type = adapter.guarded_type

    async def type_and_revoke(*args):
        await original_type(*args)
        b.latch.cancel(activity=True)

    adapter.guarded_type = type_and_revoke
    await Sender(adapter, store, config, clock).execute(b)
    assert adapter.calls == ["继续"]
    assert b.state == State.PAUSED_USER
    store.close()


@pytest.mark.parametrize("phase", ["typed", "submitted"])
@pytest.mark.parametrize("wall_only", [False, True])
async def test_sleep_during_send_never_proceeds(tmp_path, phase, wall_only):
    b, adapter, store, clock, config = setup(tmp_path)
    method = "guarded_type" if phase == "typed" else "guarded_submit"
    original = getattr(adapter, method)

    async def suspend(*args):
        await original(*args)
        if wall_only:
            clock.wall = lambda: 1000 + clock.at + 60
        else:
            clock.at += 60

    setattr(adapter, method, suspend)
    await Sender(adapter, store, config, clock).execute(b)
    assert adapter.calls == (["继续"] if phase == "typed" else ["继续", "\r"])
    assert b.reason == "CLOCK_DISCONTINUITY"
    assert b.state == State.PAUSED_UNCONFIRMED
    store.close()


@pytest.mark.parametrize("phase", ["RESERVED", "TYPED", "SUBMITTING", "SUBMITTED", "UNKNOWN"])
def test_restart_never_replays_any_unresolved_phase(tmp_path, phase):
    b, _, store, clock, config = setup(tmp_path)
    attempt = store.reserve(b, clock.wall())
    if phase != "RESERVED":
        store.phase(attempt, phase, clock.wall())
    store.close()
    restored = Store(tmp_path, config)
    loaded = restored.load_bindings()[0]
    assert loaded.state == State.PAUSED_UNCONFIRMED
    assert loaded.mode == Mode.OBSERVE and loaded.pending is None
    assert restored.budget(b.identity.key, clock.wall())["chain_attempts"] == 1
    restored.close()


def test_explicit_pause_survives_restart(tmp_path):
    b, _, store, _, config = setup(tmp_path)
    b.pause(State.PAUSED_USER, "USER_ACTIVITY")
    store.save_binding(b)
    store.close()
    restored = Store(tmp_path, config)
    loaded = restored.load_bindings()[0]
    assert loaded.state == State.PAUSED_USER
    assert loaded.mode == Mode.OBSERVE
    restored.close()
