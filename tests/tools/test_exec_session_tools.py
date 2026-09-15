from __future__ import annotations

import asyncio
import re
import shlex
import subprocess
import sys

from nanobot.agent.tools.action_receipts import ActionReceiptStore
from nanobot.agent.tools.exec_session import (
    ExecSessionManager,
    ListExecSessionsTool,
    WriteStdinTool,
)
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.shell import ExecTool


def _python_command(code: str) -> str:
    if sys.platform == "win32":
        return f"{subprocess.list2cmdline([sys.executable])} -u -c {subprocess.list2cmdline([code])}"
    return f"{shlex.quote(sys.executable)} -u -c {shlex.quote(code)}"


def _session_id(output: str) -> str:
    match = re.search(r"session_id:\s*([0-9a-f]+)", output)
    assert match, output
    return match.group(1)


def test_exec_keeps_one_shot_behavior_without_yield_time_ms(tmp_path):
    async def run() -> str:
        tool = ExecTool(working_dir=str(tmp_path), timeout=5)
        return await tool.execute(command="echo hello")

    result = asyncio.run(run())

    assert "hello" in result
    assert "Exit code: 0" in result
    assert "session_id:" not in result


def test_exec_accepts_command_aliases(tmp_path):
    async def run() -> str:
        tool = ExecTool(working_dir="/")
        return await tool.execute(
            cmd=_python_command("import os; print(os.getcwd())"),
            workdir=str(tmp_path),
        )

    result = asyncio.run(run())

    assert str(tmp_path) in result
    assert "Exit code: 0" in result


def test_exec_returns_completed_session_output_when_yield_time_ms_is_used(tmp_path):
    async def run() -> str:
        manager = ExecSessionManager()
        tool = ExecTool(working_dir=str(tmp_path), timeout=5, session_manager=manager)
        stdin_tool = WriteStdinTool(manager=manager)

        result = await tool.execute(command="echo hello", yield_time_ms=1000)
        if "session_id:" in result:
            sid = _session_id(result)
            result += "\n" + await stdin_tool.execute(
                session_id=sid,
                chars="",
                yield_time_ms=1000,
            )
        return result

    result = asyncio.run(run())

    assert "hello" in result
    assert "Exit code: 0" in result
    assert "session_id:" not in result


def test_exec_session_accepts_max_output_tokens_alias(tmp_path):
    async def run() -> str:
        manager = ExecSessionManager()
        tool = ExecTool(working_dir=str(tmp_path), timeout=5, session_manager=manager)
        command = _python_command("print('A' * 2000)")
        return await tool.execute(
            command=command,
            yield_time_ms=1000,
            max_output_tokens=1000,
        )

    result = asyncio.run(run())

    assert "chars truncated" in result
    assert "Exit code: 0" in result


def test_exec_one_shot_accepts_max_output_tokens_alias(tmp_path):
    async def run() -> str:
        tool = ExecTool(working_dir=str(tmp_path), timeout=5)
        command = _python_command("print('A' * 2000)")
        return await tool.execute(command=command, max_output_tokens=1000)

    result = asyncio.run(run())

    assert "chars truncated" in result
    assert "Exit code: 0" in result


def test_exec_accepts_supported_shell_parameter(tmp_path):
    async def run() -> str:
        tool = ExecTool(working_dir=str(tmp_path), timeout=5)
        return await tool.execute(command="echo shell-ok", shell="sh", login=False)

    if sys.platform == "win32":
        return
    result = asyncio.run(run())

    assert "shell-ok" in result
    assert "Exit code: 0" in result


def test_exec_rejects_unsupported_shell(tmp_path):
    async def run() -> str:
        tool = ExecTool(working_dir=str(tmp_path), timeout=5)
        return await tool.execute(command="echo no", shell="python")

    if sys.platform == "win32":
        return
    result = asyncio.run(run())

    assert "unsupported shell" in result


def test_exec_can_continue_with_stdin(tmp_path):
    async def run() -> tuple[str, str]:
        manager = ExecSessionManager()
        exec_tool = ExecTool(working_dir=str(tmp_path), timeout=5, session_manager=manager)
        stdin_tool = WriteStdinTool(manager=manager)
        command = _python_command(
            "import sys; print('ready', flush=True); "
            "line=sys.stdin.readline(); print('got:' + line.strip(), flush=True)"
        )

        initial = await exec_tool.execute(command=command, yield_time_ms=500)
        sid = _session_id(initial)
        result = await stdin_tool.execute(session_id=sid, chars="ping\n", yield_time_ms=1000)
        return initial, result

    initial, result = asyncio.run(run())
    assert "ready" in initial + result
    assert "Process running" in initial
    assert "Elapsed:" in initial
    assert "got:ping" in result
    assert "Exit code: 0" in result
    assert "Elapsed:" in result


