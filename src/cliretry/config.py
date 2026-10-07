from __future__ import annotations

import dataclasses
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
import tomllib
from typing import Any

from .models import RETRYABLE, RetryError


@dataclass(frozen=True)
class DaemonConfig:
    max_sessions: int = 4
    poll_interval_s: float = 2
    api_timeout_s: float = 2
    process_check_timeout_s: float = 1
    state_dir: str = "~/Library/Application Support/CLIRetry"
    socket_path: str = ""


@dataclass(frozen=True)
class RecoveryConfig:
    prompt: str = "继续"
    stable_error_s: float = 6
    builtin_retry_grace_s: float = 10
    backoff_base_s: float = 30
    backoff_multiplier: float = 2
    backoff_cap_s: float = 900
    jitter_fraction: float = .2
    global_min_attempt_gap_s: float = 15
    max_attempts_per_chain: int = 12
    max_chain_elapsed_s: float = 7200
    max_attempts_per_session_hour: int = 30
    max_attempts_per_run: int = 100
    max_enabled_duration_s: float = 43200
    text_min_settle_s: float = .5
    text_confirm_timeout_s: float = 5
    recovery_ack_timeout_s: float = 30
    sleep_gap_threshold_s: float = 15


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"
    max_bytes: int = 5242880
    backup_count: int = 5
    capture_screen: bool = False


@dataclass(frozen=True)
class CustomRule:
    id: str
    category: str
    pattern: str


def validate_custom_rule(raw):
    if not isinstance(raw, dict) or set(raw) != {"id", "category", "pattern"}:
        _error("custom_rules", "each rule requires exactly id, category, pattern")
    if not all(isinstance(value, str) for value in raw.values()):
        _error("custom_rules", "fields must be strings")
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", raw["id"]) or raw["category"] not in RETRYABLE:
        _error("custom_rules", "invalid ID or non-transient category")
    pattern = raw["pattern"]
    if not 3 <= len(pattern) <= 256 or not pattern.startswith("^") or not pattern.endswith("$"):
        _error("custom_rules.pattern", "must be a 3–256 character anchored pattern")
    # Deliberately bounded regex subset: literal text plus at most one .* or .+.
    # Reject groups, alternatives, backreferences and nested/repeated quantifiers.
    body, i, wildcards = pattern[1:-1], 0, 0
    while i < len(body):
        ch = body[i]
        if ord(ch) < 32 or 127 <= ord(ch) <= 159:
            _error("custom_rules.pattern", "control character")
        if ch == "\\":
            i += 1
            if i >= len(body) or body[i] not in r"\.^$*+?{}[]()|":
                _error("custom_rules.pattern", "only escaped literal punctuation is supported")
        elif ch == ".":
            if i + 1 >= len(body) or body[i + 1] not in "*+":
                _error("custom_rules.pattern", "escape a literal dot or use one .* / .+")
            wildcards += 1
            i += 1
        elif ch in "*+?{}[]()|^$":
            _error("custom_rules.pattern", "unsupported regex operator")
        i += 1
    if wildcards > 1:
        _error("custom_rules.pattern", "at most one wildcard allowed")
    return CustomRule(**raw)


@dataclass(frozen=True)
class ProfileConfig:
    executable_paths: tuple[str, ...] = ()
    enabled_categories: tuple[str, ...] = tuple(sorted(RETRYABLE))
    min_columns: int = 80
    max_columns: int = 240
    min_rows: int = 20
    max_rows: int = 100
    custom_rules: tuple[CustomRule, ...] = ()


