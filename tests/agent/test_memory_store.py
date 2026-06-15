"""Tests for the restructured MemoryStore — pure file I/O layer."""

import json
from datetime import datetime

import pytest

from nanobot.agent.memory import _HISTORY_ENTRY_HARD_CAP, MemoryStore


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path)


class TestMemoryStoreBasicIO:
    def test_read_memory_returns_empty_when_missing(self, store):
        assert store.read_memory() == ""

    def test_write_and_read_memory(self, store):
        store.write_memory("hello")
        assert store.read_memory() == "hello"

    def test_read_soul_returns_empty_when_missing(self, store):
        assert store.read_soul() == ""

    def test_write_and_read_soul(self, store):
        store.write_soul("soul content")
        assert store.read_soul() == "soul content"

    def test_read_user_returns_empty_when_missing(self, store):
        assert store.read_user() == ""

    def test_write_and_read_user(self, store):
        store.write_user("user content")
        assert store.read_user() == "user content"

    def test_get_memory_context_returns_empty_when_missing(self, store):
        assert store.get_memory_context() == ""

    def test_get_memory_context_returns_formatted_content(self, store):
        store.write_memory("important fact")
        ctx = store.get_memory_context()
        assert "Long-term Memory" in ctx
        assert "important fact" in ctx

    def test_get_memory_context_loads_system_files_without_frontmatter(self, store):
        system_file = store.system_dir / "corrections.md"
        system_file.write_text(
            '---\ndescription: "Behavioral corrections"\n---\n\n- Use uv, not pip.\n',
            encoding="utf-8",
        )

        ctx = store.get_memory_context()

        assert "memory/system/corrections.md" in ctx
        assert "Use uv, not pip" in ctx
        assert "description:" not in ctx

    def test_get_memory_tree_context_includes_descriptions(self, store):
        (store.memory_dir / "homelab.md").write_text(
            '---\ndescription: "Hardware, services, and network preferences."\n---\n\n# Homelab\n',
            encoding="utf-8",
        )
        (store.system_dir / "procedures.md").write_text(
            '---\ndescription: "Always-loaded operating procedures."\n---\n\n- Be brief.\n',
            encoding="utf-8",
        )

        tree = store.get_memory_tree_context()

        assert "Memory Tree" in tree
        assert "`memory/homelab.md` (topic" in tree
        assert "Hardware, services, and network preferences." in tree
        assert "`memory/system/procedures.md` (loaded" in tree
        assert "Always-loaded operating procedures." in tree

    def test_get_memory_tree_context_marks_missing_description(self, store):
        (store.memory_dir / "project.md").write_text("- fact\n", encoding="utf-8")

        tree = store.get_memory_tree_context()

        assert "`memory/project.md`" in tree
        assert "(missing description)" in tree

    def test_topic_files_discovers_nested_topic_files(self, tmp_path):
        memory_dir = tmp_path / "memory"
        (memory_dir / "projects").mkdir(parents=True)
        (memory_dir / "projects" / "nanobot.md").write_text("x", encoding="utf-8")
        (memory_dir / "system").mkdir()
        (memory_dir / "system" / "now.md").write_text("x", encoding="utf-8")

        assert "memory/projects/nanobot.md" in MemoryStore._topic_files(tmp_path)
        assert "memory/system/now.md" not in MemoryStore._topic_files(tmp_path)