def test_write_stdin_can_close_stdin(tmp_path):
    async def run() -> tuple[str, str]:
        manager = ExecSessionManager()
        exec_tool = ExecTool(working_dir=str(tmp_path), timeout=5, session_manager=manager)
        stdin_tool = WriteStdinTool(manager=manager)
        command = _python_command(
            "import sys; print('ready', flush=True); "
            "data=sys.stdin.read(); print('got:' + data, flush=True)"
        )

        initial = await exec_tool.execute(command=command, yield_time_ms=1500)
        sid = _session_id(initial)
        result = await stdin_tool.execute(
            session_id=sid,
            chars="payload",
            close_stdin=True,
            yield_time_ms=1500,
        )
        return initial, result

    initial, result = asyncio.run(run())
    assert "ready" in initial + result
    assert "got:payload" in result
    assert "Stdin closed." in result
    assert "Exit code: 0" in result


def test_write_stdin_can_terminate_session(tmp_path):
    async def run() -> tuple[str, str]:
        manager = ExecSessionManager()
        exec_tool = ExecTool(working_dir=str(tmp_path), timeout=30, session_manager=manager)
        stdin_tool = WriteStdinTool(manager=manager)
        command = _python_command(
            "import time; print('ready', flush=True); time.sleep(30)"
        )

        initial = await exec_tool.execute(command=command, yield_time_ms=100)
        sid = _session_id(initial)
        waited = await stdin_tool.execute(
            session_id=sid,
            wait_for="ready",
            wait_timeout_ms=3000,
            yield_time_ms=0,
        )
        result = await stdin_tool.execute(
            session_id=sid,
            terminate=True,
            yield_time_ms=0,
        )
        return initial + waited, result

    initial, result = asyncio.run(run())
    assert "ready" in initial
    assert "Session terminated." in result
    assert "Exit code:" in result


def test_write_stdin_accepts_max_output_tokens_alias(tmp_path):
    async def run() -> tuple[str, str, str]:
        manager = ExecSessionManager()
        exec_tool = ExecTool(working_dir=str(tmp_path), timeout=5, session_manager=manager)
        stdin_tool = WriteStdinTool(manager=manager)
        command = _python_command(
            "import time; print('A' * 2000, flush=True); time.sleep(5)"
        )

        initial = await exec_tool.execute(command=command, yield_time_ms=0)
        sid = _session_id(initial)
        poll = await stdin_tool.execute(
            session_id=sid,
            yield_time_ms=500,
            max_output_tokens=1000,
        )
        cleanup = await stdin_tool.execute(session_id=sid, terminate=True, yield_time_ms=0)
        return initial, poll, cleanup

    initial, poll, cleanup = asyncio.run(run())
    assert "Process running" in initial
    assert "chars truncated" in poll
    assert "Session terminated." in cleanup


def test_write_stdin_preserves_completed_session_output_until_polled(tmp_path):
    async def run() -> tuple[str, str]:
        manager = ExecSessionManager()
        exec_tool = ExecTool(working_dir=str(tmp_path), timeout=5, session_manager=manager)
        stdin_tool = WriteStdinTool(manager=manager)
        command = _python_command(
            "import time; print('ready', flush=True); "
            "time.sleep(1.0); print('done', flush=True)"
        )

        initial = await exec_tool.execute(command=command, yield_time_ms=300)
        sid = _session_id(initial)
        await asyncio.sleep(1.2)
        final = await stdin_tool.execute(session_id=sid, chars="", yield_time_ms=0)
        return initial, final

    initial, final = asyncio.run(run())

    assert "ready" in initial + final
    assert "done" in final
    assert "Exit code: 0" in final


def test_write_stdin_can_wait_for_expected_output(tmp_path):
    async def run() -> tuple[str, str, str]:
        manager = ExecSessionManager()
        exec_tool = ExecTool(working_dir=str(tmp_path), timeout=5, session_manager=manager)
        stdin_tool = WriteStdinTool(manager=manager)
        command = _python_command(
            "import time; print('booting', flush=True); "
            "time.sleep(0.4); print('ready', flush=True); time.sleep(5)"
        )

        initial = await exec_tool.execute(command=command, yield_time_ms=100)
        sid = _session_id(initial)
        waited = await stdin_tool.execute(
            session_id=sid,
            wait_for="ready",
            wait_timeout_ms=3000,
            yield_time_ms=0,
        )
        cleanup = await stdin_tool.execute(session_id=sid, terminate=True, yield_time_ms=0)
        return initial, waited, cleanup

    initial, waited, cleanup = asyncio.run(run())

    assert "Process running" in initial
    assert "booting" in initial + waited
    assert "ready" in waited
    assert "Wait target not observed" not in waited
    assert "Session terminated." in cleanup


