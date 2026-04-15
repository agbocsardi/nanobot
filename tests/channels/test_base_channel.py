from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel


class _DummyChannel(BaseChannel):
    name = "dummy"

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send(self, msg: OutboundMessage) -> None:
        return None


def test_is_allowed_requires_exact_match() -> None:
    channel = _DummyChannel(SimpleNamespace(allow_from=["allow@email.com"]), MessageBus())

    assert channel.is_allowed("allow@email.com") is True
    assert channel.is_allowed("attacker|allow@email.com") is False


def test_is_allowed_supports_dict_allow_from_alias() -> None:
    channel = _DummyChannel({"allowFrom": ["alice"]}, MessageBus())

    assert channel.is_allowed("alice") is True


def test_is_allowed_denies_empty_dict_allow_from() -> None:
    channel = _DummyChannel({"allow_from": []}, MessageBus())

    assert channel.is_allowed("alice") is False


async def test_transcribe_audio_skips_groq_without_api_key() -> None:
    channel = _DummyChannel({}, MessageBus())
    channel.transcription_provider = "groq"
    channel.transcription_api_key = ""

    result = await channel.transcribe_audio("/tmp/fake.ogg")
    assert result == ""


async def test_transcribe_audio_skips_openai_without_api_key() -> None:
    channel = _DummyChannel({}, MessageBus())
    channel.transcription_provider = "openai"
    channel.transcription_api_key = ""

    result = await channel.transcribe_audio("/tmp/fake.ogg")
    assert result == ""


async def test_transcribe_audio_faster_whisper_not_blocked_by_empty_key() -> None:
    channel = _DummyChannel({}, MessageBus())
    channel.transcription_provider = "faster_whisper"
    channel.transcription_api_key = ""  # should NOT block faster_whisper

    mock_provider = AsyncMock()
    mock_provider.transcribe = AsyncMock(return_value="transcribed text")

    with patch(
        "nanobot.providers.transcription.FasterWhisperTranscriptionProvider",
        return_value=mock_provider,
    ):
        result = await channel.transcribe_audio("/tmp/fake.ogg")

    assert result == "transcribed text"
    mock_provider.transcribe.assert_called_once_with("/tmp/fake.ogg")


async def test_transcribe_audio_faster_whisper_uses_config() -> None:
    channel = _DummyChannel({}, MessageBus())
    channel.transcription_provider = "faster_whisper"
    channel.transcription_api_key = ""
    channel.transcription_faster_whisper = SimpleNamespace(
        venv_python="/opt/whisper/bin/python",
        script_path="/opt/whisper/run.py",
        model="large-v3",
        device="cuda",
        compute_type="float16",
    )

    mock_provider = AsyncMock()
    mock_provider.transcribe = AsyncMock(return_value="gpu text")

    with patch(
        "nanobot.providers.transcription.FasterWhisperTranscriptionProvider",
        return_value=mock_provider,
    ) as mock_cls:
        result = await channel.transcribe_audio("/tmp/fake.ogg")

    assert result == "gpu text"
    mock_cls.assert_called_once_with(
        venv_python="/opt/whisper/bin/python",
        script_path="/opt/whisper/run.py",
        model="large-v3",
        device="cuda",
        compute_type="float16",
    )
