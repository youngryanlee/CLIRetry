"""Strict inline Codex layout parser. Unsupported layouts produce UNKNOWN.

Fixtures and compatibility records, not this parser's existence, determine whether
an installed UI combination is approved for automatic input.
"""
from __future__ import annotations

import re
import datetime

from ..config import ProfileConfig
from ..models import Composer, Observation, ScreenFrame, UiState, digest


REVISION = "codex-local-v1.1"
PROMPT = re.compile(r"^[›»] ")
FOOTER = re.compile(r"^\s*(?:\? for shortcuts|\d+% context left|\d+% context remaining|"
                    r"context left:.*|gpt-[\w.\-]+(?:\s.*)?|.*·\s*\d+% context left|"
                    r"[a-z0-9][\w.\-]*\s+(?:minimal|low|medium|high|xhigh|max|ultra)\s+·\s+.+)\s*$", re.I)
SEPARATOR = re.compile(r"^[─━╌\-]{3,}(?:.*[─━╌\-]{2,})?$")
COMPLETION_MARKER = re.compile(
    r"^\s*(?:─+\s*)?Worked for\s+\d+(?:\.\d+)?[smhd]"
    r"(?:\s+\d+(?:\.\d+)?[smhd]){0,3}(?:\s+•\s+\d{1,2}:\d{2})?\s*(?:─+)?\s*$",
    re.I,
)
MENU = re.compile(r"(?:Would you like to|Do you want to|Allow this|Approve|Select a model|"
                  r"Additional safety checks|Keep waiting|Trust this|Sign in|Log in|"
                  r"Press enter to confirm|esc to cancel|tab to select)", re.I)
BUSY = re.compile(r"^[•◦●]\s+.+\besc to interrupt\b.*$", re.I)
RECONNECT = re.compile(r"^(?:[•◦●]\s+)?Reconnecting\.\.\.\s*\d+/\d+.*$", re.I)
ERROR_HEAD = re.compile(r"^[■⚠]\s+(.+)$")
BLOCKING = [
    ("AUTH", re.compile(r"auth_not_found|unauthorized|authentication (?:failed|required)|"
                        r"invalid api key|\b401\b|没有可用账号|无可用账号|please (?:log|sign) in", re.I)),
    ("QUOTA_OR_BILLING", re.compile(r"insufficient_quota|credit.{0,30}exhaust|billing|"
                                   r"余额不足|配额耗尽", re.I)),
    ("USAGE_LIMIT", re.compile(r"usage limit|usage-limit|you.ve hit your limit|"
                              r"quota.{0,20}reset|额度.{0,20}(?:用尽|耗尽)", re.I)),
    ("POLICY_OR_APPROVAL", re.compile(r"Trusted Access|cyberPolicy|cybersecurity risk|"
                                      r"content (?:was flagged|can.t be shown)|policy violation", re.I)),
    ("CONTEXT_LIMIT", re.compile(r"context window|context.{0,15}(?:exceed|exhaust)|"
                                 r"compact.{0,20}fail|上下文.{0,10}(?:用尽|耗尽)", re.I)),
]
TRANSIENT = [
    ("CAPACITY", re.compile(r"(?:unexpected status 503 Service Unavailable: )?"
                            r"Selected model is at capacity\. Please try a different model\.", re.I)),
    ("RATE_LIMIT_TRANSIENT", re.compile(r"exceeded retry limit, last status: 429 Too Many Requests(?:.*)?", re.I)),
    ("CONNECTION_TRANSIENT", re.compile(r"(?:stream disconnected before completion: .+|"
                                       r"(?:connection (?:failed|reset|timed out)|request timed out)(?:[.: ].*)?)", re.I)),
    ("STREAM_TRANSIENT", re.compile(r"internal streaming error, please retry\.?", re.I)),
    ("UPSTREAM_TRANSIENT", re.compile(r"(?:Our servers are currently overloaded\. Please try again later\.|"
                                     r"(?:exceeded retry limit, last status: |HTTP |unexpected status )?(?:502 Bad Gateway|"
                                     r"503 Service Unavailable|504 Gateway Timeout)(?:[.: ].*)?)", re.I)),
]


