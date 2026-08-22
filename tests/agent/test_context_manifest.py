from __future__ import annotations

import json

from nanobot.agent.context_manifest import ContextManifestAssembler


def _manifest(tmp_path, payload):
    (tmp_path / "context-manifest.json").write_text(json.dumps(payload), encoding="utf-8")


def test_manifest_keeps_constitutional_rules_and_retrieves_relevant_topic(tmp_path) -> None:
    (tmp_path / "AGENTS.md").write_text("critical safety rule", encoding="utf-8")
    (tmp_path / "now.md").write_text("active project", encoding="utf-8")
    (tmp_path / "grocery.md").write_text("grocery list procedure", encoding="utf-8")
    (tmp_path / "biometrics.md").write_text("COROS biometrics procedure", encoding="utf-8")
    _manifest(tmp_path, {
        "constitutional": ["AGENTS.md"],
        "current": ["now.md"],
        "retrieved": [
            {"path": "grocery.md", "owners": ["skill:add-to-lists", "topic:grocery"]},
            {"path": "biometrics.md", "owners": ["topic:biometrics"], "keywords": ["COROS"]},
        ],
    })

    grocery = ContextManifestAssembler(tmp_path).assemble(
        "add milk to my grocery list",
        owners={"skill:add-to-lists"},
    )
    biometrics = ContextManifestAssembler(tmp_path).assemble("analyze my COROS recovery")

    assert "critical safety rule" in grocery.constitutional
    assert "active project" in grocery.current
    assert "grocery list procedure" in grocery.retrieved
    assert "COROS biometrics procedure" not in grocery.retrieved
    assert "COROS biometrics procedure" in biometrics.retrieved
    assert "grocery list procedure" not in biometrics.retrieved


def test_tier_budgets_and_provenance_are_independent(tmp_path) -> None:
    for name in ("constitution.md", "now.md", "topic.md"):
        (tmp_path / name).write_text(name[0] * 2_000, encoding="utf-8")
    _manifest(tmp_path, {
        "constitutional": ["constitution.md"],
        "current": ["now.md"],
        "retrieved": [{"path": "topic.md", "keywords": ["topic"]}],
    })
    assembler = ContextManifestAssembler(
        tmp_path,
        constitutional_budget_chars=1_000,
        current_budget_chars=1_200,
        retrieved_budget_chars=1_400,
    )

    assembly = assembler.assemble("topic")
    report = assembly.report()

    assert len(assembly.constitutional.split("\n\n", 1)[1]) == 1_000
    assert len(assembly.current.split("\n\n", 1)[1]) == 1_200
    assert len(assembly.retrieved.split("\n\n", 1)[1]) == 1_400
    assert report["totals"]["constitutional"] == {
        "characters": 1_000,
        "estimated_tokens": 250,
    }
    assert all(source["reason"] == "selected_truncated" for source in report["sources"])


def test_manifest_reports_irrelevant_missing_and_unsafe_sources(tmp_path) -> None:
    _manifest(tmp_path, {
        "retrieved": [
            {"path": "missing.md", "keywords": ["match"]},
            {"path": "../outside.md", "keywords": ["match"]},
            {"path": "ignored.md", "keywords": ["other"]},
        ],
    })

    report = ContextManifestAssembler(tmp_path).assemble("match").report()

    assert [source["reason"] for source in report["sources"]] == [
        "unavailable",
        "outside_workspace",
        "not_relevant",
    ]
