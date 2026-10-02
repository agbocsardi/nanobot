"""Fork metadata progress synthesis in the WebUI outbound projector.

Fork turns carry progress/tool-hint state in OutboundMessage metadata
(nanobot.bus.progress); the projector must render those as progress/tool-hint
rows instead of plain answer bubbles, without touching typed upstream events.
"""

from __future__ import annotations

from typing import Any

from nanobot.bus.events import OutboundMessage
from nanobot.bus.outbound_events import ProgressEvent
from nanobot.webui.outbound_projection import _fork_metadata_progress_event


def _msg(metadata: dict[str, Any] | None) -> OutboundMessage:
    return OutboundMessage(
        channel="websocket",
        chat_id="c1",
        content="reading tests/test_x.py",
        metadata=metadata or {},
    )


def test_fork_tool_hint_metadata_becomes_progress_event() -> None:
    msg = _msg({"_progress": True, "_tool_hint": True})
    event = _fork_metadata_progress_event(msg)
    assert isinstance(event, ProgressEvent)
    assert event.tool_hint is True
    assert event.content == "reading tests/test_x.py"


def test_fork_progress_metadata_without_hint() -> None:
    event = _fork_metadata_progress_event(_msg({"_progress": True}))
    assert isinstance(event, ProgressEvent)
    assert event.tool_hint is False


def test_fork_tool_and_file_events_forwarded() -> None:
    tool_events = [{"kind": "tool_start", "name": "read_file"}]
    file_edits = [{"path": "a.py"}]
    event = _fork_metadata_progress_event(_msg({
        "_progress": True,
        "_tool_events": tool_events,
        "_file_edit_events": file_edits,
    }))
    assert isinstance(event, ProgressEvent)
    assert event.tool_events == tool_events
    assert event.file_edit_events == file_edits


def test_non_progress_metadata_yields_none() -> None:
    assert _fork_metadata_progress_event(_msg(None)) is None
    assert _fork_metadata_progress_event(_msg({"message_id": "m1"})) is None
    # Malformed lists are dropped rather than forwarded.
    event = _fork_metadata_progress_event(_msg({"_progress": True, "_tool_events": "nope"}))
    assert event is not None
    assert event.tool_events is None