class CodexProfile:
    profile_id = "codex-local-v1"
    revision = REVISION

    def __init__(self, config: ProfileConfig | None = None):
        self.config = config or ProfileConfig()
        self.custom_rules = [(rule.category, re.compile(rule.pattern)) for rule in self.config.custom_rules]

    def classify(self, f: ScreenFrame) -> Observation:
        relevant = digest([f.lines, f.cursor_x, f.cursor_y,
                           [[c.faint for c in row] for row in f.cells]])

        def unknown(code):
            return Observation(UiState.UNKNOWN, Composer.UNKNOWN, REVISION, relevant,
                               blockers=(code,))

        p = self.config
        if not (p.min_columns <= f.width <= p.max_columns and p.min_rows <= f.height <= p.max_rows):
            return unknown("GEOMETRY_UNSUPPORTED")
        if len(f.lines) != f.height or len(f.hard_eols) != f.height or len(f.cells) != f.height:
            return unknown("SNAPSHOT_INVALID")
        if not 0 <= f.cursor_y < f.height or not 0 <= f.cursor_x < f.width:
            return unknown("SNAPSHOT_INVALID")

        # Locate the composer by cursor AND a bottom footer, never by a quote in history.
        row = f.cursor_y
        while row >= 0 and not PROMPT.match(f.lines[row]):
            if row == 0 or f.hard_eols[row - 1]:
                return unknown("PROFILE_MISMATCH")
            row -= 1
        if row < 0:
            return unknown("PROFILE_MISMATCH")
        end = row
        while end < f.height - 1 and not f.hard_eols[end]:
            end += 1
        if not row <= f.cursor_y <= end:
            return unknown("COMPOSER_UNVERIFIABLE")
        below = [line for line in f.lines[end + 1:] if line.strip()]
        if not below or any(not FOOTER.fullmatch(line) for line in below):
            # Menus can replace the footer; classify only structurally adjacent UI text.
            if any(MENU.search(line) for line in f.lines[max(0, row - 8):]):
                return Observation(UiState.MENU, Composer.UNKNOWN, REVISION, relevant,
                                   blockers=("MENU_PRESENT",))
            return unknown("PROFILE_MISMATCH")

        prompt_cells = f.cells[row]
        if len(prompt_cells) < 2 or prompt_cells[0].text not in {"›", "»"} or prompt_cells[1].text != " ":
            return unknown("COMPOSER_UNVERIFIABLE")
        edit_cells = list(prompt_cells[2:])
        for y in range(row + 1, end + 1):
            edit_cells.extend(f.cells[y])
        raw_text = "".join(c.text for c in edit_cells).rstrip(" ")
        start = f.cursor_y == row and f.cursor_x == 2
        nonblank = [c for c in edit_cells if c.text and not c.text.isspace()]
        if not nonblank and start and end == row:
            composer = Composer.EMPTY
        elif nonblank and start and all(c.faint is True for c in nonblank) and end == row:
            composer = Composer.PLACEHOLDER
        elif nonblank and start and any(c.faint is None for c in nonblank):
            composer = Composer.UNKNOWN
        else:
            composer = Composer.USER_TEXT
        base = dict(profile_revision=REVISION, relevant_hash=relevant,
                    composer_text=raw_text, composer_row=row, composer_start_x=2)
        if composer == Composer.UNKNOWN:
            return Observation(UiState.UNKNOWN, composer, **base, blockers=("COMPOSER_UNVERIFIABLE",))

        # Reconstruct logical lines with a direct physical origin. Only the last UI
        # block above the composer is eligible; no searching the entire transcript.
        logical: list[str] = []
        origins: list[int] = []
        partial = ""
        partial_start = 0
        for y in range(row):
            if not partial:
                partial_start = y
            line = f.lines[y]
            partial += line.rstrip() if f.hard_eols[y] else line
            if f.hard_eols[y]:
                logical.append(partial)
                origins.append(partial_start)
                partial = ""
        if partial:
            return unknown("PROFILE_MISMATCH")
        while logical and (not logical[-1].strip() or SEPARATOR.fullmatch(logical[-1])):
            logical.pop()
            origins.pop()
        tail = logical[-1] if logical else ""
        error_index = len(logical) - 1
        if len(logical) >= 2 and ERROR_HEAD.fullmatch(logical[-2]):
            head = logical[-2]
            if re.fullmatch(r"https?://\S+", tail) and re.search(r"\burl:\s*$", head, re.I):
                # At narrow widths iTerm can put the URL value on its own row.
                tail = head + " " + tail
                error_index -= 1
            elif re.fullmatch(r"url: https?://\S+", tail):
                tail = head + " " + tail
                error_index -= 1
            else:
                # Codex 0.160 renders a long 503 body on a second hard line,
                # followed by the request URL. Join only this known status
                # wrapper and only when a terminal URL proves the continuation
                # belongs to that error block.
                continuation = re.fullmatch(r"(.*?)\s*,?\s*url: https?://\S+", tail)
                head_text = ERROR_HEAD.fullmatch(head)[1]
                if (continuation and continuation[1]
                        and re.match(r"^unexpected status 503 Service Unavailable:", head_text, re.I)):
                    tail = head + " " + continuation[1].strip()
                    error_index -= 1
        completion = [(f.absolute_top + origin, line) for origin, line in zip(origins[-5:], logical[-5:])
                      if COMPLETION_MARKER.fullmatch(line)]
        base["completion_signature"] = digest(completion[-1]) if completion else ""
        if MENU.search(tail):
            return Observation(UiState.MENU, composer, **base, blockers=("MENU_PRESENT",))
        if BUSY.fullmatch(tail) or RECONNECT.fullmatch(tail):
            return Observation(UiState.BUSY, composer, **base, busy_evidence=True)
        match = ERROR_HEAD.fullmatch(tail)
        if match:
            fence = None
            for line in logical[:error_index]:
                marker = re.match(r"^\s*(`{3,}|~{3,})", line)
                if marker:
                    value = marker[1]
                    if fence is None:
                        fence = value
                    elif value[0] == fence[0] and len(value) >= len(fence):
                        fence = None
            if fence:
                return unknown("QUOTED_ERROR")
            text = match[1]
            if len(text) > 4096:
                return unknown("PROFILE_MISMATCH")
            category = next((name for name, pattern in BLOCKING if pattern.search(text)), "")
            if not category:
                category = next((name for name, pattern in TRANSIENT + self.custom_rules if pattern.fullmatch(text)), "UNKNOWN_ERROR")
            wait = re.search(r"(?:retry[- ]after\s*[:=]?\s*|try again in\s+)(\d+)\s*(?:s(?:econds?)?)?\b", text, re.I)
            retry_after = float(wait[1]) if wait else 0
            timestamp = re.search(r"retry[- ]at\s*[:=]?\s*(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:Z|[+-]\d\d:\d\d))", text, re.I)
            if timestamp:
                try:
                    target = datetime.datetime.fromisoformat(timestamp[1].replace("Z", "+00:00"))
                    retry_after = max(retry_after, target.timestamp() - f.wall_at, 0)
                except ValueError:
                    pass
            blockers = () if category in p.enabled_categories else (category,)
            return Observation(UiState.READY_ERROR, composer, **base, category=category,
                               error_signature=digest([category, tail]), retry_after_s=retry_after,
                               blockers=blockers, error_row=origins[error_index])

        # Do not recognize historical BUSY/errors above a later ordinary response.
        # Normal completion is deliberately stronger than "no error is visible".
        complete = bool(completion)
        return Observation(UiState.IDLE, composer, **base, normal_completion=complete)

    def submission_markers(self, f: ScreenFrame, text: str) -> set[int]:
        """Absolute starts of exact historical user submissions, excluding composer."""
        obs = self.classify(f)
        if obs.composer_row < 0:
            return set()
        result = set()
        partial, start = "", 0
        for y in range(obs.composer_row):
            if not partial:
                start = y
            partial += f.lines[y].rstrip() if f.hard_eols[y] else f.lines[y]
            if f.hard_eols[y]:
                if partial in {f"› {text}", f"» {text}"}:
                    result.add(f.absolute_top + start)
                partial = ""
        return result

    def verify_typed_text(self, f: ScreenFrame, text: str) -> bool:
        o = self.classify(f)
        if o.composer_state != Composer.USER_TEXT or o.composer_text != text:
            return False
        # Cell widths, not Python len, determine the cursor position.
        y = o.composer_row
        x = 2
        assembled = ""
        while y < f.height:
            row = f.cells[y]
            for index in range(x, len(row)):
                cell = row[index]
                if cell.text:
                    assembled += cell.text
                if assembled == text:
                    endpoint = index + 1
                    # A full-width glyph has an empty continuation cell.
                    if endpoint < f.width and endpoint < len(row) and row[endpoint].text == "":
                        endpoint += 1
                    return f.cursor_y == y and f.cursor_x == endpoint
                if not text.startswith(assembled):
                    return False
            if f.hard_eols[y]:
                return False
            y, x = y + 1, 0
        return False
