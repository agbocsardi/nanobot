from nanobot.config.schema import Config
from nanobot.providers.factory import _make_provider_core
from nanobot.providers.openai_compat_provider import OpenAICompatProvider
from nanobot.providers.registry import find_by_name


def test_umans_uses_openai_compatible_provider() -> None:
    config = Config.model_validate({
        "agents": {"defaults": {"model": "umans/umans-kimi-k2.7"}},
        "providers": {"umans": {"apiKey": "test-key"}},
    })

    provider = _make_provider_core(config)

    assert isinstance(provider, OpenAICompatProvider)
    assert provider._spec == find_by_name("umans")
    assert provider._effective_base == "https://api.code.umans.ai/v1"


def test_umans_sends_openai_chat_completion_fields() -> None:
    provider = OpenAICompatProvider(
        api_key="test-key",
        default_model="umans/umans-kimi-k2.7",
        spec=find_by_name("umans"),
    )

    kwargs = provider._build_kwargs(
        messages=[{"role": "user", "content": "Reply exactly OK"}],
        tools=None,
        model=None,
        max_tokens=100,
        temperature=0.1,
        reasoning_effort="low",
        tool_choice=None,
    )

    assert kwargs["model"] == "umans/umans-kimi-k2.7"
    assert kwargs["max_completion_tokens"] == 100
    assert kwargs["reasoning_effort"] == "low"
