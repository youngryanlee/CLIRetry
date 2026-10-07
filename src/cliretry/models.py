from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Mode(StrEnum):
    OBSERVE = "OBSERVE"
    AUTO = "AUTO"


class State(StrEnum):
    BASELINING = "BASELINING"
    OBSERVING = "OBSERVING"
    CANDIDATE = "CANDIDATE"
    BACKOFF = "BACKOFF"
    PREPARING = "PREPARING"
    VERIFYING_TEXT = "VERIFYING_TEXT"
    WAIT_ACK = "WAIT_ACK"
    PAUSED_USER = "PAUSED_USER"
    PAUSED_BLOCKED = "PAUSED_BLOCKED"
    PAUSED_UNCONFIRMED = "PAUSED_UNCONFIRMED"
    DISCONNECTED = "DISCONNECTED"
    TARGET_GONE = "TARGET_GONE"


class UiState(StrEnum):
    BUSY = "BUSY"
    READY_ERROR = "READY_ERROR"
    IDLE = "IDLE"
    MENU = "MENU"
    UNKNOWN = "UNKNOWN"


class Composer(StrEnum):
    EMPTY = "EMPTY"
    PLACEHOLDER = "PLACEHOLDER"
    USER_TEXT = "USER_TEXT"
    UNKNOWN = "UNKNOWN"


RETRYABLE = frozenset({
    "CAPACITY", "RATE_LIMIT_TRANSIENT", "CONNECTION_TRANSIENT",
    "STREAM_TRANSIENT", "UPSTREAM_TRANSIENT",
})


class RetryError(Exception):
    def __init__(self, code: str, message: str = "", *, exit_code: int = 4):
        super().__init__(message or code)
        self.code = code
        self.exit_code = exit_code


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     default=str).encode()).hexdigest()


def as_json(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {k: as_json(v) for k, v in dataclasses.asdict(value).items()}
    if isinstance(value, dict):
        return {k: as_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [as_json(v) for v in value]
    return value


@dataclass(frozen=True)
class SessionRef:
    session_id: str
    window_id: str = ""
    tab_id: str = ""
    title: str = ""
    job_name: str = ""


@dataclass(frozen=True)
class TabSelection:
    """A one-time, user-facing tab ordinal resolved in the active window."""
    tab_number: int
    window_id: str
    tab_id: str
    sessions: tuple[SessionRef, ...]


@dataclass(frozen=True)
class ProcessIdentity:
    iterm_pid: int
    iterm_started_at: float
    root_pid: int
    root_started_at: float
    codex_pid: int
    codex_started_at: float
    exe_realpath: str
    exe_device: int
    exe_inode: int
    exe_sha256: str
    exe_size: int
    exe_mtime_ns: int
    exe_ctime_ns: int
    tty_path: str
    tty_rdev: int
    pgid: int
    uid: int

    @property
    def key(self) -> str:
        return digest(as_json(self))


@dataclass(frozen=True)
class Cell:
    text: str = ""
    faint: bool | None = None
    fg: str | None = None
    bg: str | None = None


@dataclass(frozen=True)
class ScreenFrame:
    session_id: str
    connection_epoch: int
    seq: int
    monotonic_at: float
    wall_at: float
    width: int
    height: int
    cursor_x: int
    cursor_y: int
    lines: tuple[str, ...]
    hard_eols: tuple[bool, ...]
    cells: tuple[tuple[Cell, ...], ...]
    absolute_top: int
    first_visible_line: int
    selection_length: int
    variables: dict[str, Any] = field(default_factory=dict)
    activity_seq: int = 0

    @classmethod
    def from_dict(cls, data: dict) -> ScreenFrame:
        values = dict(data)
        values["lines"] = tuple(values["lines"])
        values["hard_eols"] = tuple(values["hard_eols"])
        values["cells"] = tuple(tuple(Cell(**c) for c in row) for row in values["cells"])
        return cls(**values)


@dataclass(frozen=True)
class Observation:
    ui_state: UiState
    composer_state: Composer
    profile_revision: str
    relevant_hash: str
    category: str = ""
    error_signature: str = ""
    retry_after_s: float = 0
    composer_text: str = ""
    composer_row: int = -1
    composer_start_x: int = -1
    busy_evidence: bool = False
    normal_completion: bool = False
    blockers: tuple[str, ...] = ()
    error_row: int = -1
    completion_signature: str = ""

    @property
    def ready(self) -> bool:
        return (self.ui_state == UiState.READY_ERROR
                and self.composer_state in (Composer.EMPTY, Composer.PLACEHOLDER)
                and self.category in RETRYABLE and not self.blockers)


@dataclass
class FailureEvent:
    event_id: str
    generation: int
    category: str
    signature: str
    first_seen_mono: float
    last_seen_mono: float
    retry_after_s: float
    source_seq: int
    identity_hash: str
    schedule_version: int
    deadline: float = 0
    samples: int = 1
    reported: bool = False
    one_shot: bool = False


@dataclass
class RevocationLatch:
    activity_seq: int = 0
    cancel_version: int = 0

    @property
    def token(self) -> tuple[int, int]:
        return self.activity_seq, self.cancel_version

    def cancel(self, *, activity: bool = False) -> None:
        self.cancel_version += 1
        if activity:
            self.activity_seq += 1


@dataclass
class Binding:
    binding_id: str
    session: SessionRef
    identity: ProcessIdentity
    profile_id: str = "codex-local-v1"
    profile_revision: str = "codex-local-v1.1"
    mode: Mode = Mode.OBSERVE
    state: State = State.BASELINING
    generation: int = 0
    schedule_version: int = 0
    connection_epoch: int = 0
    baseline_hash: str = ""
    baseline_samples: int = 0
    previous: Observation | None = None
    previous_frame: ScreenFrame | None = None
    pending: FailureEvent | None = None
    reason: str = ""
    observed_busy: bool = False
    idle_since: float | None = None
    consumed: set[str] = field(default_factory=set)
    latch: RevocationLatch = field(default_factory=RevocationLatch)
    inflight_attempt: str | None = None
    last_resumed_at: float | None = None

    def invalidate(self) -> None:
        self.schedule_version += 1
        self.pending = None
        self.latch.cancel()

    def baseline(self) -> None:
        self.invalidate()
        self.state = State.BASELINING
        self.reason = ""
        self.baseline_hash = ""
        self.baseline_samples = 0
        self.previous = None
        self.previous_frame = None
        self.observed_busy = False
        self.idle_since = None

    def pause(self, state: State, reason: str) -> None:
        self.invalidate()
        self.state = state
        self.reason = reason


PAUSED = frozenset({State.PAUSED_USER, State.PAUSED_BLOCKED,
                    State.PAUSED_UNCONFIRMED, State.TARGET_GONE})
