"""Success-output suppression rules for cron/background turns.

Covers cron_suppress_success_delivery: silent flag + [SILENT] marker, cron
turns only, never normal chat, exact marker match only.
"""

from __future__ import annotations

from nanobot.cron.session_turns import (
    CRON_SILENT_MARKER,
    CRON_SILENT_META,
    CRON_TRIGGER_META,
    cron_suppress_success_delivery,
)


def _cron_meta(**extra) -> dict:
    return {CRON_TRIGGER_META: {"job_id": "j1", "run_id": "r1"}, **extra}


def test_silent_flag_suppresses_cron_turn() -> None:
    meta = _cron_meta(**{CRON_SILENT_META: True})
    assert cron_suppress_success_delivery(meta, "No output needed — job completed successfully.")


def test_silent_marker_exact_match_suppresses() -> None:
    assert cron_suppress_success_delivery(_cron_meta(), "  [SILENT]\n")


def test_silent_marker_not_substring_matched() -> None:
    # Fuzzy/substring matching would eat valid replies — must be exact trimmed.
    assert not cron_suppress_success_delivery(_cron_meta(), "Done. [SILENT] for now.")
    assert not cron_suppress_success_delivery(_cron_meta(), "[silent]")
    assert not cron_suppress_success_delivery(_cron_meta(), "[SILENT] more text")


def test_normal_cron_turn_with_real_text_delivers() -> None:
    assert not cron_suppress_success_delivery(_cron_meta(), "Upstream released v2.6 — TTS changes!")


def test_non_cron_turn_never_suppressed_even_with_marker() -> None:
    # Normal user chat containing [SILENT] must still be delivered.
    assert not cron_suppress_success_delivery({}, "[SILENT]")
    assert not cron_suppress_success_delivery(None, "[SILENT]")


def test_silent_flag_ignored_off_chat() -> None:
    # A stray silent meta on a non-cron turn is not honored.
    assert not cron_suppress_success_delivery({CRON_SILENT_META: True}, "anything")


def test_empty_marker_constant_is_stable() -> None:
    assert CRON_SILENT_MARKER == "[SILENT]"