def test_write_stdin_wait_for_reports_timeout_without_killing_session(tmp_path):
    async def run() -> tuple[str, str, str]:
        manager = ExecSessionManager()
        exec_tool = ExecTool(working_dir=str(tmp_path), timeout=5, session_manager=manager)
        stdin_tool = WriteStdinTool(manager=manager)
        command = _python_command(
            "import time; print('booting', flush=True); time.sleep(5)"
        )

        initial = await exec_tool.execute(command=command, yield_time_ms=100)
        sid = _session_id(initial)
        waited = await stdin_tool.execute(
            session_id=sid,
            wait_for="never-ready",
            wait_timeout_ms=200,
            yield_time_ms=0,
        )
        cleanup = await stdin_tool.execute(session_id=sid, terminate=True, yield_time_ms=0)
        return initial, waited, cleanup

    initial, waited, cleanup = asyncio.run(run())

    assert "Process running" in initial
    assert "Process running" in waited
    assert "Wait target not observed: 'never-ready'" in waited
    assert "Session terminated." in cleanup


def test_exec_session_mode_reuses_exec_safety_guard(tmp_path):
    manager = ExecSessionManager()
    tool = ExecTool(
        working_dir=str(tmp_path),
        deny_patterns=[r"echo\s+blocked"],
        session_manager=manager,
    )

    result = asyncio.run(tool.execute(command="echo blocked", yield_time_ms=0))

    assert "blocked by deny pattern" in result


def test_write_stdin_reports_missing_session(tmp_path):
    manager = ExecSessionManager()
    tool = WriteStdinTool(manager=manager)

    result = asyncio.run(tool.execute(session_id="missing", chars=""))

    assert "exec session not found" in result


def test_list_exec_sessions_reports_running_commands(tmp_path):
    async def run() -> tuple[str, str, str]:
        manager = ExecSessionManager()
        exec_tool = ExecTool(working_dir=str(tmp_path), timeout=5, session_manager=manager)
        list_tool = ListExecSessionsTool(manager=manager)
        stdin_tool = WriteStdinTool(manager=manager)
        command = _python_command(
            "import time; print('ready', flush=True); time.sleep(5)"
        )

        initial = await exec_tool.execute(command=command, yield_time_ms=500)
        sid = _session_id(initial)
        listing = await list_tool.execute()
        cleanup = await stdin_tool.execute(session_id=sid, terminate=True, yield_time_ms=0)
        return sid, listing, cleanup

    sid, listing, cleanup = asyncio.run(run())

    assert sid in listing
    assert "running" in listing
    assert "elapsed=" in listing
    assert "remaining=" in listing
    assert str(tmp_path) in listing
    assert "Session terminated." in cleanup


def test_list_exec_sessions_reports_empty_state():
    result = asyncio.run(ListExecSessionsTool(manager=ExecSessionManager()).execute())

    assert result == "No active exec sessions."


# ---------------------------------------------------------------------------
# ported upstream QoL: wait without losing the target (b1030ab1) + until_exit
# ---------------------------------------------------------------------------


def test_write_stdin_wait_for_searches_before_response_truncation(tmp_path):
    """Wait targets beyond a small user cap must still be found (b1030ab1)."""
    async def run() -> str:
        manager = ExecSessionManager()
        exec_tool = ExecTool(working_dir=str(tmp_path), timeout=5, session_manager=manager)
        stdin_tool = WriteStdinTool(manager=manager)
        command = _python_command(
            "import sys, time; "
            "sys.stdout.write('A' * 3000 + 'TARGET-MARKER' + 'B' * 3000); "
            "sys.stdout.flush(); time.sleep(0.2)"
        )

        # Start without draining output: yield_time_ms=0 returns the session id
        # while the process is still starting, so the wait loop sees the burst.
        initial = await exec_tool.execute(command=command, yield_time_ms=0)
        sid = _session_id(initial)
        waited = await stdin_tool.execute(
            session_id=sid,
            wait_for="TARGET-MARKER",
            wait_timeout_ms=3000,
            max_output_chars=1000,  # small user cap; target lays beyond it
            yield_time_ms=0,
        )
        return waited

    result = asyncio.run(run())

    assert "TARGET-MARKER" in result
    assert "Wait target not observed" not in result