class TestHistoryWithCursor:
    def test_append_history_returns_cursor(self, store):
        cursor = store.append_history("event 1")
        assert cursor == 1
        cursor2 = store.append_history("event 2")
        assert cursor2 == 2

    def test_append_history_includes_cursor_in_file(self, store):
        store.append_history("event 1")
        content = store.read_file(store.history_file)
        data = json.loads(content)
        assert data["cursor"] == 1

    def test_append_history_includes_session_key_when_provided(self, store):
        store.append_history("event 1", session_key="telegram:chat-1")
        content = store.read_file(store.history_file)
        data = json.loads(content)
        assert data["session_key"] == "telegram:chat-1"

    def test_cursor_persists_across_appends(self, store):
        store.append_history("event 1")
        store.append_history("event 2")
        cursor = store.append_history("event 3")
        assert cursor == 3

    def test_append_history_strips_thinking_content(self, store):
        """`strip_think` must run before persistence — well-formed thinking
        blocks shouldn't land in history."""
        cursor = store.append_history("<think>reasoning</think>final answer")
        content = store.read_file(store.history_file)
        data = json.loads(content)
        assert data["cursor"] == cursor
        assert data["content"] == "final answer"

    def test_append_history_drops_pure_leak_content(self, store):
        """Regression: entries that strip down to empty (pure template-token
        leak) must NOT fall back to the raw leak. Persisting the raw text
        would re-pollute context via consolidation / replay, undoing the
        protection `strip_think` provides."""
        cursor = store.append_history("<think>nothing user-facing</think>")
        content = store.read_file(store.history_file)
        data = json.loads(content)
        assert data["cursor"] == cursor
        assert data["content"] == ""

    def test_append_history_drops_malformed_leak_prefix(self, store):
        """Channel-marker / malformed opening leaks should not survive."""
        cursor = store.append_history("<channel|>")
        content = store.read_file(store.history_file)
        data = json.loads(content)
        assert data["cursor"] == cursor
        assert data["content"] == ""

    def test_read_unprocessed_history(self, store):
        store.append_history("event 1")
        store.append_history("event 2")
        store.append_history("event 3")
        entries = store.read_unprocessed_history(since_cursor=1)
        assert len(entries) == 2
        assert entries[0]["cursor"] == 2

    def test_read_unprocessed_history_returns_all_when_cursor_zero(self, store):
        store.append_history("event 1")
        store.append_history("event 2")
        entries = store.read_unprocessed_history(since_cursor=0)
        assert len(entries) == 2

    def test_prompt_history_filters_to_current_session(self, store):
        store.append_history("legacy entry without session")
        store.append_history("telegram entry", session_key="telegram:chat-1")
        store.append_history("slack entry", session_key="slack:chat-2")

        entries = store.read_recent_history_for_prompt(
            since_cursor=0,
            session_key="telegram:chat-1",
        )

        assert [e["content"] for e in entries] == ["telegram entry"]
        assert [e["content"] for e in store.read_unprocessed_history(0)] == [
            "legacy entry without session",
            "telegram entry",
            "slack entry",
        ]

    def test_unified_prompt_history_excludes_internal_cron_sessions(self, store):
        store.append_history("legacy entry without session")
        store.append_history("unified entry", session_key="unified:default")
        store.append_history("telegram entry", session_key="telegram:chat-1")
        store.append_history("cron internal entry", session_key="cron:job-1")

        entries = store.read_recent_history_for_prompt(
            since_cursor=0,
            session_key="unified:default",
            unified_session=True,
        )

        assert [e["content"] for e in entries] == [
            "legacy entry without session",
            "unified entry",
            "telegram entry",
        ]

    def test_unified_cron_prompt_history_includes_own_cron_entry(self, store):
        store.append_history("unified entry", session_key="unified:default")
        store.append_history("other cron entry", session_key="cron:job-2")
        store.append_history("own cron entry", session_key="cron:job-1")

        entries = store.read_recent_history_for_prompt(
            since_cursor=0,
            session_key="cron:job-1",
            unified_session=True,
        )

        assert [e["content"] for e in entries] == ["unified entry", "own cron entry"]

    def test_read_unprocessed_skips_entries_without_cursor(self, store):
        """Regression: entries missing the cursor key should be silently skipped."""
        store.history_file.write_text(
            '{"timestamp": "2026-04-01 10:00", "content": "no cursor"}\n'
            '{"cursor": 2, "timestamp": "2026-04-01 10:01", "content": "valid"}\n'
            '{"cursor": 3, "timestamp": "2026-04-01 10:02", "content": "also valid"}\n',
            encoding="utf-8",
        )
        entries = store.read_unprocessed_history(since_cursor=0)
        assert [e["cursor"] for e in entries] == [2, 3]

    def test_read_unprocessed_skips_malformed_history_payloads(self, store):
        """Externally edited JSONL can keep an int cursor but miss required payload fields."""
        store.history_file.write_text(
            '{"cursor": 1, "timestamp": "2026-04-01 10:00", "content": "valid"}\n'
            '{"cursor": 2, "timestamp": "2026-04-01 10:01"}\n'
            '{"cursor": 3, "content": "missing timestamp"}\n'
            '{"cursor": 4, "timestamp": "2026-04-01 10:03", "content": 123}\n'
            '{"cursor": 5, "timestamp": "2026-04-01 10:04", "content": "bad session", "session_key": 42}\n'
            '{"cursor": 6, "timestamp": "2026-04-01 10:05", "content": "also valid", "session_key": "telegram:chat-1"}\n',
            encoding="utf-8",
        )

        entries = store.read_unprocessed_history(since_cursor=0)

        assert [e["cursor"] for e in entries] == [1, 6]
        assert [e["content"] for e in entries] == ["valid", "also valid"]

    def test_next_cursor_falls_back_when_last_entry_has_no_cursor(self, store):
        """Regression: _next_cursor should not KeyError on entries without cursor."""
        store.history_file.write_text(
            '{"timestamp": "2026-04-01 10:01", "content": "no cursor"}\n',
            encoding="utf-8",
        )
        # Delete .cursor file so _next_cursor falls back to reading JSONL
        store._cursor_file.unlink(missing_ok=True)
        # Last entry has no cursor — should safely return 1, not KeyError
        cursor = store.append_history("new event")
        assert cursor == 1

    def test_append_history_allocates_unique_cursors_under_concurrent_writes(self, store):
        """Regression: concurrent appends must not allocate duplicate cursors."""
        import threading

        writers = 16
        start = threading.Barrier(writers)
        cursors: list[int] = []
        lock = threading.Lock()

        def worker(i):
            start.wait()
            c = store.append_history(f"event {i}")
            with lock:
                cursors.append(c)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(writers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(cursors) == writers
        assert len(set(cursors)) == writers, f"duplicate cursors: {sorted(cursors)}"
        assert sorted(cursors) == list(range(1, writers + 1))
        persisted = store.read_unprocessed_history(since_cursor=0)
        assert sorted(e["cursor"] for e in persisted) == list(range(1, writers + 1))

    def test_compact_history_drops_oldest(self, tmp_path):
        store = MemoryStore(tmp_path, max_history_entries=2)
        store.append_history("event 1")
        store.append_history("event 2")
        store.append_history("event 3")
        store.append_history("event 4")
        store.append_history("event 5")
        store.compact_history()
        entries = store.read_unprocessed_history(since_cursor=0)
        assert len(entries) == 2
        assert entries[0]["cursor"] in {4, 5}

    def test_write_entries_uses_atomic_write(self, tmp_path):
        """_write_entries uses temp file + os.replace for atomicity."""
        store = MemoryStore(tmp_path)
        store.append_history("event 1")
        store.append_history("event 2")
        store.append_history("event 3")
        entries = store.read_unprocessed_history(since_cursor=0)

        # Monitor temp file existence
        tmp_path_obj = store.history_file.with_suffix(".jsonl.tmp")
        assert not tmp_path_obj.exists()  # Should not exist initially

        # Call _write_entries
        store._write_entries(entries)

        # Temp file should be cleaned up
        assert not tmp_path_obj.exists()
        # Original file should exist
        assert store.history_file.exists()

    def test_write_entries_cleans_up_tmp_on_exception(self, tmp_path, monkeypatch):
        """Exception during _write_entries cleans up the temp file."""
        store = MemoryStore(tmp_path)
        store.append_history("event 1")
        entries = store.read_unprocessed_history(since_cursor=0)

        tmp_path_obj = store.history_file.with_suffix(".jsonl.tmp")

        # Mock os.replace to raise an exception
        def failing_replace(*args, **kwargs):
            raise RuntimeError("Simulated failure")

        monkeypatch.setattr('os.replace', failing_replace)

        with pytest.raises(RuntimeError):
            store._write_entries(entries)

        # Temp file should be cleaned up
        assert not tmp_path_obj.exists()

        # Original file should still exist (because replace failed)
        assert store.history_file.exists()


class TestAppendHistoryHardCap:
    """append_history has a defensive cap that catches new callers who forgot
    to set their own tighter cap. The default is intentionally larger than
    any current caller's per-call cap, so normal operation never trips it."""

    def test_oversized_entry_is_truncated(self, store):
        """An entry above _HISTORY_ENTRY_HARD_CAP is truncated before being persisted."""
        huge = "x" * (_HISTORY_ENTRY_HARD_CAP + 10_000)
        store.append_history(huge)
        entry = store.read_unprocessed_history(since_cursor=0)[0]
        assert len(entry["content"]) <= _HISTORY_ENTRY_HARD_CAP + 50

    def test_oversize_warning_is_emitted_once(self, store, caplog):
        """Repeated oversized writes should warn only on the first occurrence."""
        from loguru import logger as loguru_logger

        records: list[str] = []
        handler_id = loguru_logger.add(lambda m: records.append(m), level="WARNING")
        try:
            huge = "x" * (_HISTORY_ENTRY_HARD_CAP + 1)
            store.append_history(huge)
            store.append_history(huge)
            store.append_history(huge)
        finally:
            loguru_logger.remove(handler_id)

        oversize_warnings = [r for r in records if "exceeds" in r and "chars" in r]
        assert len(oversize_warnings) == 1

    def test_custom_max_chars_overrides_default(self, store):
        """Callers that pass max_chars should get their tighter cap applied."""
        store.append_history("a" * 500, max_chars=100)
        entry = store.read_unprocessed_history(since_cursor=0)[0]
        assert len(entry["content"]) <= 150  # 100 + "\n... (truncated)"

    def test_normal_sized_entries_unaffected(self, store):
        """The hard cap must not alter entries that fit within it."""
        msg = "normal short entry"
        store.append_history(msg)
        entry = store.read_unprocessed_history(since_cursor=0)[0]
        assert entry["content"] == msg


class TestStructuredConversationHistory:
    def test_append_conversation_history_writes_v2_record_with_preview(self, store):
        messages = [
            {
                "timestamp": "2026-06-01T10:00:00+02:00",
                "role": "user",
                "content": "hello",
            },
            {
                "timestamp": "2026-06-01T10:01:00+02:00",
                "role": "assistant",
                "content": "used a thing",
                "tools_used": ["read_file"],
            },
            {
                "timestamp": "2026-06-01T10:02:00+02:00",
                "role": "tool",
                "name": "read_file",
                "content": "[tool: read_file — 2 lines, 20 chars]",
            },
        ]

        cursor = store.append_conversation_history(
            messages,
            session_key="telegram:123",
            content="[CONV] 3 messages\npreview",
        )

        entry = store.read_unprocessed_history(since_cursor=0)[0]
        assert cursor == 1
        assert entry["schema_version"] == 2
        assert entry["kind"] == "conversation"
        assert entry["session_key"] == "telegram:123"
        assert entry["message_count"] == 3
        assert entry["start_time"] == "2026-06-01T10:00:00+02:00"
        assert entry["end_time"] == "2026-06-01T10:02:00+02:00"
        assert entry["content"] == "[CONV] 3 messages\npreview"
        assert entry["messages"][1]["tools_used"] == ["read_file"]
        assert entry["messages"][2]["name"] == "read_file"

    def test_append_conversation_history_strips_thinking_from_messages(self, store):
        store.append_conversation_history([
            {
                "timestamp": "2026-06-01T10:00:00+02:00",
                "role": "assistant",
                "content": "<think>private</think>visible",
            },
            {
                "timestamp": "2026-06-01T10:01:00+02:00",
                "role": "assistant",
                "content": "<think>only private</think>",
            },
        ])

        entry = store.read_unprocessed_history(since_cursor=0)[0]
        assert entry["message_count"] == 1
        assert entry["messages"][0]["content"] == "visible"

    def test_append_conversation_history_sanitizes_structured_tool_payloads(self, store):
        huge_args = "x" * 5000
        store.append_conversation_history([
            {"timestamp": "2026-06-01T10:00:00", "role": "user", "content": "visible user"},
            {
                "timestamp": "2026-06-01T10:01:00",
                "role": "assistant",
                "content": "visible assistant",
                "reasoning_content": "private",
                "tool_calls": [
                    {"id": "c", "type": "function", "function": {"name": "read_file", "arguments": huge_args}}
                ],
            },
            {
                "timestamp": "2026-06-01T10:02:00",
                "role": "tool",
                "tool_call_id": "c",
                "name": "read_file",
                "content": "y" * 4000,
            },
        ])

        entry = store.read_unprocessed_history(since_cursor=0)[0]
        raw = store.history_file.read_text(encoding="utf-8")
        assert "private" not in raw
        assert huge_args not in raw
        assert "y" * 1000 not in raw
        assert entry["messages"][0]["content"] == "visible user"
        assert entry["messages"][1]["content"] == "visible assistant"
        assert entry["messages"][2]["content"] == "[tool result omitted: read_file, 4000 chars]"


class TestDreamCursor:
    def test_initial_cursor_is_zero(self, store):
        assert store.get_last_dream_cursor() == 0

    def test_set_and_get_cursor(self, store):
        store.set_last_dream_cursor(5)
        assert store.get_last_dream_cursor() == 5

    def test_cursor_persists(self, store):
        store.set_last_dream_cursor(3)
        store2 = MemoryStore(store.workspace)
        assert store2.get_last_dream_cursor() == 3

    def test_git_restore_rolls_back_dream_cursor(self, tmp_path):
        store = MemoryStore(tmp_path)
        store.write_memory("before")
        store.set_last_dream_cursor(1)
        assert store.git.init() is True

        store.write_memory("after")
        store.set_last_dream_cursor(2)
        dream_sha = store.git.auto_commit("dream: update")
        assert dream_sha is not None

        store.write_memory("newer")
        store.set_last_dream_cursor(3)

        restore_sha = store.git.revert(dream_sha)

        assert restore_sha is not None
        assert store.read_memory() == "before"
        assert store.get_last_dream_cursor() == 1


class TestLegacyHistoryMigration:
    def test_read_unprocessed_history_handles_entries_without_cursor(self, store):
        """JSONL entries with cursor=1 are correctly parsed and returned."""
        store.history_file.write_text(
            '{"cursor": 1, "timestamp": "2026-03-30 14:30", "content": "Old event"}\n',
            encoding="utf-8",
        )
        entries = store.read_unprocessed_history(since_cursor=0)
        assert len(entries) == 1
        assert entries[0]["cursor"] == 1

    def test_migrates_legacy_history_md_preserving_partial_entries(self, tmp_path):
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        legacy_file = memory_dir / "HISTORY.md"
        legacy_content = (
            "[2026-04-01 10:00] User prefers dark mode.\n\n"
            "[2026-04-01 10:05] [RAW] 2 messages\n"
            "[2026-04-01 10:04] USER: hello\n"
            "[2026-04-01 10:04] ASSISTANT: hi\n\n"
            "Legacy chunk without timestamp.\n"
            "Keep whatever content we can recover.\n"
        )
        legacy_file.write_text(legacy_content, encoding="utf-8")

        store = MemoryStore(tmp_path)
        fallback_timestamp = datetime.fromtimestamp(
            (memory_dir / "HISTORY.md.bak").stat().st_mtime,
        ).strftime("%Y-%m-%d %H:%M")

        entries = store.read_unprocessed_history(since_cursor=0)
        assert [entry["cursor"] for entry in entries] == [1, 2, 3]
        assert entries[0]["timestamp"] == "2026-04-01 10:00"
        assert entries[0]["content"] == "User prefers dark mode."
        assert entries[1]["timestamp"] == "2026-04-01 10:05"
        assert entries[1]["content"].startswith("[RAW] 2 messages")
        assert "USER: hello" in entries[1]["content"]
        assert entries[2]["timestamp"] == fallback_timestamp
        assert entries[2]["content"].startswith("Legacy chunk without timestamp.")
        assert store.read_file(store._cursor_file).strip() == "3"
        assert store.read_file(store._dream_cursor_file).strip() == "3"
        assert not legacy_file.exists()
        assert (memory_dir / "HISTORY.md.bak").read_text(encoding="utf-8") == legacy_content

    def test_migrates_consecutive_entries_without_blank_lines(self, tmp_path):
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        legacy_file = memory_dir / "HISTORY.md"
        legacy_content = (
            "[2026-04-01 10:00] First event.\n"
            "[2026-04-01 10:01] Second event.\n"
            "[2026-04-01 10:02] Third event.\n"
        )
        legacy_file.write_text(legacy_content, encoding="utf-8")

        store = MemoryStore(tmp_path)

        entries = store.read_unprocessed_history(since_cursor=0)
        assert len(entries) == 3
        assert [entry["content"] for entry in entries] == [
            "First event.",
            "Second event.",
            "Third event.",
        ]

    def test_raw_archive_stays_single_entry_while_following_events_split(self, tmp_path):
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        legacy_file = memory_dir / "HISTORY.md"
        legacy_content = (
            "[2026-04-01 10:05] [RAW] 2 messages\n"
            "[2026-04-01 10:04] USER: hello\n"
            "[2026-04-01 10:04] ASSISTANT: hi\n"
            "[2026-04-01 10:06] Normal event after raw block.\n"
        )
        legacy_file.write_text(legacy_content, encoding="utf-8")

        store = MemoryStore(tmp_path)

        entries = store.read_unprocessed_history(since_cursor=0)
        assert len(entries) == 2
        assert entries[0]["content"].startswith("[RAW] 2 messages")
        assert "USER: hello" in entries[0]["content"]
        assert entries[1]["content"] == "Normal event after raw block."

    def test_nonstandard_date_headers_still_start_new_entries(self, tmp_path):
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        legacy_file = memory_dir / "HISTORY.md"
        legacy_content = (
            "[2026-03-25–2026-04-02] Multi-day summary.\n[2026-03-26/27] Cross-day summary.\n"
        )
        legacy_file.write_text(legacy_content, encoding="utf-8")

        store = MemoryStore(tmp_path)
        fallback_timestamp = datetime.fromtimestamp(
            (memory_dir / "HISTORY.md.bak").stat().st_mtime,
        ).strftime("%Y-%m-%d %H:%M")

        entries = store.read_unprocessed_history(since_cursor=0)
        assert len(entries) == 2
        assert entries[0]["timestamp"] == fallback_timestamp
        assert entries[0]["content"] == "[2026-03-25–2026-04-02] Multi-day summary."
        assert entries[1]["timestamp"] == fallback_timestamp
        assert entries[1]["content"] == "[2026-03-26/27] Cross-day summary."

    def test_existing_history_jsonl_skips_legacy_migration(self, tmp_path):
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        history_file = memory_dir / "history.jsonl"
        history_file.write_text(
            '{"cursor": 7, "timestamp": "2026-04-01 12:00", "content": "existing"}\n',
            encoding="utf-8",
        )
        legacy_file = memory_dir / "HISTORY.md"
        legacy_file.write_text("[2026-04-01 10:00] legacy\n\n", encoding="utf-8")

        store = MemoryStore(tmp_path)

        entries = store.read_unprocessed_history(since_cursor=0)
        assert len(entries) == 1
        assert entries[0]["cursor"] == 7
        assert entries[0]["content"] == "existing"
        assert legacy_file.exists()
        assert not (memory_dir / "HISTORY.md.bak").exists()

    def test_empty_history_jsonl_still_allows_legacy_migration(self, tmp_path):
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        history_file = memory_dir / "history.jsonl"
        history_file.write_text("", encoding="utf-8")
        legacy_file = memory_dir / "HISTORY.md"
        legacy_file.write_text("[2026-04-01 10:00] legacy\n\n", encoding="utf-8")

        store = MemoryStore(tmp_path)

        entries = store.read_unprocessed_history(since_cursor=0)
        assert len(entries) == 1
        assert entries[0]["cursor"] == 1
        assert entries[0]["timestamp"] == "2026-04-01 10:00"
        assert entries[0]["content"] == "legacy"
        assert not legacy_file.exists()
        assert (memory_dir / "HISTORY.md.bak").exists()

    def test_migrates_legacy_history_with_invalid_utf8_bytes(self, tmp_path):
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        legacy_file = memory_dir / "HISTORY.md"
        legacy_file.write_bytes(b"[2026-04-01 10:00] Broken \xff data still needs migration.\n\n")

        store = MemoryStore(tmp_path)

        entries = store.read_unprocessed_history(since_cursor=0)
        assert len(entries) == 1
        assert entries[0]["timestamp"] == "2026-04-01 10:00"
        assert "Broken" in entries[0]["content"]
        assert "migration." in entries[0]["content"]
