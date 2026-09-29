import base64
from types import SimpleNamespace

from nanobot.providers.openai_codex_provider import OpenAICodexProvider
from nanobot.utils.helpers import estimate_prompt_tokens_chain


class UnknownProvider:
    pass


def _tool_image_messages() -> list[dict]:
    data = base64.b64encode(b"x" * 80_000).decode()
    return [{
        "role": "tool",
        "tool_call_id": "call_1",
        "content": [{
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64," + data},
        }],
    }]


def test_codex_tool_image_uses_vision_allowance() -> None:
    tokens, source = estimate_prompt_tokens_chain(
        OpenAICodexProvider(), "openai-codex/gpt-5", _tool_image_messages()
    )

    assert tokens < 10_000
    assert source == "tiktoken"


def test_unknown_provider_counts_tool_image_base64() -> None:
    tokens, _ = estimate_prompt_tokens_chain(
        UnknownProvider(), "some-model", _tool_image_messages()
    )

    assert tokens > 50_000


def test_wrapped_codex_primary_uses_vision_allowance() -> None:
    provider = SimpleNamespace(_primary=OpenAICodexProvider())
    tokens, _ = estimate_prompt_tokens_chain(
        provider, "openai-codex/gpt-5", _tool_image_messages()
    )
    assert tokens < 10_000


def test_thinking_blocks_count_toward_prompt_budget() -> None:
    short, _ = estimate_prompt_tokens_chain(
        UnknownProvider(), "some-model", [{"role": "assistant", "content": "ok"}]
    )
    long, _ = estimate_prompt_tokens_chain(
        UnknownProvider(), "some-model", [{
            "role": "assistant", "content": "ok",
            "thinking_blocks": [{"type": "thinking", "thinking": "x" * 100_000, "signature": "sig"}],
        }]
    )
    assert long > short + 10_000