def test_write_stdin_until_exit_waits_and_reports_exit_code(tmp_path):
    async def run() -> tuple[str, str]:
        manager = ExecSessionManager()
        exec_tool = ExecTool(working_dir=str(tmp_path), timeout=5, session_manager=manager)
        stdin_tool = WriteStdinTool(manager=manager)
        command = _python_command(
            "import time; print('booting', flush=True); "
            "time.sleep(0.3); print('done', flush=True)"
        )

        initial = await exec_tool.execute(command=command, yield_time_ms=50)
        sid = _session_id(initial)
        waited = await stdin_tool.execute(session_id=sid, until_exit=True)
        return initial, waited

    initial, waited = asyncio.run(run())

    assert "Process running" in initial
    assert "done" in waited
    assert "Exit code: 0" in waited
    assert "Process still running" not in waited


def test_write_stdin_until_exit_timeout_reports_still_running(tmp_path):
    async def run() -> str:
        manager = ExecSessionManager()
        exec_tool = ExecTool(working_dir=str(tmp_path), timeout=5, session_manager=manager)
        stdin_tool = WriteStdinTool(manager=manager)
        command = _python_command(
            "import time; print('started', flush=True); time.sleep(5)"
        )

        initial = await exec_tool.execute(command=command, yield_time_ms=0)
        sid = _session_id(initial)
        waited = await stdin_tool.execute(session_id=sid, until_exit=True, timeout_ms=200)
        await stdin_tool.execute(session_id=sid, terminate=True, yield_time_ms=0)
        return waited

    result = asyncio.run(run())

    assert "Process still running after 0.2s." in result


def test_write_stdin_validates_wait_conflicts(tmp_path):
    async def run() -> tuple[str, str, str]:
        manager = ExecSessionManager()
        stdin_tool = WriteStdinTool(manager=manager)
        exec_tool = ExecTool(working_dir=str(tmp_path), timeout=5, session_manager=manager)
        initial = await exec_tool.execute(
            command=_python_command("import time; time.sleep(5)"),
            yield_time_ms=50,
        )
        sid = _session_id(initial)
        both = await stdin_tool.execute(
            session_id=sid, wait_for="x", until_exit=True
        )
        empty_target = await stdin_tool.execute(
            session_id=sid, wait_for=""
        )
        terminate_with_input = await stdin_tool.execute(
            session_id=sid, terminate=True, chars="x"
        )
        await stdin_tool.execute(session_id=sid, terminate=True, yield_time_ms=0)
        return both, empty_target, terminate_with_input

    both, empty_target, terminate_with_input = asyncio.run(run())

    assert "mutually exclusive" in both
    assert "must not be empty" in empty_target
    assert "terminate must be used alone" in terminate_with_input


def test_write_stdin_timeout_ms_unifies_wait_budget(tmp_path):
    async def run() -> tuple[str, str]:
        manager = ExecSessionManager()
        exec_tool = ExecTool(working_dir=str(tmp_path), timeout=5, session_manager=manager)
        stdin_tool = WriteStdinTool(manager=manager)
        command = _python_command(
            "import time; print('booting', flush=True); time.sleep(5)"
        )

        initial = await exec_tool.execute(command=command, yield_time_ms=100)
        sid = _session_id(initial)
        from_timeout = await stdin_tool.execute(
            session_id=sid, wait_for="never-ready", timeout_ms=150
        )
        from_alias = await stdin_tool.execute(
            session_id=sid, wait_for="never-ready", wait_timeout_ms=150
        )
        await stdin_tool.execute(session_id=sid, terminate=True, yield_time_ms=0)
        return from_timeout, from_alias

    from_timeout, from_alias = asyncio.run(run())

    assert "Wait target not observed: 'never-ready'" in from_timeout
    assert "Wait target not observed: 'never-ready'" in from_alias


# ---------------------------------------------------------------------------
# structured outcomes: nonzero exit / timeout / cancel / still-running are
# never ordinary prose success (#structured-outcomes)
# ---------------------------------------------------------------------------


def test_registry_classifies_nonzero_session_exit_as_retryable(tmp_path):
    """A session exit code N through the real registry is a retryable failure."""
    registry = ToolRegistry()
    registry.register(ExecTool(working_dir=str(tmp_path), timeout=10))

    async def run():
        return await registry.execute(
            "exec",
            {
                "command": _python_command("import sys; sys.exit(3)"),
                "yield_time_ms": 1000,
            },
            exec_id="exit-3",
        )

    result = asyncio.run(run())

    assert "Exit code: 3" in str(result)
    assert result.status == "retryable_error"
    assert result.data["state"] == "exited"
    assert result.exit_code == 3


