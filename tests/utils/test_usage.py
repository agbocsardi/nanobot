from nanobot.utils.usage import add_usage, usage_delta


def test_add_usage_accumulates_by_model():
    total = {}

    assert add_usage(
        total,
        {"prompt_tokens": 10, "completion_tokens": 5, "cached_tokens": 3},
        provider="opencode_go",
        model="deepseek-v4-pro",
    )
    assert add_usage(
        total,
        {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9},
        provider="opencode_go",
        model="deepseek-v4-pro",
    )

    assert total["prompt_tokens"] == 17
    assert total["completion_tokens"] == 7
    assert total["total_tokens"] == 24
    assert total["cached_tokens"] == 3
    assert total["requests"] == 2
    row = total["by_model"]["opencode_go/deepseek-v4-pro"]
    assert row["provider"] == "opencode_go"
    assert row["model"] == "deepseek-v4-pro"
    assert row["total_tokens"] == 24
    assert row["requests"] == 2


def test_usage_delta_includes_model_breakdown():
    previous = {}
    current = {}
    add_usage(previous, {"prompt_tokens": 10, "completion_tokens": 5}, provider="openai", model="a")
    add_usage(current, {"prompt_tokens": 10, "completion_tokens": 5}, provider="openai", model="a")
    add_usage(current, {"prompt_tokens": 3, "completion_tokens": 4}, provider="opencode_go", model="b")

    delta = usage_delta(current, previous)

    assert delta["prompt_tokens"] == 3
    assert delta["completion_tokens"] == 4
    assert delta["total_tokens"] == 7
    assert delta["requests"] == 1
    assert list(delta["by_model"]) == ["opencode_go/b"]
    assert delta["by_model"]["opencode_go/b"]["total_tokens"] == 7
