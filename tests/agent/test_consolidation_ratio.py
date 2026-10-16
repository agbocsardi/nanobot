"""The existing ratio now selects a recent-token target, not an archival cursor."""

from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from nanobot.agent.memory import Consolidator, MemoryStore
from nanobot.config.schema import AgentDefaults
from nanobot.session.compaction import RequestBudget
from nanobot.session.manager import SessionManager
from tests.session.test_compaction import SummaryProvider, transcript


@pytest.mark.parametrize("ratio", [0.1, 0.5, 0.9])
async def test_consolidation_ratio_controls_recent_token_target(tmp_path, ratio):
    provider = SummaryProvider()
    sessions = SessionManager(tmp_path)
    session = sessions.get_or_create("test:ratio")
    session.messages = transcript()
    sessions.save(session)
    consolidator = Consolidator(
        store=MemoryStore(tmp_path), provider=provider, model="physical-model", sessions=sessions,
        context_window_tokens=8192, max_completion_tokens=256, consolidation_ratio=ratio,
        build_messages=lambda **kw: kw["history"], get_tool_definitions=lambda: [],
    )
    consolidator.flush_before_compaction = AsyncMock()
    checkpoint = {"version": 1, "type": "compaction", "id": "prepared-checkpoint",
                  "summary": "continuity", "first_kept_entry_id": session.messages[-2]["_entry_id"],
                  "source_end_id": session.messages[-1]["_entry_id"]}
    prepare = AsyncMock(return_value=checkpoint)
    with patch("nanobot.session.compaction.CompactionEngine.prepare", prepare):
        assert await consolidator.compact_session(session) == "continuity"
    snapshot = RequestBudget.freeze(provider, "physical-model", 8192, 256)
    assert prepare.await_args.kwargs["recent_tokens"] == int(snapshot.input_limit * ratio)
    assert session.last_consolidated == 0
    assert len(session.messages) == 17


def test_ratio_propagated_from_config_schema() -> None:
    defaults = AgentDefaults()
    assert defaults.consolidation_ratio == 0.5
    defaults = AgentDefaults.model_validate({"consolidationRatio": 0.3})
    assert defaults.consolidation_ratio == 0.3
    assert defaults.model_dump(by_alias=True)["consolidationRatio"] == 0.3


def test_ratio_validation_rejects_out_of_range() -> None:
    with pytest.raises(ValidationError):
        AgentDefaults(consolidation_ratio=0.05)
    with pytest.raises(ValidationError):
        AgentDefaults(consolidation_ratio=1.0)
