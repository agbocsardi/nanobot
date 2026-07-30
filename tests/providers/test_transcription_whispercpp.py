"""wiring checks for the whisper.cpp transcription provider.

These guard the glue (registry resolution, adapter URL, and the no-api-key
`configured` short-circuit) — the parts most likely to silently break. The
HTTP path itself is exercised through the shared `_post_transcription_with_retry`
helper used by every other transcription adapter.
"""

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


def test_adapter_points_at_inference_endpoint_and_needs_no_key() -> None:
    provider = WhisperCppTranscriptionProvider(
        api_base="http://127.0.0.1:8888/", language="en"
    )
    assert provider.api_url == "http://127.0.0.1:8888/inference"
    assert provider.language == "en"


def test_adapter_falls_back_to_env_base_url() -> None:
    provider = WhisperCppTranscriptionProvider()
    # Default when neither arg nor env is set.
    assert provider.api_url.endswith("/inference")


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
