"""Old tool images should not remain in the live turn indefinitely."""

from nanobot.agent.runner import AgentRunner


def _image(i):
    return {
        "role": "tool", "name": "read_file", "tool_call_id": f"call_{i}",
        "content": [
            {"type": "text", "text": "(Image file)"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "a" * 5000},
             "_meta": {"path": f"/tmp/screenshot-{i}.png"}},
        ],
    }


def test_microcompact_old_images_but_preserves_latest_unseen_batch():
    messages = [{"role": "user", "content": "inspect screenshots"}]
    for i in range(5):
        messages.append({"role": "assistant", "content": "", "tool_calls": [{"id": f"call_{i}"}]})
        messages.append(_image(i))
    compacted = AgentRunner._microcompact(messages)
    assert all("image omitted" in compacted[i]["content"] for i in (2, 4, 6))
    assert "/tmp/screenshot-0.png" in compacted[2]["content"]
    assert all(isinstance(compacted[i]["content"], list) for i in (8, 10))
    assert all(isinstance(messages[i]["content"], list) for i in (2, 4, 6, 8, 10))


def test_microcompact_keeps_all_images_in_same_unseen_batch():
    messages = [{"role": "user", "content": "inspect screenshots"},
                {"role": "assistant", "content": "", "tool_calls": [{"id": "one"}, {"id": "two"}]},
                _image(0), _image(1)]
    compacted = AgentRunner._microcompact(messages)
    assert all(isinstance(msg["content"], list) for msg in compacted[2:])


def test_microcompact_does_not_discard_the_only_image():
    image = _image(0)
    assert AgentRunner._microcompact([image])[0]["content"] == image["content"]
