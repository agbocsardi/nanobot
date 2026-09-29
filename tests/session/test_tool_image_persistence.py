from pathlib import Path

from nanobot.session.manager import Session, SessionManager, sanitize_message_for_persistence


def _image_block(path: str, url: str = "data:image/png;base64,AAAA") -> dict:
    return {
        "type": "image_url",
        "image_url": {"url": url},
        "_meta": {"path": path},
    }


def test_tool_image_reference_survives_save_load(tmp_path: Path) -> None:
    manager = SessionManager(tmp_path)
    staged = tmp_path / "tmp" / "context-images" / "123-random.img"
    staged.parent.mkdir(parents=True)
    session = Session(key="test:1", messages=[
        {"role": "assistant", "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "generate_image", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call-1", "name": "generate_image", "content": [
            {"type": "text", "text": "created"}, _image_block(str(staged)),
        ]},
    ])
    manager.save(session)
    manager.invalidate(session.key)
    loaded = manager.get_or_create(session.key)
    tool = loaded.messages[1]
    assert tool["tool_call_id"] == "call-1"
    lines = tool["content"].splitlines()
    assert lines[0].startswith("[tool result omitted: generate_image, ")
    assert lines[0].endswith(" chars]")
    assert lines[1] == "[image: tmp/context-images/123-random.img; re-read via read_file]"
    assert "base64" not in tool["content"]


def test_tool_image_metadata_rejects_remote_and_outside_paths(tmp_path: Path) -> None:
    workspace = tmp_path
    safe = workspace / "tmp" / "context-images" / "ok.img"
    cases = [
        _image_block(str(workspace / "secret.txt")),
        _image_block(str(workspace / "tmp" / "context-images" / "../.." / "secret.txt")),
        _image_block(str(safe), "https://example.test/image.png"),
        _image_block(str(safe), "data:text/plain;base64,SECRET"),
    ]
    for block in cases:
        result = sanitize_message_for_persistence(
            {"role": "tool", "name": "tool", "content": [block]}, workspace=workspace
        )
        assert "tmp/context-images" not in result["content"]
        assert "secret" not in result["content"]
        assert "https://" not in result["content"]
        assert "base64" not in result["content"]


def test_non_image_tool_result_keeps_existing_placeholder(tmp_path: Path) -> None:
    result = sanitize_message_for_persistence(
        {"role": "tool", "name": "shell", "content": "private output"}, workspace=tmp_path
    )
    assert result["content"] == "[tool result omitted: shell, 14 chars]"
