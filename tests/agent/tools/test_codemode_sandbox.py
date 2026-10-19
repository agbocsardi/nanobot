"""Deterministic Bubblewrap selection, active-policy validation, and setup diagnostics."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nanobot.agent.tools import _codemode_sandbox as sandbox
from nanobot.agent.tools.codemode import CodemodeConfig
from nanobot.config.schema import ToolsConfig

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux sandbox validation")


@pytest.fixture
def executable(tmp_path, monkeypatch):
    path = tmp_path / "bwrap"
    path.write_text("#!/bin/sh\nexit 1\n")
    path.chmod(0o755)
    monkeypatch.setattr(sandbox, "_probe_bwrap", AsyncMock(return_value=(
        "bubblewrap 0.11.2", "nanobot-bwrap (unconfined)",
    )))
    monkeypatch.setattr(sandbox, "_apparmor_userns_restricted", lambda: True)
    monkeypatch.setattr(sandbox, "_export_seccomp", lambda _fd: None)
    return path


def test_config_defaults_to_system_binary_and_accepts_absolute_override(executable):
    assert CodemodeConfig().bwrap_path == "/usr/bin/bwrap"
    cfg = ToolsConfig.model_validate({"codemode": {"bwrapPath": str(executable)}})
    assert cfg.codemode.bwrap_path == str(executable)


async def test_absolute_selection_ignores_path_and_resolves_symlinks(executable, monkeypatch):
    wrong = executable.parent / "path-shadow"
    wrong.mkdir()
    (wrong / "bwrap").write_text("not the selected binary")
    monkeypatch.setenv("PATH", str(wrong))
    link = executable.parent / "bwrap-link"
    link.symlink_to(executable)
    selected = await sandbox.validate_bwrap(str(link))
    assert selected.path == executable
    assert selected.version == "0.11.2"
    sandbox._probe_bwrap.assert_awaited_once_with(executable)
    with sandbox.sandbox_command(selected) as (argv, _):
        assert argv[0] == str(executable)


async def test_default_binary_is_not_shadowed_by_path(executable, monkeypatch):
    if not Path(sandbox.DEFAULT_BWRAP_PATH).is_file():
        pytest.skip("System Bubblewrap not installed")
    monkeypatch.setenv("PATH", str(executable.parent))
    selected = await sandbox.validate_bwrap(CodemodeConfig().bwrap_path)
    assert selected.path == Path("/usr/bin/bwrap").resolve()
    sandbox._probe_bwrap.assert_awaited_once_with(selected.path)


@pytest.mark.parametrize("path, message", [
    ("bwrap", "absolute executable path"),
    ("", "absolute executable path"),
    ("/tmp/bwrap\0", "absolute executable path"),
    ("/nonexistent/nanobot-bwrap", "is unavailable"),
])
async def test_invalid_paths_fail_before_running_a_probe(executable, path, message):
    with pytest.raises(RuntimeError, match=message):
        await sandbox.validate_bwrap(path)
    sandbox._probe_bwrap.assert_not_awaited()


async def test_directory_and_nonexecutable_file_are_rejected(executable):
    with pytest.raises(RuntimeError, match="not an executable file"):
        await sandbox.validate_bwrap(str(executable.parent))
    executable.chmod(0o644)
    with pytest.raises(RuntimeError, match="not an executable file"):
        await sandbox.validate_bwrap(str(executable))
    sandbox._probe_bwrap.assert_not_awaited()


@pytest.mark.parametrize("version", ["bubblewrap 0.9.0", "bubblewrap 0.10.0", "bubblewrap 0.11.2"])
async def test_supported_versions_are_accepted(executable, version):
    sandbox._probe_bwrap.return_value = (version, "nanobot-bwrap (unconfined)")
    assert (await sandbox.validate_bwrap(str(executable))).version == version.split()[1]


@pytest.mark.parametrize("version, message", [
    ("bubblewrap 0.8.0", "Unsupported Bubblewrap 0.8.0"),
    ("some other executable", "Unexpected --version"),
])
async def test_wrong_version_or_program_names_the_selected_file(executable, version, message):
    sandbox._probe_bwrap.return_value = (version, "nanobot-bwrap (unconfined)")
    with pytest.raises(RuntimeError, match=message) as error:
        await sandbox.validate_bwrap(str(executable))
    assert str(executable) in str(error.value)
    assert "0.9.0" in str(error.value)


@pytest.mark.parametrize("label", ["unconfined", "unprivileged_userns (enforce)"])
async def test_restricted_unprofiled_binary_is_rejected_with_actual_cause(executable, label):
    sandbox._probe_bwrap.return_value = ("bubblewrap 0.11.2", label)
    with pytest.raises(RuntimeError, match="no user-namespace-authorized AppArmor profile") as error:
        await sandbox.validate_bwrap(str(executable))
    message = str(error.value)
    assert str(executable) in message and "0.11.2" in message and label in message
    assert "apparmor_restrict_unprivileged_userns=1" in message
    assert "profile for another path does not cover it" in message


@pytest.mark.parametrize("label", ["unavailable (Permission denied)", ""])
async def test_unknown_active_profile_fails_closed_on_restricted_host(executable, label):
    sandbox._probe_bwrap.return_value = ("bubblewrap 0.11.2", label)
    with pytest.raises(RuntimeError, match="cannot validate the active AppArmor profile"):
        await sandbox.validate_bwrap(str(executable))


async def test_unconfined_is_valid_when_apparmor_does_not_restrict_userns(executable, monkeypatch):
    sandbox._probe_bwrap.return_value = ("bubblewrap 0.11.2", "unconfined")
    monkeypatch.setattr(sandbox, "_apparmor_userns_restricted", lambda: False)
    assert (await sandbox.validate_bwrap(str(executable))).apparmor_label == "unconfined"


def test_modern_setup_capabilities_are_explicit_and_network_stays_private(executable):
    binary = sandbox.BwrapBinary(executable, "0.11.2", "nanobot-bwrap (unconfined)")
    with sandbox.sandbox_command(binary) as (argv, _):
        assert "--unshare-net" in argv and "--share-net" not in argv
        assert argv[argv.index("--cap-drop") + 1] == "ALL"
        added = [argv[index + 1] for index, value in enumerate(argv) if value == "--cap-add"]
        assert added == ["CAP_NET_ADMIN", "CAP_SETPCAP"]


@pytest.mark.parametrize("returncode, stderr, expected", [
    (1, "bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted", "despite requested CAP_NET_ADMIN"),
    (1, "bwrap: creating new namespace: Permission denied", "Namespace creation/mapping was denied"),
    (1, "bwrap: Unknown option --size", "does not support a required sandbox option"),
    (-9, "", "signal SIGKILL"),
])
def test_setup_errors_identify_binary_version_profile_and_stage(executable, returncode, stderr, expected):
    error = str(sandbox.worker_exit_error(
        sandbox.BwrapBinary(executable, "0.11.2", "restricted-profile (enforce)"), returncode, stderr,
    ))
    assert str(executable) in error and "0.11.2" in error and "restricted-profile" in error
    assert expected in error and "No unsandboxed fallback" in error
    if returncode == -9:
        assert "not evidence of an AppArmor" in error


async def test_cancelled_version_probe_closes_pipes_and_reaps_process(monkeypatch):
    fds = os.pipe()
    inherited_read_fd = os.dup(fds[0])  # Stand in for the child's inherited read end.
    monkeypatch.setattr(sandbox.os, "pipe", lambda: fds)
    started = asyncio.Event()

    async def hang():
        started.set()
        await asyncio.Future()

    proc = SimpleNamespace(
        pid=123, returncode=None, stdout=SimpleNamespace(readline=hang),
        stderr=SimpleNamespace(read=AsyncMock(return_value=b"")), wait=AsyncMock(),
    )

    def kill():
        proc.returncode = -9

    proc.kill = kill
    monkeypatch.setattr(sandbox.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    inspected = []

    def read_label(path, **kwargs):
        inspected.append(str(path))
        return "nanobot-bwrap (unconfined)"

    monkeypatch.setattr(Path, "read_text", read_label)
    task = asyncio.create_task(sandbox._probe_bwrap(Path("/usr/bin/bwrap")))
    try:
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert proc.returncode == -9
        assert inspected == ["/proc/123/attr/apparmor/current"]
        proc.wait.assert_awaited_once()
        for fd in fds:
            with pytest.raises(OSError):
                os.fstat(fd)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        os.close(inherited_read_fd)
