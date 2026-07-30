"""Wiring + lifecycle checks for the whisper.cpp transcription provider.

Guards the glue (registry resolution, the no-api-key `configured`
short-circuit) and the auto-spawn lifecycle (probe → spawn → POST → kill
what we started; reuse an already-running server). The HTTP transcription
path itself rides the shared `_post_transcription_with_retry` helper used by
every other adapter, so we mock it here rather than standing up a real
whisper-server.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanobot.audio.transcription import EffectiveTranscriptionConfig
from nanobot.audio.transcription_registry import (
    get_transcription_provider,
    resolve_transcription_provider,
)
from nanobot.providers.transcription import WhisperCppTranscriptionProvider


def test_registry_resolves_whispercpp_name_and_aliases() -> None:
    spec = get_transcription_provider("whispercpp")
    assert spec is not None
    assert spec.adapter == "nanobot.providers.transcription:WhisperCppTranscriptionProvider"

    for alias in ("whisper.cpp", "whisper-cpp", "whisper_server", "whisper-server"):
        assert resolve_transcription_provider(alias).name == "whispercpp"


def test_adapter_points_at_inference_endpoint() -> None:
    provider = WhisperCppTranscriptionProvider(
        api_base="http://127.0.0.1:8888/", language="en"
    )
    assert provider.api_url == "http://127.0.0.1:8888/inference"
    assert provider.language == "en"


def test_whispercpp_is_configured_without_an_api_key() -> None:
    cfg = EffectiveTranscriptionConfig(
        enabled=True,
        provider="whispercpp",
        model="whispercpp",
        language=None,
        api_key="",
        api_base="",
        max_duration_sec=120,
        max_upload_mb=25,
    )
    assert cfg.configured is True


@pytest.mark.asyncio
async def test_reuses_already_running_server_without_spawning(tmp_path) -> None:
    audio = tmp_path / "note.wav"
    audio.write_bytes(b"data")

    provider = WhisperCppTranscriptionProvider(api_base="http://127.0.0.1:8888")

    with (
        patch.object(
            provider, "_server_up", new=AsyncMock(return_value=True)
        ) as up,
        patch.object(
            provider, "_spawn_server", new=AsyncMock(return_value=None)
        ) as spawn,
        patch(
            "nanobot.providers.transcription._post_transcription_with_retry",
            new=AsyncMock(return_value="hello world"),
        ) as post,
    ):
        text = await provider.transcribe(audio)

    assert text == "hello world"
    up.assert_awaited_once()
    spawn.assert_not_awaited()  # server was up → no spawn
    post.assert_awaited_once()


@pytest.mark.asyncio
async def test_spawns_when_no_server_then_kills_it(tmp_path) -> None:
    audio = tmp_path / "note.wav"
    audio.write_bytes(b"data")

    provider = WhisperCppTranscriptionProvider(api_base="http://127.0.0.1:8888")
    fake_proc = MagicMock()
    fake_proc.returncode = None  # still running until we kill it
    fake_proc.wait = AsyncMock()

    with (
        patch.object(
            provider, "_server_up", new=AsyncMock(return_value=False)
        ) as up,
        patch.object(
            provider, "_spawn_server", new=AsyncMock(return_value=fake_proc)
        ) as spawn,
        patch(
            "nanobot.providers.transcription._post_transcription_with_retry",
            new=AsyncMock(return_value="hello world"),
        ) as post,
    ):
        text = await provider.transcribe(audio)

    assert text == "hello world"
    up.assert_awaited_once()
    spawn.assert_awaited_once()  # no server → spawned one
    post.assert_awaited_once()
    # we started it → we must tear it down
    fake_proc.terminate.assert_called_once()
