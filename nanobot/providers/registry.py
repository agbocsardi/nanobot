"""
Provider Registry — single source of truth for LLM provider metadata.

Adding a new provider:
  1. Add a ProviderSpec to PROVIDERS below.
  2. Add a field to ProvidersConfig in config/schema.py.
  Done. Env vars, config matching, status display all derive from here.

Order matters — it controls match priority and fallback. Gateways first.
Every entry writes out all fields so you can copy-paste as a template.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic.alias_generators import to_snake


@dataclass(frozen=True)
class ProviderSpec:
    """One LLM provider's metadata. See PROVIDERS below for real examples.

    Placeholders in env_extras values:
      {api_key}  — the user's API key
      {api_base} — api_base from config, or this spec's default_api_base
    """

    # identity
    name: str  # config field name, e.g. "dashscope"
    keywords: tuple[str, ...]  # model-name keywords for matching (lowercase)
    env_key: str  # env var for API key, e.g. "DASHSCOPE_API_KEY"
    display_name: str = ""  # shown in `nanobot status`

    # which provider implementation to use
    # "openai_compat" | "anthropic" | "openai_codex"
    backend: str = "openai_compat"

    # extra env vars, e.g. (("ZHIPUAI_API_KEY", "{api_key}"),)
    env_extras: tuple[tuple[str, str], ...] = ()

    # gateway / local detection
    is_gateway: bool = False  # routes any model (OpenRouter, AiHubMix)
    is_local: bool = False  # local deployment (vLLM, Ollama)
    detect_by_key_prefix: str = ""  # match api_key prefix, e.g. "sk-or-"
    detect_by_base_keyword: str = ""  # match substring in api_base URL
    default_api_base: str = ""  # OpenAI-compatible base URL for this provider

    # gateway behavior
    strip_model_prefix: bool = False  # strip "provider/" before sending to gateway
    strip_model_prefixes: tuple[str, ...] = ()  # strip only when the first model segment matches
    supports_max_completion_tokens: bool = False

    # per-model param overrides, e.g. (("kimi-k2.5", {"temperature": 1.0}),)
    model_overrides: tuple[tuple[str, dict[str, Any]], ...] = ()

    # OAuth-based providers (e.g., OpenAI Codex) don't use API keys
    is_oauth: bool = False

    # Direct providers skip API-key validation (user supplies everything)
    is_direct: bool = False

    # Provider is listed for shared credentials but cannot serve chat completions.
    is_transcription_only: bool = False

    # Provider supports cache_control on content blocks (e.g. Anthropic prompt caching)
    supports_prompt_caching: bool = False

    # How to inject the thinking on/off toggle into extra_body.
    # ""              — no extra_body needed (default)
    # "thinking_type" — {"thinking": {"type": "enabled"/"disabled"}}
    #                   (DeepSeek, VolcEngine, BytePlus)
    # "enable_thinking" — {"enable_thinking": true/false}  (DashScope)
    # "reasoning_split" — {"reasoning_split": true/false}  (MiniMax)
    thinking_style: str = ""

    # Gateway-native reasoning control to pair with model-level thinking styles.
    # "reasoning_effort" — {"reasoning": {"effort": <none|minimal|...>}}
    #                      (OpenRouter)
    gateway_reasoning_style: str = ""

    # When True, treat the "reasoning" response field as formal content
    # when "content" is empty.  Only set this for providers (e.g. StepFun)
    # whose API returns the actual answer in "reasoning" instead of "content".
    reasoning_as_content: bool = False

    @property
    def label(self) -> str:
        return self.display_name or self.name.title()


# ---------------------------------------------------------------------------
# PROVIDERS — the registry. Order = priority. Copy any entry as template.
# ---------------------------------------------------------------------------

PROVIDERS: tuple[ProviderSpec, ...] = (
    # === Custom direct OpenAI-compatible endpoint ==========================
    ProviderSpec(
        name="custom",
        keywords=(),
        env_key="",
        display_name="Custom",
        backend="openai_compat",
        is_direct=True,
    ),

    # === Gateway kept as an explicit fallback/escape hatch =================
    ProviderSpec(
        name="openrouter",
        keywords=("openrouter",),
        env_key="OPENROUTER_API_KEY",
        display_name="OpenRouter",
        backend="openai_compat",
        is_gateway=True,
        detect_by_key_prefix="sk-or-",
        detect_by_base_keyword="openrouter",
        default_api_base="https://openrouter.ai/api/v1",
        supports_prompt_caching=True,
        gateway_reasoning_style="reasoning_effort",
    ),

    # === Active providers / OpenCode routes ================================
    ProviderSpec(
        name="anthropic",
        keywords=("anthropic", "claude"),
        env_key="ANTHROPIC_API_KEY",
        display_name="Anthropic",
        backend="anthropic",
        supports_prompt_caching=True,
    ),
    ProviderSpec(
        name="openai",
        keywords=("openai", "gpt"),
        env_key="OPENAI_API_KEY",
        display_name="OpenAI",
        backend="openai_compat",
        supports_max_completion_tokens=True,
    ),
    ProviderSpec(
        name="openai_codex",
        keywords=("openai-codex",),
        env_key="",
        display_name="OpenAI Codex",
        backend="openai_codex",
        detect_by_base_keyword="codex",
        default_api_base="https://chatgpt.com/backend-api",
        is_oauth=True,
    ),
    ProviderSpec(
        name="deepseek",
        keywords=("deepseek",),
        env_key="DEEPSEEK_API_KEY",
        display_name="DeepSeek",
        backend="openai_compat",
        default_api_base="https://api.deepseek.com",
        thinking_style="thinking_type",
    ),
    ProviderSpec(
        name="zhipu",
        keywords=("zhipu", "glm"),
        env_key="ZAI_API_KEY",
        display_name="Zhipu AI",
        backend="openai_compat",
        env_extras=(("ZHIPUAI_API_KEY", "{api_key}"),),
        default_api_base="https://open.bigmodel.cn/api/paas/v4",
    ),
    ProviderSpec(
        name="zai",
        keywords=("zai",),
        env_key="ZAI_API_KEY",
        display_name="Z.ai",
        backend="openai_compat",
        default_api_base="https://api.z.ai/api/paas/v4",
    ),
    ProviderSpec(
        name="dashscope",
        keywords=("qwen", "dashscope"),
        env_key="DASHSCOPE_API_KEY",
        display_name="DashScope",
        backend="openai_compat",
        default_api_base="https://dashscope.aliyuncs.com/compatible-mode/v1",
        thinking_style="enable_thinking",
    ),
    ProviderSpec(
        name="moonshot",
        keywords=("moonshot", "kimi"),
        env_key="MOONSHOT_API_KEY",
        display_name="Moonshot",
        backend="openai_compat",
        default_api_base="https://api.moonshot.ai/v1",
        model_overrides=(
            ("kimi-k2.5", {"temperature": 1.0}),
            ("kimi-k2.6", {"temperature": 1.0}),
        ),
    ),
    ProviderSpec(
        name="minimax",
        keywords=("minimax",),
        env_key="MINIMAX_API_KEY",
        display_name="MiniMax",
        backend="openai_compat",
        default_api_base="https://api.minimax.io/v1",
        thinking_style="reasoning_split",
    ),
    ProviderSpec(
        name="minimax_anthropic",
        keywords=("minimax_anthropic",),
        env_key="MINIMAX_API_KEY",
        display_name="MiniMax (Anthropic)",
        backend="anthropic",
        default_api_base="https://api.minimax.io/anthropic",
    ),
    ProviderSpec(
        name="xiaomi_mimo",
        keywords=("xiaomi_mimo", "mimo"),
        env_key="XIAOMIMIMO_API_KEY",
        display_name="Xiaomi MIMO",
        backend="openai_compat",
        default_api_base="https://api.xiaomimimo.com/v1",
        thinking_style="thinking_type",
    ),
    # OpenCode Zen: curated reliable models for coding agents
    ProviderSpec(
        name="opencode_zen",
        keywords=("opencode_zen", "opencode-zen", "zen"),
        env_key="OPENCODE_API_KEY",
        display_name="OpenCode Zen",
        backend="openai_compat",
        is_gateway=True,
        default_api_base="https://opencode.ai/zen/v1",
    ),
    # OpenCode Go: low-cost coding models
    ProviderSpec(
        name="opencode_go",
        keywords=("opencode_go", "opencode-go"),
        env_key="OPENCODE_API_KEY",
        display_name="OpenCode Go",
        backend="openai_compat",
        is_gateway=True,
        default_api_base="https://opencode.ai/zen/go/v1",
    ),
    # Umans Code: open-weight coding models (umans-coder routes to Kimi K2.7-Code)
    # OpenAI route uses max_completion_tokens and reasoning counts toward it
    # (docs: /v1/chat/completions), and primary models reason by default.
    ProviderSpec(
        name="umans",
        keywords=("umans",),
        env_key="UMANS_API_KEY",
        display_name="Umans",
        backend="openai_compat",
        default_api_base="https://api.code.umans.ai/v1",
        supports_max_completion_tokens=True,
    ),
)

# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------


def find_by_name(name: str) -> ProviderSpec | None:
    """Find a provider spec by config field name, e.g. "dashscope"."""
    normalized = to_snake(name.replace("-", "_"))
    for spec in PROVIDERS:
        if spec.name == normalized:
            return spec
    return None


def create_dynamic_spec(name: str) -> ProviderSpec:
    """Create a dynamic ProviderSpec for custom user-defined providers."""
    normalized = to_snake(name.replace("-", "_"))
    strip_prefixes = tuple(dict.fromkeys((name, normalized)))
    return ProviderSpec(
        name=normalized,
        keywords=(),
        env_key="",
        display_name=name.title(),
        backend="openai_compat",
        is_direct=True,
        strip_model_prefixes=strip_prefixes,
    )
