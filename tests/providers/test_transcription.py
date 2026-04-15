"""Tests for FasterWhisperTranscriptionProvider."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from nanobot.providers.transcription import FasterWhisperTranscriptionProvider


def _make_proc(
    returncode: int = 0, stdout: bytes = b"hello world", stderr: bytes = b""
) -> SimpleNamespace:
    """Fake async subprocess with communicate() returning (stdout, stderr)."""
    proc = SimpleNamespace(
        returncode=returncode,
        kill=AsyncMock(),
        wait=AsyncMock(),
    )
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


@pytest.fixture()
def provider(tmp_path: Path) -> FasterWhisperTranscriptionProvider:
    return FasterWhisperTranscriptionProvider(
        venv_python="/usr/bin/python3",
        script_path=str(tmp_path / "transcribe.py"),
        model="small",
        device="cpu",
        compute_type="int8",
    )


def test_default_script_path_is_bundled() -> None:
    p = FasterWhisperTranscriptionProvider()
    assert p.script_path.endswith("whisper/transcribe.py")


def test_explicit_script_path_is_used(tmp_path: Path) -> None:
    custom = tmp_path / "custom.py"
    custom.touch()
    p = FasterWhisperTranscriptionProvider(script_path=str(custom))
    assert p.script_path == str(custom)


def test_venv_python_expands_tilde() -> None:
    p = FasterWhisperTranscriptionProvider(venv_python="~/my-env/bin/python")
    assert "~" not in p.venv_python
    assert p.venv_python.endswith("my-env/bin/python")


async def test_transcribe_returns_stdout_on_success(
    provider: FasterWhisperTranscriptionProvider, tmp_path: Path
) -> None:
    audio = tmp_path / "note.ogg"
    audio.write_bytes(b"fake")

    proc = _make_proc(returncode=0, stdout=b"Hello, world!")

    with patch(
        "nanobot.providers.transcription.asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
        return_value=proc,
    ) as mock_exec:
        result = await provider.transcribe(audio)

    assert result == "Hello, world!"
    mock_exec.assert_called_once()
    args = mock_exec.call_args
    assert args[0][0] == provider.venv_python
    assert args[0][1] == provider.script_path
    assert args[0][2] == str(audio)


async def test_transcribe_returns_empty_on_nonzero_exit(
    provider: FasterWhisperTranscriptionProvider, tmp_path: Path
) -> None:
    audio = tmp_path / "note.ogg"
    audio.write_bytes(b"fake")

    proc = _make_proc(returncode=2, stdout=b"", stderr=b"model load failed")

    with patch(
        "nanobot.providers.transcription.asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
        return_value=proc,
    ):
        result = await provider.transcribe(audio)

    assert result == ""


async def test_transcribe_returns_empty_on_missing_file(
    provider: FasterWhisperTranscriptionProvider,
) -> None:
    result = await provider.transcribe("/tmp/does_not_exist_12345.ogg")
    assert result == ""


async def test_transcribe_returns_empty_on_timeout(
    provider: FasterWhisperTranscriptionProvider, tmp_path: Path
) -> None:
    audio = tmp_path / "note.ogg"
    audio.write_bytes(b"fake")

    proc = SimpleNamespace(
        returncode=None,
        kill=AsyncMock(),
        wait=AsyncMock(),
    )
    proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)

    with (
        patch(
            "nanobot.providers.transcription.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=proc,
        ),
        patch("nanobot.providers.transcription.asyncio.wait_for", side_effect=asyncio.TimeoutError),
    ):
        result = await provider.transcribe(audio)

    assert result == ""
    proc.kill.assert_called_once()


async def test_transcribe_passes_env_vars(
    provider: FasterWhisperTranscriptionProvider, tmp_path: Path
) -> None:
    audio = tmp_path / "note.ogg"
    audio.write_bytes(b"fake")

    proc = _make_proc(returncode=0, stdout=b"ok")

    with patch(
        "nanobot.providers.transcription.asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
        return_value=proc,
    ) as mock_exec:
        await provider.transcribe(audio)

    env = mock_exec.call_args[1]["env"]
    assert env["NANOBOT_WHISPER_MODEL"] == "small"
    assert env["NANOBOT_WHISPER_DEVICE"] == "cpu"
    assert env["NANOBOT_WHISPER_COMPUTE_TYPE"] == "int8"


async def test_transcribe_honors_custom_config(tmp_path: Path) -> None:
    audio = tmp_path / "note.ogg"
    audio.write_bytes(b"fake")

    provider = FasterWhisperTranscriptionProvider(
        venv_python="/usr/bin/python3",
        script_path=str(tmp_path / "custom.py"),
        model="large-v3",
        device="cuda",
        compute_type="float16",
    )

    proc = _make_proc(returncode=0, stdout=b"gpu result")

    with patch(
        "nanobot.providers.transcription.asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
        return_value=proc,
    ) as mock_exec:
        result = await provider.transcribe(audio)

    assert result == "gpu result"
    env = mock_exec.call_args[1]["env"]
    assert env["NANOBOT_WHISPER_MODEL"] == "large-v3"
    assert env["NANOBOT_WHISPER_DEVICE"] == "cuda"
    assert env["NANOBOT_WHISPER_COMPUTE_TYPE"] == "float16"
