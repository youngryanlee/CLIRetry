from __future__ import annotations

import hashlib
import errno
import os
from pathlib import Path
import re
import stat
import sys
import subprocess

import psutil

from ..models import ProcessIdentity, RetryError


class MacProcessProbe:
    @staticmethod
    def _ps_foreground_pgid(pid, tty):
        # Darwin rejects TIOCGPGRP for a terminal that is not the caller's
        # controlling terminal (ENOTTY). /bin/ps reads the kernel's tpgid via
        # sysctl instead; require exact PID, TTY and a positive foreground group.
        try:
            result = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "pid=,pgid=,tpgid=,tty="],
                                    stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                    timeout=.75, env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
            fields = result.stdout.split()
            if (result.returncode or len(fields) != 4 or int(fields[0]) != pid
                    or fields[3] != tty.removeprefix("/dev/")
                    or int(fields[1]) != os.getpgid(pid) or int(fields[2]) <= 0):
                raise RetryError("FOREGROUND_UNVERIFIABLE", "Kernel process group snapshot did not match")
            return int(fields[2])
        except (subprocess.SubprocessError, OSError, ValueError) as exc:
            raise RetryError("FOREGROUND_UNVERIFIABLE", "Kernel process group snapshot unavailable") from exc

    def check_liveness(self, expected: ProcessIdentity) -> None:
        """Check bound processes independently of which child owns the foreground."""
        try:
            for pid, started in ((expected.iterm_pid, expected.iterm_started_at),
                                 (expected.root_pid, expected.root_started_at),
                                 (expected.codex_pid, expected.codex_started_at)):
                process = psutil.Process(pid)
                if process.status() in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD):
                    raise RetryError("TARGET_GONE")
                if process.create_time() != started or process.uids().real != expected.uid:
                    raise RetryError("IDENTITY_CHANGED")
        except psutil.NoSuchProcess as exc:
            raise RetryError("TARGET_GONE") from exc
        except psutil.Error as exc:
            raise RetryError("FOREGROUND_UNVERIFIABLE", type(exc).__name__) from exc

    def read(self, variables: dict, allowed_paths: tuple[str, ...],
             expected: ProcessIdentity | None = None) -> ProcessIdentity:
        if sys.platform != "darwin":
            raise RetryError("UNSUPPORTED_PLATFORM", "macOS required", exit_code=6)
        try:
            if variables.get("tmuxRole") or variables.get("sshIntegrationLevel") not in (None, "", 0, "0"):
                raise RetryError("UNSUPPORTED_PROCESS", "SSH and tmux are not supported")
            pid = int(variables.get("jobPid") or 0)
            if expected and pid != expected.codex_pid:
                if not psutil.pid_exists(expected.codex_pid):
                    raise RetryError("TARGET_GONE")
                raise RetryError("FOREGROUND_UNVERIFIABLE")
            codex = psutil.Process(pid)
            exe = str(Path(codex.exe()).resolve(strict=True))
            approved = {str(Path(p).resolve(strict=True)) for p in allowed_paths}
            if exe not in approved or Path(exe).name != "codex":
                raise RetryError("UNSUPPORTED_PROCESS", "Explicit native Codex executable path required")
            if codex.status() in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD) or codex.uids().real != os.getuid():
                raise RetryError("IDENTITY_CHANGED")
            tty = str(variables.get("tty") or "")
            if not re.fullmatch(r"/dev/ttys[0-9A-Za-z]+", tty):
                raise RetryError("FOREGROUND_UNVERIFIABLE", "Unsupported TTY")
            st = os.lstat(tty)
            if not stat.S_ISCHR(st.st_mode) or st.st_uid != os.getuid() or codex.terminal() != tty:
                raise RetryError("FOREGROUND_UNVERIFIABLE")
            fd = os.open(tty, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK | os.O_NOFOLLOW)
            try:
                try:
                    foreground = os.tcgetpgrp(fd)
                except OSError as exc:
                    if exc.errno != errno.ENOTTY:
                        raise
                    foreground = self._ps_foreground_pgid(pid, tty)
            finally:
                os.close(fd)
            pgid = os.getpgid(pid)
            if foreground != pgid:
                raise RetryError("FOREGROUND_UNVERIFIABLE")
            root = psutil.Process(int(variables.get("pid") or 0))
            iterm = psutil.Process(int(variables.get("iterm2.pid") or 0))
            if root.pid not in {p.pid for p in codex.parents()} and root.pid != codex.pid:
                raise RetryError("IDENTITY_CHANGED", "Codex is not a descendant of the session root")
            info = os.stat(exe)
            if expected:
                exe_hash = expected.exe_sha256
            else:
                with open(exe, "rb") as stream:
                    exe_hash = hashlib.file_digest(stream, "sha256").hexdigest()
            identity = ProcessIdentity(iterm.pid, iterm.create_time(), root.pid, root.create_time(),
                                       pid, codex.create_time(), exe, info.st_dev, info.st_ino,
                                       exe_hash, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
                                       tty, st.st_rdev, pgid, codex.uids().real)
            if expected and identity != expected:
                raise RetryError("IDENTITY_CHANGED")
            return identity
        except RetryError:
            raise
        except psutil.NoSuchProcess as exc:
            raise RetryError("TARGET_GONE") from exc
        except (psutil.Error, OSError, ValueError, TypeError) as exc:
            raise RetryError("FOREGROUND_UNVERIFIABLE", f"{type(exc).__name__}: {exc}") from exc
