"""Tests for Dream guardrails M1b (diff limits) and M1c (rollback-on-incomplete).

These exercise ``MemoryStore.run_dream`` directly with deterministic fake
"Dream runs" (async callables that mutate memory files exactly like the
restricted Dream toolset would, then return a fake response with a stop
reason). No LLM or AgentLoop is involved.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from nanobot.agent.memory import MemoryStore

SOUL = "# Soul\n- Helpful"
MEMORY = "# Memory\n- Project X active"


def _completed_resp(**metadata):
    return SimpleNamespace(
        metadata={"_stop_reason": "completed", **metadata},
        content="dream summary",
    )


def _soul(store) -> str:
    return (store.workspace / "SOUL.md").read_text(encoding="utf-8")


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(tmp_path)
    s.write_soul(SOUL)
    s.write_memory(MEMORY)
    assert s.git.init() is True  # fresh repo with one init commit
    s.append_history("first conversation entry")
    return s


async def _run_into(store, edits, resp, **kwargs):
    """Run a guarded Dream pass whose fake agent applies ``edits`` then returns ``resp``."""

    async def run(prompt):
        edits()
        return resp

    return await store.run_dream(run, **kwargs)


class TestSuccessfulRun:
    async def test_small_diff_commits_and_advances_cursor(self, store):
        _, last_cursor = store.build_dream_prompt()
        assert last_cursor > 0

        outcome = await _run_into(
            store,
            edits=lambda: (store.workspace / "SOUL.md").write_text(
                "# Soul\n- More helpful", encoding="utf-8",
            ),
            resp=_completed_resp(),
            max_batch_size=20, max_iterations=10, timeout_s=30,
            max_changed_files=8, max_diff_chars=32_000,
            model_label="dream-model",
        )

        assert outcome.completed is True
        assert outcome.reason == "completed"
        assert outcome.changed_files == ["SOUL.md"]
        assert outcome.diff_chars > 0
        assert outcome.commit_sha is not None
        assert store.get_last_dream_cursor() == last_cursor
        # The commit exists and the working tree is clean afterwards.
        commits = store.git.log()
        assert len(commits) == 2
        assert commits[0].message.startswith("dream:")
        assert store.git.auto_commit("noop") is None
        # Observability: metadata attached to the response.
        assert outcome.stop_reason == "completed"

    async def test_cursor_commit_includes_cursor_file(self, store):
        _, last_cursor = store.build_dream_prompt()

        async def run(prompt):
            return _completed_resp()

        outcome = await store.run_dream(
            run, max_batch_size=20, max_iterations=10, timeout_s=30,
            max_changed_files=8, max_diff_chars=32_000, model_label="m",
        )

        assert outcome.completed
        assert store.get_last_dream_cursor() == last_cursor
        assert outcome.commit_sha is not None
        info, diff = store.git.show_commit_diff(outcome.commit_sha)
        assert "memory/.dream_cursor" in diff

    async def test_nothing_to_process_does_not_run(self, tmp_path):
        # Empty store: no history at all.
        fresh = MemoryStore(tmp_path)
        called = False

        async def run(prompt):
            nonlocal called
            called = True
            return _completed_resp()

        outcome = await fresh.run_dream(
            run, max_batch_size=20, max_iterations=10, timeout_s=30,
            max_changed_files=8, max_diff_chars=32_000, model_label="m",
        )

        assert outcome.reason == "nothing_to_process"
        assert outcome.completed is False
        assert called is False
        assert fresh.get_last_dream_cursor() == 0


class TestMaxChangedFiles:
    async def test_too_many_files_is_incomplete_no_commit_cursor_unchanged(self, store):
        def edits():
            (store.workspace / "SOUL.md").write_text("# Soul\n- Changed", encoding="utf-8")
            for i in range(8):  # 1 + 8 = 9 files > max_changed_files=8
                p = store.workspace / "memory" / f"topic-{i}.md"
                p.write_text("new memory file", encoding="utf-8")

        outcome = await _run_into(
            store, edits, _completed_resp(),
            max_batch_size=20, max_iterations=10, timeout_s=30,
            max_changed_files=8, max_diff_chars=32_000, model_label="m",
        )

        assert outcome.completed is False
        assert outcome.reason == "max_changed_files"
        assert len(outcome.changed_files) == 9
        assert outcome.commit_sha is None
        assert len(store.git.log()) == 1  # only init commit — nothing committed
        assert store.get_last_dream_cursor() == 0
        assert _soul(store) == SOUL  # memory restored
        assert list((store.workspace / "memory").glob("topic-*.md")) == []

    async def test_exactly_at_limit_passes(self, store):
        def edits():
            (store.workspace / "SOUL.md").write_text("# Soul\n- Changed", encoding="utf-8")
            for i in range(6):  # 1 + 6 = 7 files <= max_changed_files=8
                p = store.workspace / "memory" / f"topic-{i}.md"
                p.write_text("new memory file", encoding="utf-8")

        outcome = await _run_into(
            store, edits, _completed_resp(),
            max_batch_size=20, max_iterations=10, timeout_s=30,
            max_changed_files=8, max_diff_chars=32_000, model_label="m",
        )

        assert outcome.completed is True
        assert outcome.commit_sha is not None
        assert store.get_last_dream_cursor() > 0


class TestMaxDiffChars:
    async def test_too_large_diff_is_incomplete_no_commit_cursor_unchanged(self, store):
        big = ("# Soul\n- " + "x" * 4_000 + "\n")  # far above max_diff_chars=1_000

        def edits():
            (store.workspace / "SOUL.md").write_text(big, encoding="utf-8")

        outcome = await _run_into(
            store, edits, _completed_resp(),
            max_batch_size=20, max_iterations=10, timeout_s=30,
            max_changed_files=8, max_diff_chars=1_000, model_label="m",
        )

        assert outcome.completed is False
        assert outcome.reason == "max_diff_chars"
        assert outcome.changed_files == ["SOUL.md"]
        assert outcome.diff_chars > 1_000
        assert outcome.commit_sha is None
        assert len(store.git.log()) == 1
        assert store.get_last_dream_cursor() == 0
        assert _soul(store) == SOUL


class TestRollbackOnIncomplete:
    async def test_timeout_restores_memory_and_leaves_cursor(self, store):
        async def run(prompt):
            (store.workspace / "SOUL.md").write_text("# Soul\n- Edited before timeout", encoding="utf-8")
            (store.workspace / "memory" / "partial.md").write_text("partial write", encoding="utf-8")
            await asyncio.sleep(5)  # never finishes

        outcome = await store.run_dream(
            run, max_batch_size=20, max_iterations=10, timeout_s=0.05,
            max_changed_files=8, max_diff_chars=32_000, model_label="m",
        )

        assert outcome.completed is False
        assert outcome.reason == "timeout"
        assert store.get_last_dream_cursor() == 0
        assert _soul(store) == SOUL
        assert not (store.workspace / "memory" / "partial.md").exists()
        assert store.git.auto_commit("noop") is None  # git tree clean after rollback

    async def test_exception_restores_memory_and_leaves_cursor(self, store):
        async def run(prompt):
            (store.workspace / "SOUL.md").write_text("# Soul\n- Edited before crash", encoding="utf-8")
            raise RuntimeError("boom")

        outcome = await store.run_dream(
            run, max_batch_size=20, max_iterations=10, timeout_s=30,
            max_changed_files=8, max_diff_chars=32_000, model_label="m",
        )

        assert outcome.completed is False
        assert outcome.reason == "exception"
        assert outcome.error == "boom"
        assert store.get_last_dream_cursor() == 0
        assert _soul(store) == SOUL

    async def test_max_iterations_stop_reason_restores_memory_and_leaves_cursor(self, store):
        def edits():
            (store.workspace / "USER.md").write_text("# User\n- Someone", encoding="utf-8")
            (store.workspace / "memory" / "MEMORY.md").write_text(
                "# Memory\n- Changed", encoding="utf-8",
            )

        resp = SimpleNamespace(metadata={"_stop_reason": "max_iterations"})
        outcome = await _run_into(
            store, edits, resp,
            max_batch_size=20, max_iterations=10, timeout_s=30,
            max_changed_files=8, max_diff_chars=32_000, model_label="m",
        )

        assert outcome.completed is False
        assert outcome.reason == "max_iterations"
        assert outcome.stop_reason == "max_iterations"
        assert store.get_last_dream_cursor() == 0
        assert (store.workspace / "USER.md").read_text(encoding="utf-8") == ""
        assert (store.workspace / "memory" / "MEMORY.md").read_text(encoding="utf-8") == MEMORY
        assert len(store.git.log()) == 1

    async def test_untracked_new_memory_file_removed_on_rollback(self, store):
        async def run(prompt):
            (store.workspace / "memory" / "new-topic.md").write_text(
                "created during dream", encoding="utf-8",
            )
            return SimpleNamespace(metadata={"_stop_reason": "max_iterations"})

        outcome = await store.run_dream(
            run, max_batch_size=20, max_iterations=10, timeout_s=30,
            max_changed_files=8, max_diff_chars=32_000, model_label="m",
        )

        assert outcome.completed is False
        assert not (store.workspace / "memory" / "new-topic.md").exists()
        assert store.get_last_dream_cursor() == 0

    async def test_pre_existing_untracked_file_survives_rollback(self, store):
        """Byte snapshots restore only what changed; pre-existing dirty files stay."""
        pre_existing = store.workspace / "memory" / "user-notes.md"
        pre_existing.write_text("user's own note", encoding="utf-8")

        async def run(prompt):
            (pre_existing).write_text("do NOT clobber this", encoding="utf-8")  # edited by Dream
            (store.workspace / "memory" / "dream-created.md").write_text("x", encoding="utf-8")
            return SimpleNamespace(metadata={"_stop_reason": "max_iterations"})

        outcome = await store.run_dream(
            run, max_batch_size=20, max_iterations=10, timeout_s=30,
            max_changed_files=8, max_diff_chars=32_000, model_label="m",
        )

        assert outcome.completed is False
        assert pre_existing.read_text(encoding="utf-8") == "user's own note"
        assert not (store.workspace / "memory" / "dream-created.md").exists()

    async def test_rollback_without_git_repo_restores_bytes(self, tmp_path):
        """The byte-snapshot fallback must work when the workspace has no git repo."""
        s = MemoryStore(tmp_path)
        s.write_soul(SOUL)
        s.write_memory(MEMORY)  # note: no git init
        s.append_history("entry")

        async def run(prompt):
            (tmp_path / "SOUL.md").write_text("# Soul\n- Edited", encoding="utf-8")
            (tmp_path / "memory" / "new.md").write_text("new", encoding="utf-8")
            return SimpleNamespace(metadata={"_stop_reason": "max_iterations"})

        outcome = await s.run_dream(
            run, max_batch_size=20, max_iterations=10, timeout_s=30,
            max_changed_files=8, max_diff_chars=32_000, model_label="m",
        )

        assert outcome.completed is False
        assert (tmp_path / "SOUL.md").read_text(encoding="utf-8") == SOUL
        assert not (tmp_path / "memory" / "new.md").exists()
        assert s.get_last_dream_cursor() == 0


class TestObservability:
    def _fake_session_manager(self):
        saved = []

        class _Fake:
            def get_or_create(self, key):
                return SimpleNamespace(metadata={})

            def save(self, session):
                saved.append(session)

        return _Fake(), saved

    async def test_incomplete_reason_in_response_and_session_metadata(self, store):
        resp = SimpleNamespace(metadata={"_stop_reason": "max_iterations"})
        manager, saved = self._fake_session_manager()

        async def run(prompt):
            (store.workspace / "SOUL.md").write_text("# Soul\n- Changed", encoding="utf-8")
            return resp

        outcome = await store.run_dream(
            run, max_batch_size=7, max_iterations=5, timeout_s=120,
            max_changed_files=8, max_diff_chars=32_000, model_label="dream-model",
            session_key="dream:20260831-120000", session_manager=manager,
        )

        assert outcome.completed is False
        assert resp.metadata["_dream_run"]["reason"] == "max_iterations"
        assert resp.metadata["_dream_run"]["result"] == "incomplete"
        assert len(saved) == 1
        meta = saved[0].metadata["_last_dream_run"]
        assert meta["reason"] == "max_iterations"
        assert meta["model"] == "dream-model"
        assert meta["batch_size"] == 7
        assert meta["max_iterations"] == 5
        assert meta["timeout_s"] == 120

    async def test_success_metadata_recorded(self, store):
        resp = _completed_resp()
        manager, saved = self._fake_session_manager()

        async def run(prompt):
            (store.workspace / "SOUL.md").write_text("# Soul\n- More helpful", encoding="utf-8")
            return resp

        outcome = await store.run_dream(
            run, max_batch_size=20, max_iterations=10, timeout_s=30,
            max_changed_files=8, max_diff_chars=32_000, model_label="dream-model",
            session_key="dream:20260831-120001", session_manager=manager,
        )

        assert outcome.completed is True
        assert resp.metadata["_dream_run"]["result"] == "completed"
        assert resp.metadata["_dream_run"]["changed_files"] == ["SOUL.md"]
        assert resp.metadata["_dream_run"]["commit_sha"] == outcome.commit_sha
        assert saved[0].metadata["_last_dream_run"]["reason"] == "completed"

    async def test_failed_run_restores_previous_cursor_not_zero(self, store):
        """M1c: a failure after a previous success restores the old cursor value."""
        _, first_cursor = store.build_dream_prompt()

        async def ok(prompt):
            (store.workspace / "SOUL.md").write_text("# Soul\n- More helpful", encoding="utf-8")
            return _completed_resp()

        o1 = await store.run_dream(
            ok, max_batch_size=20, max_iterations=10, timeout_s=30,
            max_changed_files=8, max_diff_chars=32_000, model_label="m",
        )
        assert o1.completed
        assert store.get_last_dream_cursor() == first_cursor

        store.append_history("second entry")
        big = "# Soul\n- " + "x" * 4_000

        async def bad(prompt):
            (store.workspace / "SOUL.md").write_text(big, encoding="utf-8")
            return _completed_resp()

        o2 = await store.run_dream(
            bad, max_batch_size=20, max_iterations=10, timeout_s=30,
            max_changed_files=8, max_diff_chars=1_000, model_label="m",
        )
        assert o2.completed is False
        assert store.get_last_dream_cursor() == first_cursor  # not 0, not advanced
        assert (store.workspace / "SOUL.md").read_text(encoding="utf-8") == "# Soul\n- More helpful"
        assert len(store.git.log()) == 2  # init + first run only

    async def test_null_response_is_incomplete_and_rolls_back(self, store):
        async def run(prompt):
            (store.workspace / "memory" / "orphan.md").write_text("x", encoding="utf-8")
            return None  # process_direct returned no outbound message

        outcome = await store.run_dream(
            run, max_batch_size=20, max_iterations=10, timeout_s=30,
            max_changed_files=8, max_diff_chars=32_000, model_label="m",
        )

        assert outcome.completed is False
        assert outcome.reason == "incomplete"
        assert not (store.workspace / "memory" / "orphan.md").exists()
        assert store.get_last_dream_cursor() == 0
