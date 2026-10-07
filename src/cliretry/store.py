from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import sqlite3
import stat
import time
import uuid
from contextlib import contextmanager

from .config import RecoveryConfig
from .models import Binding, Mode, PAUSED, ProcessIdentity, RetryError, SessionRef, State, as_json, digest


def private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RetryError("UNSAFE_STATE_DIRECTORY", f"Expected an owned 0700 directory: {path}")


class InstanceLock:
    def __init__(self, directory: Path):
        private_dir(directory)
        self.path = directory / "daemon.lock"
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        self.file = os.fdopen(fd, "r+")
        try:
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.file.close()
            raise RetryError("DAEMON_ALREADY_RUNNING", "This state directory is locked") from exc

    def close(self):
        if not self.file.closed:
            fcntl.flock(self.file, fcntl.LOCK_UN)
            self.file.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta(version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bindings(
 binding_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, data TEXT NOT NULL,
 active INTEGER NOT NULL DEFAULT 1, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS runs(
 run_id TEXT PRIMARY KEY, identity_hash TEXT NOT NULL, started REAL NOT NULL,
 expires REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, closed INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS chains(
 chain_id TEXT PRIMARY KEY, identity_hash TEXT NOT NULL, started REAL NOT NULL,
 attempts INTEGER NOT NULL DEFAULT 0, closed INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS events(
 event_id TEXT PRIMARY KEY, binding_id TEXT NOT NULL, signature TEXT NOT NULL,
 category TEXT NOT NULL, generation INTEGER NOT NULL, consumed INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS attempts(
 attempt_id TEXT PRIMARY KEY, event_id TEXT NOT NULL UNIQUE REFERENCES events(event_id),
 binding_id TEXT NOT NULL, identity_hash TEXT NOT NULL,
 run_id TEXT NOT NULL REFERENCES runs(run_id), chain_id TEXT NOT NULL REFERENCES chains(chain_id),
 reserved REAL NOT NULL, phase TEXT NOT NULL, outcome TEXT NOT NULL DEFAULT '',
 text_ack REAL, enter_ack REAL);
CREATE INDEX IF NOT EXISTS attempts_by_identity_time ON attempts(identity_hash, reserved);
CREATE TABLE IF NOT EXISTS control_requests(
 request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, response TEXT, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS audit(
 id INTEGER PRIMARY KEY, timestamp REAL NOT NULL, binding_id TEXT,
 event_id TEXT, attempt_id TEXT, action TEXT NOT NULL, reason TEXT NOT NULL,
 metadata TEXT NOT NULL DEFAULT '{}');
"""


class Store:
    def __init__(self, directory: Path, recovery: RecoveryConfig):
        private_dir(directory)
        self.path = directory / "state.sqlite3"
        self.recovery = recovery
        self.failed = False
        if not self.path.exists():
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            os.close(fd)
        info = self.path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise RetryError("UNSAFE_STATE_FILE", str(self.path))
        try:
            self.db = sqlite3.connect(self.path, isolation_level=None)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA busy_timeout=2000")
            if self.db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RetryError("STORE_UNAVAILABLE", "SQLite integrity check failed")
            # Do not mutate a database whose schema version is not supported.
            exists = self.db.execute("SELECT 1 FROM sqlite_master WHERE name='schema_meta'").fetchone()
            if exists:
                rows = self.db.execute("SELECT version FROM schema_meta").fetchall()
                if len(rows) != 1 or rows[0][0] != 1:
                    raise RetryError("STORE_UNAVAILABLE", "Unsupported database schema")
            self.db.executescript(SCHEMA)
            if not exists:
                self.db.execute("INSERT INTO schema_meta VALUES (1)")
            for suffix in ("-wal", "-shm"):
                side = Path(str(self.path) + suffix)
                if side.exists():
                    os.chmod(side, 0o600)
        except sqlite3.Error as exc:
            self.failed = True
            raise RetryError("STORE_UNAVAILABLE", str(exc), exit_code=5) from exc

    @contextmanager
    def transaction(self):
        if self.failed:
            raise RetryError("STORE_UNAVAILABLE", "Store is disabled after an earlier failure")
        try:
            self.db.execute("BEGIN IMMEDIATE")
            yield self.db
            self.db.execute("COMMIT")
        except Exception as exc:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            if isinstance(exc, sqlite3.Error):
                self.failed = True
                raise RetryError("STORE_UNAVAILABLE", str(exc), exit_code=5) from exc
            raise

    def _audit(self, action, reason="", *, binding_id=None, event_id=None,
               attempt_id=None, now=None, metadata=None):
        self.db.execute("INSERT INTO audit(timestamp,binding_id,event_id,attempt_id,action,reason,metadata) "
                        "VALUES (?,?,?,?,?,?,?)", (time.time() if now is None else now, binding_id,
                                                 event_id, attempt_id, action, reason,
                                                 json.dumps(metadata or {})))

    def audit(self, action, reason="", **kwargs):
        with self.transaction():
            self._audit(action, reason, **kwargs)

    def _save_binding_row(self, b: Binding, *, active=True, now=None):
        data = {"binding_id": b.binding_id, "session": as_json(b.session),
                "identity": as_json(b.identity), "profile_id": b.profile_id,
                "profile_revision": b.profile_revision, "generation": b.generation,
                "mode": b.mode.value, "state": b.state.value, "reason": b.reason,
                "last_resumed_at": b.last_resumed_at}
        self.db.execute("INSERT INTO bindings VALUES (?,?,?,?,?) ON CONFLICT(binding_id) DO UPDATE "
                        "SET data=excluded.data,active=excluded.active,updated=excluded.updated",
                        (b.binding_id, b.session.session_id, json.dumps(data), int(active),
                         time.time() if now is None else now))

    def save_binding(self, b: Binding, *, active=True, now=None):
        with self.transaction():
            self._save_binding_row(b, active=active, now=now)

    def save_bindings(self, bindings, *, active=True, now=None):
        """Persist a multi-session selection atomically."""
        bindings = list(bindings)
        session_ids = [b.session.session_id for b in bindings]
        if len(session_ids) != len(set(session_ids)):
            raise RetryError("INVALID_TAB_SELECTION", "A session was selected more than once")
        with self.transaction():
            for binding in bindings:
                self._save_binding_row(binding, active=active, now=now)

    def load_bindings(self) -> list[Binding]:
        result = []
        for row in self.db.execute("SELECT data FROM bindings WHERE active=1"):
            data = json.loads(row[0])
            data["session"] = SessionRef(**data["session"])
            data["identity"] = ProcessIdentity(**data["identity"])
            data["mode"] = Mode.OBSERVE
            saved_state = State(data["state"])
            data["state"] = saved_state if saved_state in PAUSED else State.BASELINING
            b = Binding(**data)
            b.consumed = {r[0] for r in self.db.execute(
                "SELECT event_id FROM events WHERE binding_id=? AND consumed=1", (b.binding_id,))}
            if self.db.execute("SELECT 1 FROM attempts WHERE binding_id=? AND outcome='' LIMIT 1",
                               (b.binding_id,)).fetchone():
                b.state = State.PAUSED_UNCONFIRMED
                b.reason = "SEND_OUTCOME_UNKNOWN"
            result.append(b)
        return result

    def _clock(self, now: float):
        previous = self.db.execute("SELECT value FROM metadata WHERE key='last_wall'").fetchone()
        if previous and now < float(previous[0]) - 5:
            raise RetryError("CLOCK_DISCONTINUITY", "Wall clock moved backwards")
        self.db.execute("INSERT INTO metadata VALUES ('last_wall',?) ON CONFLICT(key) DO UPDATE "
                        "SET value=excluded.value", (str(now),))

    def _ensure_run(self, identity: str, now: float):
        run = self.db.execute("SELECT * FROM runs WHERE identity_hash=? AND closed=0 ORDER BY started DESC LIMIT 1",
                              (identity,)).fetchone()
        if not run:
            run_id = str(uuid.uuid4())
            self.db.execute("INSERT INTO runs VALUES (?,?,?,?,0,0)",
                            (run_id, identity, now, now + self.recovery.max_enabled_duration_s))
            run = self.db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return run

    def ensure_run(self, identity: str, now: float):
        with self.transaction():
            self._clock(now)
            return dict(self._ensure_run(identity, now))

    def _chain(self, identity: str):
        return self.db.execute("SELECT * FROM chains WHERE identity_hash=? AND closed=0 ORDER BY started DESC LIMIT 1",
                               (identity,)).fetchone()

    def ensure_chain(self, identity: str, now: float):
        with self.transaction():
            if not self._chain(identity):
                self.db.execute("INSERT INTO chains VALUES (?,?,?,0,0)",
                                (str(uuid.uuid4()), identity, now))

    def close_chain(self, identity: str):
        with self.transaction():
            self.db.execute("UPDATE chains SET closed=1 WHERE identity_hash=?", (identity,))

    def cleanup(self, now: float):
        """Remove only old inactive bindings with no unresolved sends or live run.

        Event/attempt retention is never used to reset active budgets.
        """
        cutoff = now - 30 * 86400
        with self.transaction():
            rows = self.db.execute(
                "SELECT b.binding_id FROM bindings b WHERE active=0 AND updated<? "
                "AND NOT EXISTS(SELECT 1 FROM attempts a WHERE a.binding_id=b.binding_id AND a.outcome='') "
                "AND NOT EXISTS(SELECT 1 FROM attempts a JOIN runs r ON a.run_id=r.run_id "
                "WHERE a.binding_id=b.binding_id AND r.closed=0 AND r.expires>?)", (cutoff, now)).fetchall()
            for row in rows:
                bid = row[0]
                self.db.execute("DELETE FROM attempts WHERE binding_id=?", (bid,))
                self.db.execute("DELETE FROM events WHERE binding_id=?", (bid,))
                self.db.execute("DELETE FROM audit WHERE binding_id=? AND timestamp<?", (bid, cutoff))
                self.db.execute("DELETE FROM bindings WHERE binding_id=?", (bid,))
            self.db.execute("DELETE FROM control_requests WHERE created<? AND response IS NOT NULL", (cutoff,))
            self.db.execute("DELETE FROM runs WHERE expires<? AND NOT EXISTS "
                            "(SELECT 1 FROM attempts a WHERE a.run_id=runs.run_id)", (cutoff,))
            self.db.execute("DELETE FROM chains WHERE closed=1 AND started<? AND NOT EXISTS "
                            "(SELECT 1 FROM attempts a WHERE a.chain_id=chains.chain_id)", (cutoff,))

    def budget(self, identity: str, now: float, *, prospective_at: float | None = None) -> dict:
        r = self.recovery
        run = self.db.execute("SELECT * FROM runs WHERE identity_hash=? AND closed=0 ORDER BY started DESC LIMIT 1",
                              (identity,)).fetchone()
        chain = self._chain(identity)
        hour = self.db.execute("SELECT COUNT(*) FROM attempts WHERE identity_hash=? AND reserved>?",
                               (identity, now - 3600)).fetchone()[0]
        last = self.db.execute("SELECT MAX(reserved) FROM attempts").fetchone()[0]
        at = now if prospective_at is None else prospective_at
        reason = ""
        if run and at >= run["expires"]:
            reason = "RUN_EXPIRED"
        elif run and run["attempts"] >= r.max_attempts_per_run:
            reason = "RUN_BUDGET_EXHAUSTED"
        elif hour >= r.max_attempts_per_session_hour:
            reason = "HOURLY_BUDGET_EXHAUSTED"
        elif chain and (chain["attempts"] >= r.max_attempts_per_chain
                        or at - chain["started"] >= r.max_chain_elapsed_s):
            reason = "CHAIN_BUDGET_EXHAUSTED"
        return {"reason": reason, "chain_attempts": chain["attempts"] if chain else 0,
                "hour_attempts": hour, "run_attempts": run["attempts"] if run else 0,
                "run_expires": run["expires"] if run else None,
                "global_next_at": last + r.global_min_attempt_gap_s if last is not None else 0}

    def reserve(self, b: Binding, now: float) -> str:
        event = b.pending
        if event is None or (b.mode != Mode.AUTO and not event.one_shot):
            raise RetryError("NOT_AUTHORIZED")
        with self.transaction():
            self._clock(now)
            run = self._ensure_run(b.identity.key, now)
            chain = self._chain(b.identity.key)
            if not chain:
                raise RetryError("CHAIN_MISSING")
            budget = self.budget(b.identity.key, now)
            if budget["reason"]:
                raise RetryError(budget["reason"])
            if now < budget["global_next_at"]:
                raise RetryError("GLOBAL_BACKOFF")
            found = self.db.execute("SELECT consumed FROM events WHERE event_id=?", (event.event_id,)).fetchone()
            if found and found[0]:
                raise RetryError("ALREADY_CONSUMED")
            self.db.execute("INSERT INTO events VALUES (?,?,?,?,?,1) ON CONFLICT(event_id) "
                            "DO UPDATE SET consumed=1", (event.event_id, b.binding_id, event.signature,
                                                        event.category, event.generation))
            attempt = str(uuid.uuid4())
            self.db.execute("INSERT INTO attempts(attempt_id,event_id,binding_id,identity_hash,run_id,chain_id,reserved,phase) "
                            "VALUES (?,?,?,?,?,?,?,'RESERVED')",
                            (attempt, event.event_id, b.binding_id, b.identity.key, run["run_id"],
                             chain["chain_id"], now))
            self.db.execute("UPDATE runs SET attempts=attempts+1 WHERE run_id=?", (run["run_id"],))
            self.db.execute("UPDATE chains SET attempts=attempts+1 WHERE chain_id=?", (chain["chain_id"],))
            self._audit("reserved", binding_id=b.binding_id, event_id=event.event_id,
                        attempt_id=attempt, now=now, metadata={
                            "source_seq": event.source_seq, "first_seen_mono": event.first_seen_mono,
                            "last_seen_mono": event.last_seen_mono, "connection_epoch": b.connection_epoch,
                            "schedule_version": event.schedule_version,
                            "profile_revision": b.profile_revision,
                            "prompt_hash": digest(self.recovery.prompt),
                            "activity_seq": b.latch.activity_seq, "cancel_version": b.latch.cancel_version,
                        })
        b.consumed.add(event.event_id)
        return attempt

    def phase(self, attempt: str, phase: str, now: float, *, outcome="", reason=""):
        allowed = {"TYPED", "SUBMITTING", "SUBMITTED", "FINISHED", "UNKNOWN"}
        if phase not in allowed:
            raise ValueError(phase)
        with self.transaction():
            changed = self.db.execute("UPDATE attempts SET phase=?,outcome=? WHERE attempt_id=?",
                                      (phase, outcome, attempt)).rowcount
            if not changed:
                raise RetryError("STORE_UNAVAILABLE", "Missing attempt")
            if phase in {"TYPED", "SUBMITTED"}:
                column = "text_ack" if phase == "TYPED" else "enter_ack"
                self.db.execute(f"UPDATE attempts SET {column}=? WHERE attempt_id=?", (now, attempt))
            self._audit(phase.lower(), reason, attempt_id=attempt, now=now)

    def reset_budget(self, b: Binding, now: float):
        with self.transaction():
            self._clock(now)
            self.db.execute("UPDATE runs SET closed=1 WHERE identity_hash=?", (b.identity.key,))
            self.db.execute("UPDATE chains SET closed=1 WHERE identity_hash=?", (b.identity.key,))
            self._ensure_run(b.identity.key, now)
            self._audit("budget_reset", binding_id=b.binding_id, now=now)

    def logs(self, limit=30):
        return [dict(r) for r in self.db.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))]

    def request_begin(self, request_id, fingerprint, now):
        with self.transaction():
            row = self.db.execute("SELECT * FROM control_requests WHERE request_id=?", (request_id,)).fetchone()
            if row:
                if row["fingerprint"] != fingerprint:
                    raise RetryError("REQUEST_ID_CONFLICT")
                if row["response"] is None:
                    raise RetryError("REQUEST_OUTCOME_UNKNOWN", "The request was accepted; do not replay it")
                return json.loads(row["response"])
            self.db.execute("INSERT INTO control_requests VALUES (?,?,NULL,?)", (request_id, fingerprint, now))
            return None

    def request_finish(self, request_id, response):
        with self.transaction():
            self.db.execute("UPDATE control_requests SET response=? WHERE request_id=?",
                            (json.dumps(response), request_id))

    def request_result(self, request_id):
        row = self.db.execute("SELECT response FROM control_requests WHERE request_id=?", (request_id,)).fetchone()
        if not row:
            raise RetryError("REQUEST_NOT_FOUND")
        return json.loads(row[0]) if row[0] else {"pending_or_unknown": True}

    def close(self):
        self.db.close()