def test_registry_replay_does_not_reexecute_completed_mutation(tmp_path):
    marker = tmp_path / "marker.txt"
    code = (
        "import pathlib;"
        f"p = pathlib.Path({marker.as_posix()!r});"
        "p.open('a').write('ran\\n')"
    )
    registry = ToolRegistry(receipt_store=ActionReceiptStore(tmp_path))
    registry.register(ExecTool(working_dir=str(tmp_path), timeout=10))
    params = {"command": _python_command(code), "yield_time_ms": 1000}

    async def run():
        first = await registry.execute("exec", params, exec_id="once-only")
        second = await registry.execute("exec", params, exec_id="once-only")
        return first, second

    first, second = asyncio.run(run())

    assert first.status == "success"
    assert "Replayed from receipt" in str(second)
    assert second.status == "success"
    assert marker.read_text().count("ran") == 1


def test_registry_persists_running_session_as_unknown_and_suppresses_redispatch(tmp_path):
    marker = tmp_path / "started.txt"
    code = (
        "import pathlib, time;"
        f"p = pathlib.Path({marker.as_posix()!r});"
        "p.open('a').write('started\\n');"
        "time.sleep(10)"
    )
    manager = ExecSessionManager()
    registry = ToolRegistry(receipt_store=ActionReceiptStore(tmp_path))
    registry.register(
        ExecTool(working_dir=str(tmp_path), timeout=30, session_manager=manager)
    )
    params = {"command": _python_command(code), "yield_time_ms": 0}

    async def run():
        first = await registry.execute("exec", params, exec_id="long-run")
        second = await registry.execute("exec", params, exec_id="long-run")
        for _ in range(40):
            if marker.exists():
                break
            await asyncio.sleep(0.05)
        await manager.write(
            session_id=first.data["session_id"],
            chars=None,
            close_stdin=False,
            terminate=True,
            yield_time_ms=0,
            max_output_chars=1000,
        )
        await asyncio.sleep(0.05)  # let subprocess transport close on this loop
        return first, second

    first, second = asyncio.run(run())

    assert first.status == "partial"
    assert first.data["state"] == "running"
    assert "Process running" in str(first)
    assert registry.receipt_store.get("long-run").status == "unknown"
    assert second.status == "partial"
    assert "not auto-repeated" in str(second)
    assert marker.read_text().count("started") == 1


def test_write_stdin_running_poll_is_partial_not_success(tmp_path):
    async def run():
        manager = ExecSessionManager()
        exec_tool = ExecTool(working_dir=str(tmp_path), timeout=10, session_manager=manager)
        stdin_tool = WriteStdinTool(manager=manager)
        start = await exec_tool.execute(
            command=_python_command("import time; time.sleep(5)"), yield_time_ms=0
        )
        sid = _session_id(start)
        poll = await stdin_tool.execute(session_id=sid, yield_time_ms=0)
        await stdin_tool.execute(session_id=sid, terminate=True)
        return start, poll

    start, poll = asyncio.run(run())

    assert start.status == "partial"
    assert poll.status == "partial"
    assert "Process running." in str(poll)
    assert poll.data["state"] == "running"


def test_write_stdin_session_timeout_is_retryable_not_success(tmp_path):
    async def run():
        manager = ExecSessionManager()
        exec_tool = ExecTool(working_dir=str(tmp_path), timeout=1, session_manager=manager)
        stdin_tool = WriteStdinTool(manager=manager)
        start = await exec_tool.execute(
            command=_python_command("import time; time.sleep(30)"), yield_time_ms=0
        )
        return await stdin_tool.execute(
            session_id=_session_id(start), yield_time_ms=1500
        )

    poll = asyncio.run(run())

    assert poll.status == "retryable_error"
    assert "timed out" in str(poll)


def test_write_stdin_terminate_is_retryable_not_success(tmp_path):
    async def run():
        manager = ExecSessionManager()
        exec_tool = ExecTool(working_dir=str(tmp_path), timeout=10, session_manager=manager)
        stdin_tool = WriteStdinTool(manager=manager)
        start = await exec_tool.execute(
            command=_python_command("import time; time.sleep(30)"), yield_time_ms=0
        )
        return await stdin_tool.execute(session_id=_session_id(start), terminate=True)

    poll = asyncio.run(run())

    assert poll.status == "retryable_error"
    assert "Session terminated." in str(poll)