@dataclass(frozen=True)
class Config:
    schema_version: int = 1
    daemon: DaemonConfig = field(default_factory=DaemonConfig)
    recovery: RecoveryConfig = field(default_factory=RecoveryConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    profile: ProfileConfig = field(default_factory=ProfileConfig)

    @property
    def state_dir(self) -> Path:
        return Path(self.daemon.state_dir).expanduser().absolute()

    @property
    def socket_path(self) -> Path:
        if self.daemon.socket_path:
            return Path(self.daemon.socket_path).expanduser().absolute()
        return self.state_dir / "control.sock"


def _error(key: str, message: str) -> None:
    raise RetryError("INVALID_CONFIG", f"{key}: {message}", exit_code=2)


def _section(cls, data: dict, name: str):
    if not isinstance(data, dict):
        _error(name, "expected table")
    defaults = cls()
    fields = {f.name for f in dataclasses.fields(cls)}
    for key, value in data.items():
        if key not in fields:
            _error(f"{name}.{key}", "unknown field")
        default = getattr(defaults, key)
        valid = (isinstance(value, (float, int)) and not isinstance(value, bool)
                 if isinstance(default, float) else type(value) is type(default))
        # Numeric dataclass defaults may be written as integral values for float annotations.
        annotation = next(f.type for f in dataclasses.fields(cls) if f.name == key)
        if annotation == "float":
            valid = isinstance(value, (int, float)) and not isinstance(value, bool)
        if isinstance(default, tuple):
            valid = isinstance(value, list) and all(isinstance(x, str) for x in value)
            if key == "custom_rules":
                if not isinstance(value, list) or len(value) > 20:
                    _error(f"{name}.{key}", "expected at most 20 rules")
                value = tuple(validate_custom_rule(rule) for rule in value)
                if len({rule.id for rule in value}) != len(value):
                    _error(f"{name}.{key}", "duplicate rule ID")
                valid = True
            if valid:
                data = dict(data)
                data[key] = tuple(value)
        if not valid:
            _error(f"{name}.{key}", "wrong type")
    result = cls(**data)
    for f in dataclasses.fields(result):
        value = getattr(result, f.name)
        if isinstance(value, (float, int)) and not isinstance(value, bool):
            if not math.isfinite(value) or value < 0:
                _error(f"{name}.{f.name}", "must be finite and nonnegative")
    return result


def load_config(path: str | Path | None = None) -> Config:
    try:
        with open(path, "rb") as stream:
            raw = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        if path is not None:
            raise RetryError("INVALID_CONFIG", str(exc), exit_code=2) from exc
        raw = {}
    except TypeError:
        if path is not None:
            raise
        raw = {}
    return parse_config(raw)


def parse_config(raw: dict[str, Any]) -> Config:
    allowed = {"schema_version", "daemon", "recovery", "logging", "profiles"}
    for key in raw.keys() - allowed:
        _error(key, "unknown field")
    if type(raw.get("schema_version", 1)) is not int or raw.get("schema_version", 1) != 1:
        _error("schema_version", "only version 1 supported")
    profiles = raw.get("profiles", {})
    if not isinstance(profiles, dict) or profiles.keys() - {"codex-local-v1"}:
        _error("profiles", "only codex-local-v1 supported")
    c = Config(daemon=_section(DaemonConfig, raw.get("daemon", {}), "daemon"),
               recovery=_section(RecoveryConfig, raw.get("recovery", {}), "recovery"),
               logging=_section(LoggingConfig, raw.get("logging", {}), "logging"),
               profile=_section(ProfileConfig, profiles.get("codex-local-v1", {}), "profiles.codex-local-v1"))
    r, d, p = c.recovery, c.daemon, c.profile
    for key, low, high in [("max_sessions", 1, 4), ("poll_interval_s", .5, 30)]:
        if not low <= getattr(d, key) <= high:
            _error(f"daemon.{key}", f"must be between {low} and {high}")
    for key in ("api_timeout_s", "process_check_timeout_s"):
        if not 0 < getattr(d, key) <= 10:
            _error(f"daemon.{key}", "must be in (0, 10]")
    if not 1 <= len(r.prompt) <= 256 or r.prompt.startswith("/") or any(
        ord(ch) < 32 or 127 <= ord(ch) <= 159 for ch in r.prompt
    ):
        _error("recovery.prompt", "expected 1–256 plain single-line characters, not a command")
    if not r.prompt.strip() or r.prompt != r.prompt.strip():
        _error("recovery.prompt", "must not be blank or have leading/trailing whitespace")
    for key in ("stable_error_s", "builtin_retry_grace_s", "backoff_base_s",
                "global_min_attempt_gap_s", "max_chain_elapsed_s", "max_attempts_per_run",
                "max_attempts_per_session_hour", "max_enabled_duration_s",
                "text_confirm_timeout_s", "recovery_ack_timeout_s", "sleep_gap_threshold_s"):
        if getattr(r, key) <= 0:
            _error(f"recovery.{key}", "must be positive")
    if not 1 <= r.max_attempts_per_chain <= 100:
        _error("recovery.max_attempts_per_chain", "must be in [1,100]")
    if not 1 <= r.backoff_multiplier <= 4 or not 0 <= r.jitter_fraction <= .5:
        _error("recovery", "invalid multiplier or jitter")
    if r.backoff_cap_s < r.backoff_base_s:
        _error("recovery.backoff_cap_s", "must be >= base")
    if r.text_min_settle_s < .5 or r.text_confirm_timeout_s <= r.text_min_settle_s + .25:
        _error("recovery.text_confirm_timeout_s", "must allow two settled frames")
    if c.logging.capture_screen:
        _error("logging.capture_screen", "daemon screen persistence is unsupported; use explicit capture")
    if c.logging.level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        _error("logging.level", "unsupported level")
    if not c.logging.max_bytes or not 1 <= c.logging.backup_count <= 20:
        _error("logging", "invalid rotation limits")
    if not set(p.enabled_categories) <= RETRYABLE:
        _error("profiles.codex-local-v1.enabled_categories", "only transient categories allowed")
    if not 20 <= p.min_columns <= p.max_columns <= 500 or not 5 <= p.min_rows <= p.max_rows <= 200:
        _error("profiles.codex-local-v1", "invalid geometry bounds")
    for value in p.executable_paths:
        candidate = Path(value)
        if not candidate.is_absolute() or not candidate.is_file() or not os.access(candidate, os.X_OK):
            _error("profiles.codex-local-v1.executable_paths", f"not an executable absolute path: {value}")
    for key in ("state_dir", "socket_path"):
        if "\0" in getattr(d, key):
            _error(f"daemon.{key}", "NUL is not allowed")
    if not d.state_dir:
        _error("daemon.state_dir", "must not be empty")
    if len(os.fsencode(c.socket_path)) >= 104:
        raise RetryError("SOCKET_PATH_TOO_LONG", "Set daemon.socket_path to a shorter owned path", exit_code=2)
    return c
