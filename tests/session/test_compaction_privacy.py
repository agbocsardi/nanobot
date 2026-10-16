"""Internal projection metadata is opt-in and never crosses the provider boundary."""

from copy import deepcopy

import pytest

from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.session.manager import Session
from nanobot.webui.session_context import session_context_payload
from tests.session.test_compaction import SummaryProvider
from tests.session.test_compaction_retention import checkpoint


def assert_public(value):
    if isinstance(value, dict):
        assert all(not str(key).startswith("_") for key in value)
        for item in value.values():
            assert_public(item)
    elif isinstance(value, list):
        for item in value:
            assert_public(item)


def test_history_identity_opt_in_and_public_webui_metrics():
    session = Session("test:public")
    for i in range(4):
        session.add_message("user", f"message {i}")
    checkpoint(session, 2)
    original = deepcopy(session)
    public = session.get_history(projected=True)
    assert_public(public)
    internal = session.get_history(projected=True, include_entry_ids=True)
    assert internal[0]["_compaction_summary"]
    assert internal[1]["_entry_id"] == session.messages[2]["_entry_id"]
    payload = session_context_payload(session)
    assert_public(payload)
    assert payload["schema_version"] == 1
    assert payload["replay_messages"] == 3
    assert "messages" not in payload  # This endpoint returns aggregates, not replay data.
    assert session == original


async def test_vision_describer_also_receives_only_public_fields():
    from nanobot.agent.vision_handoff import VisionHandoff

    provider = SummaryProvider()
    handoff = VisionHandoff(provider, "vision-model", max_raw_images=0)
    source = [{"role": "user", "content": [{
        "type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="},
        "_meta": {"path": "tmp/context-images/known.img"},
    }]}]
    original = deepcopy(source)
    await handoff.transform(source, "physical-model")
    assert provider.requests
    assert_public(provider.requests[0]["messages"])
    assert source == original


@pytest.mark.parametrize("no_tools", [False, True])
async def test_every_provider_message_is_recursively_public(no_tools):
    provider = SummaryProvider()
    source = [
        {"role": "user", "content": "goal", "_entry_id": "user", "_future_metadata": {"secret": True}},
        {"role": "assistant", "content": "", "_private": True, "tool_calls": [{
            "id": "call", "type": "function", "function": {
                "name": "read_file", "arguments": '{"_business_argument":"unchanged"}'},
            "extra_content": {"google": {"thought_signature": "native-signature", "_private": "hidden"}},
        }]},
        {"role": "tool", "tool_call_id": "call", "_images_seen": True, "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="},
             "_meta": {"path": "tmp/context-images/test.img"}},
            {"type": "text", "text": "evidence", "_local": "hidden"},
        ]},
    ]
    original = deepcopy(source)
    spec = AgentRunSpec(initial_messages=source, tools=ToolRegistry(), model="physical-model",
                        context_window_tokens=32768, max_iterations=2, max_tool_result_chars=1000)
    runner = AgentRunner(provider)
    if no_tools:
        await runner._request_no_tools(spec, source)
    else:
        result = await runner.run(spec)
        assert result.stop_reason == "completed"
    for request in provider.requests:
        assert_public(request["messages"])
    messages = provider.requests[-1]["messages"]
    assert messages[1]["tool_calls"][0]["function"]["arguments"] == '{"_business_argument":"unchanged"}'
    assert messages[1]["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == "native-signature"
    assert source == original
