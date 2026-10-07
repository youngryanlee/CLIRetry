import os
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest

from cliretry.adapters.process_macos import MacProcessProbe
from cliretry.models import RetryError


@pytest.fixture
def probe_fixture(tmp_path, monkeypatch):
    from cliretry.adapters import process_macos as module
    executable = tmp_path / "codex"
    executable.write_bytes(b"synthetic native identity fixture")
    executable.chmod(0o700)
    times = {1: 1., 2: 2., 3: 3.}

    class Process:
        def __init__(self, pid):
            self.pid = pid

        def create_time(self):
            return times[self.pid]

        def exe(self):
            return str(executable)

        def terminal(self):
            return "/dev/ttys999"

        def uids(self):
            return SimpleNamespace(real=os.getuid())

        def status(self):
            return "running"

        def parents(self):
            return [Process(2), Process(1)]

    monkeypatch.setattr(module.sys, "platform", "darwin")
    monkeypatch.setattr(module.psutil, "Process", Process)
    monkeypatch.setattr(module.psutil, "pid_exists", lambda _: True)
    old_lstat = os.lstat
    monkeypatch.setattr(module.os, "lstat", lambda path, *a, **kw:
                        SimpleNamespace(st_mode=stat.S_IFCHR | 0o600, st_uid=os.getuid(), st_rdev=7)
                        if str(path) == "/dev/ttys999" else old_lstat(path, *a, **kw))
    old_open = os.open
    monkeypatch.setattr(module.os, "open", lambda path, *a, **kw:
                        old_open(os.devnull, os.O_RDONLY) if str(path) == "/dev/ttys999" else old_open(path, *a, **kw))
    monkeypatch.setattr(module.os, "tcgetpgrp", lambda fd: 3)
    monkeypatch.setattr(module.os, "getpgid", lambda pid: 3)
    variables = {"jobPid": 3, "pid": 2, "iterm2.pid": 1, "tty": "/dev/ttys999"}
    return MacProcessProbe(), variables, (str(executable),), times


def test_identity_full_roundtrip(probe_fixture):
    probe, variables, paths, _ = probe_fixture
    original = probe.read(variables, paths)
    assert probe.read(variables, paths, original) == original


def test_pid_reuse_rejected(probe_fixture):
    probe, variables, paths, times = probe_fixture
    original = probe.read(variables, paths)
    times[3] += 1
    with pytest.raises(RetryError, match="IDENTITY_CHANGED"):
        probe.read(variables, paths, original)


def test_binary_replacement_rejected(probe_fixture):
    probe, variables, paths, _ = probe_fixture
    original = probe.read(variables, paths)
    Path(paths[0]).write_bytes(b"different executable content")
    with pytest.raises(RetryError, match="IDENTITY_CHANGED"):
        probe.read(variables, paths, original)


@pytest.mark.parametrize("variables", [{"jobPid": 99}, {"tty": "/dev/null"},
                                      {"tmuxRole": "client"}, {"sshIntegrationLevel": 1}])
def test_changed_frontend_rejected(probe_fixture, variables):
    probe, base, paths, _ = probe_fixture
    original = probe.read(base, paths)
    with pytest.raises(RetryError):
        probe.read(base | variables, paths, original)


def test_no_executable_allowlist_rejected(probe_fixture):
    probe, variables, _, _ = probe_fixture
    with pytest.raises(RetryError, match="Explicit native"):
        probe.read(variables, ())


def test_liveness_does_not_require_codex_to_own_foreground(probe_fixture):
    probe, variables, paths, times = probe_fixture
    original = probe.read(variables, paths)
    probe.check_liveness(original)
    times[3] += 1
    with pytest.raises(RetryError, match="IDENTITY_CHANGED"):
        probe.check_liveness(original)


def test_darwin_noncontrolling_terminal_uses_kernel_snapshot(probe_fixture, monkeypatch):
    import errno
    from cliretry.adapters import process_macos as module
    probe, variables, paths, _ = probe_fixture

    def enotty(fd):
        raise OSError(errno.ENOTTY, "not controlling tty")

    monkeypatch.setattr(module.os, "tcgetpgrp", enotty)
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **kw:
                        SimpleNamespace(returncode=0, stdout="3 3 3 ttys999\n"))
    assert probe.read(variables, paths).pgid == 3
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **kw:
                        SimpleNamespace(returncode=0, stdout="3 3 9 ttys999\n"))
    with pytest.raises(RetryError, match="FOREGROUND_UNVERIFIABLE"):
        probe.read(variables, paths)


@pytest.mark.parametrize("row", ["3 3 -1 ttys999", "3 3 3 ttys000", "9 3 3 ttys999", "", "bad output"])
def test_kernel_snapshot_must_match_target(probe_fixture, monkeypatch, row):
    from cliretry.adapters import process_macos as module
    probe, _, _, _ = probe_fixture
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **kw:
                        SimpleNamespace(returncode=0, stdout=row))
    with pytest.raises(RetryError, match="snapshot"):
        probe._ps_foreground_pgid(3, "/dev/ttys999")
