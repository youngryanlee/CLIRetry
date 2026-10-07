"""Accelerated fault replay; explicitly not a real-time resource endurance run."""
from dataclasses import replace

from cliretry.config import RecoveryConfig
from cliretry.engine import frame_step, new_event
from cliretry.models import Mode, State
from cliretry.sender import Sender
from cliretry.store import Store
from tests.helpers import FakeAdapter, FakeClock, binding, frame


async def test_eight_virtual_hours_restart_disconnect_pause_and_budget(tmp_path):
    config = replace(RecoveryConfig(), max_attempts_per_chain=2)
    clock = FakeClock()
    store = Store(tmp_path, config)
    b = binding()
    sends = 0
    for hour in range(8):
        clock.at = 100 + hour * 3600
        if hour:
            store.close()
            store = Store(tmp_path, config)
            b = store.load_bindings()[0]
            assert b.mode == Mode.OBSERVE and b.pending is None
            assert store.budget(b.identity.key, clock.wall())["run_attempts"] == 2
        # Explicit operator reset/enable, never an automatic restart reset.
        store.reset_budget(b, clock.wall())
        b.baseline()
        b.mode = Mode.AUTO
        adapter = FakeAdapter(clock)
        for at, kind in ((0, "idle"), (2, "idle"), (4, "busy"), (6, "capacity")):
            f = frame(kind, at=clock.at + at)
            frame_step(b, f, adapter.profile.classify(f), config)
        assert b.pending
        disconnected = replace(frame("capacity", at=clock.at + 8), connection_epoch=2)
        frame_step(b, disconnected, adapter.profile.classify(disconnected), config)
        assert b.pending is None  # old error/deadline invalidated by reconnection
        b.pause(State.PAUSED_USER, "USER_ACTIVITY")
        assert not frame_step(b, frame("capacity", at=clock.at + 10),
                              adapter.profile.classify(frame("capacity")), config)
        b.baseline()  # explicit manual resume; preserve budget and consumed IDs
        b.connection_epoch = 1
        store.ensure_chain(b.identity.key, clock.wall())
        for index in range(3):
            b.generation += 1
            b.state = State.BACKOFF
            f = frame("capacity", at=clock.at)
            b.pending = new_event(b, f, adapter.profile.classify(f))
            adapter.phase = "initial"
            await Sender(adapter, store, config, clock).execute(b)
            clock.at += 30
            if index < 2:
                assert b.state == State.OBSERVING
            else:
                assert b.state == State.PAUSED_BLOCKED
                assert b.reason == "CHAIN_BUDGET_EXHAUSTED"
        assert adapter.calls == ["继续", "\r", "继续", "\r"]
        sends += len(adapter.calls) // 2
    clock.at = 100 + 8 * 3600
    assert sends == 16
    assert store.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 16
    assert store.db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    store.close()
