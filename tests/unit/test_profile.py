from dataclasses import replace

import pytest

from cliretry.models import Cell, Composer, UiState
from cliretry.profiles import CodexProfile
from tests.helpers import CAPACITY, frame


profile = CodexProfile()


@pytest.mark.parametrize("kind,state", [("idle", UiState.IDLE), ("busy", UiState.BUSY),
                                        ("capacity", UiState.READY_ERROR), ("menu", UiState.MENU),
                                        ("quoted", UiState.IDLE)])
def test_layout_classification(kind, state):
    assert profile.classify(frame(kind)).ui_state == state


@pytest.mark.parametrize("text,category", [
    (CAPACITY, "CAPACITY"),
    ("■ exceeded retry limit, last status: 429 Too Many Requests", "RATE_LIMIT_TRANSIENT"),
    ("■ stream disconnected before completion: stream closed before response.completed", "CONNECTION_TRANSIENT"),
    ("■ internal streaming error, please retry", "STREAM_TRANSIENT"),
    ("■ HTTP 503 Service Unavailable", "UPSTREAM_TRANSIENT"),
    ("■ HTTP 503 Service Unavailable: auth_not_found", "AUTH"),
    ("■ insufficient_quota", "QUOTA_OR_BILLING"),
    ("■ You've hit your usage limit", "USAGE_LIMIT"),
    ("■ Context window exhausted", "CONTEXT_LIMIT"),
    ("■ cyberPolicy", "POLICY_OR_APPROVAL"),
    ("■ HTTP 429 mystery failure", "UNKNOWN_ERROR"),
])
def test_error_categories(text, category):
    assert profile.classify(frame("capacity", error=text)).category == category


def test_native_503_capacity_error_continuation():
    f = frame("capacity")
    lines, cells = list(f.lines), list(f.cells)
    head = "■ unexpected status 503 Service Unavailable: Selected model is at capacity."
    continuation = "Please try a different model., url: http://127.0.0.1:49152/v1/responses"
    first = f.cursor_y - 2
    for row, text in ((first, head), (first + 1, continuation)):
        lines[row] = text
        cells[row] = tuple(Cell(ch) for ch in text) + (Cell(" "),) * (f.width - len(text))
    observation = profile.classify(replace(f, lines=tuple(lines), cells=tuple(cells)))
    assert observation.ui_state == UiState.READY_ERROR
    assert observation.category == "CAPACITY"
    assert not observation.blockers


@pytest.mark.parametrize("status,phrase", [(502, "Bad Gateway"), (504, "Gateway Timeout")])
def test_native_upstream_error_with_wrapped_url(status, phrase):
    f = frame("capacity")
    lines, cells = list(f.lines), list(f.cells)
    first = f.cursor_y - 2
    head = f"■ unexpected status {status} {phrase}: {phrase}, url:"
    continuation = "http://127.0.0.1:49152/v1/responses"
    for row, text in ((first, head), (first + 1, continuation)):
        lines[row] = text
        cells[row] = tuple(Cell(ch) for ch in text) + (Cell(" "),) * (f.width - len(text))
    observation = profile.classify(replace(f, lines=tuple(lines), cells=tuple(cells)))
    assert observation.ui_state == UiState.READY_ERROR
    assert observation.category == "UPSTREAM_TRANSIENT"
    assert not observation.blockers


def test_retry_after():
    o = profile.classify(frame("capacity", error="■ exceeded retry limit, last status: 429 Too Many Requests; retry-after: 120 seconds"))
    assert o.retry_after_s == 120


def test_placeholder_requires_style():
    assert profile.classify(frame("capacity", placeholder=True)).composer_state == Composer.PLACEHOLDER
    assert profile.classify(frame("capacity", placeholder=True, style=False)).composer_state == Composer.UNKNOWN
    assert profile.classify(frame("capacity", user_text="Summarize recent commits", cursor_home=True)).composer_state == Composer.USER_TEXT


def test_actual_empty_without_style():
    assert profile.classify(frame("capacity", style=False)).ready


@pytest.mark.parametrize("text", ["unfinished", "继续", " ", "/model"])
def test_user_text_not_empty(text):
    assert not profile.classify(frame("capacity", user_text=text)).ready


def test_unicode_exact_composer_verification():
    assert profile.verify_typed_text(frame("capacity", user_text="继续"), "继续")
    assert not profile.verify_typed_text(frame("capacity", user_text="继续其他"), "继续")
    assert not profile.verify_typed_text(frame("capacity", user_text="继续", cursor_home=True), "继续")


def test_geometry_and_missing_rows():
    assert profile.classify(frame(width=10)).ui_state == UiState.UNKNOWN
    f = frame()
    assert profile.classify(replace(f, cells=())).ui_state == UiState.UNKNOWN


def test_normal_completion():
    assert profile.classify(frame("complete")).normal_completion
    assert not profile.classify(frame("idle")).normal_completion


def test_current_codex_worked_for_completion_marker():
    f = frame("complete")
    lines, cells = list(f.lines), list(f.cells)
    row = f.cursor_y - 4
    marker = "Worked for 2s • 15:36"
    lines[row] = marker
    cells[row] = tuple(Cell(ch) for ch in marker) + (Cell(" "),) * (f.width - len(marker))
    assert profile.classify(replace(f, lines=tuple(lines), cells=tuple(cells))).normal_completion
