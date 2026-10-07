from dataclasses import replace
import unicodedata

from cliretry.models import Binding, Cell, ProcessIdentity, ScreenFrame, SessionRef
from cliretry.profiles import CodexProfile


CAPACITY = "⚠ Selected model is at capacity. Please try a different model."
BUSY = "• Working (1s • esc to interrupt)"


def identity():
    return ProcessIdentity(1, 100, 2, 101, 3, 102, "/test/codex", 1, 2, "fixture-sha256",
                           10, 10, 10, "/dev/ttys999", 1, 3, 501)


def binding():
    return Binding("test-binding", SessionRef("test-session"), identity(), connection_epoch=1)


def frame(kind="idle", *, at=0, seq=None, user_text="", placeholder=False,
          style=True, cursor_home=False, error=CAPACITY, width=100, height=24):
    lines = [""] * height
    prompt_y = height - 4
    if kind == "capacity":
        lines[prompt_y - 2] = error
    elif kind == "busy":
        lines[prompt_y - 2] = BUSY
    elif kind == "complete":
        lines[prompt_y - 4] = "─ Worked for 1m ─"
        lines[prompt_y - 2] = "• Done."
    elif kind == "quoted":
        lines[prompt_y - 2] = "    " + error
    elif kind == "menu":
        lines[prompt_y - 2] = "Would you like to allow this command?"
    text = "Summarize recent commits" if placeholder else user_text
    lines[prompt_y] = "› " + text
    lines[prompt_y + 2] = "? for shortcuts"
    cells = []
    for y, line in enumerate(lines):
        row = []
        for ch in line:
            faint = (placeholder and y == prompt_y and len(row) >= 2) if style else None
            row.append(Cell(ch, faint))
            if unicodedata.east_asian_width(ch) in {"W", "F"}:
                row.append(Cell("", faint))
        row.extend(Cell(" ", None) for _ in range(max(0, width - len(row))))
        cells.append(tuple(row))
    text_width = sum(2 if unicodedata.east_asian_width(ch) in {"W", "F"} else 1 for ch in text)
    x = 2 if placeholder or cursor_home else 2 + text_width
    return ScreenFrame("test-session", 1, int(at * 10) if seq is None else seq,
                       at, 1000 + at, width, height, x, prompt_y, tuple(lines),
                       (True,) * height, tuple(cells), 0, 0, 0, {}, 0)


class FakeClock:
    def __init__(self, at=100):
        self.at = at

    def monotonic(self):
        return self.at

    def wall(self):
        return 1000 + self.at

    async def sleep(self, seconds):
        import asyncio
        self.at += seconds
        await asyncio.sleep(0)


class FakeAdapter:
    async def check_liveness(self, expected):
        return None

    def __init__(self, clock, *, after_submit="busy", fail_at=None, user_edit=False):
        self.clock = clock
        self.profile = CodexProfile()
        self.connected = True
        self.epoch = 1
        self.calls = []
        self.phase = "initial"
        self.after_submit = after_submit
        self.fail_at = fail_at
        self.user_edit = user_edit
        self.revocation = None

    async def identity(self, session_id, paths, expected=None):
        if self.fail_at == "identity":
            from cliretry.models import RetryError
            raise RetryError("TARGET_GONE")
        return expected or identity()

    async def capture(self, session_id):
        if self.revocation:
            fn, self.revocation = self.revocation, None
            fn()
        kind = self.after_submit if self.phase == "submitted" else "capacity"
        text = ("user edit" if self.user_edit else "继续") if self.phase == "typed" else ""
        return replace(frame(kind, at=self.clock.monotonic(), user_text=text), session_id=session_id)

    def automatic_ready(self, b):
        return True

    async def guarded_type(self, b, event, text, latch_token):
        self.calls.append(text)
        self.phase = "typed"
        if self.fail_at == "type":
            raise OSError("reply lost after typing")

    async def guarded_submit(self, b, event, text, latch_token):
        self.calls.append("\r")
        self.phase = "submitted"
        if self.fail_at == "submit":
            raise OSError("reply lost after Enter")
