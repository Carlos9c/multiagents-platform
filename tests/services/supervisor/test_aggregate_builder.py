"""
Tests for aggregate_builder.py — frequency table and text corpus.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from app.services.supervisor.aggregate_builder import (
    _MAX_CORPUS_CHARS,
    build_frequency_table,
    build_text_corpus,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_report(
    *,
    project_id: int = 1,
    report_id: int = 1,
    overall_verdict: str = "healthy",
    agent_evals: list[dict] | None = None,
    synthesis: str = "Synthesis text for this project.",
) -> MagicMock:
    report = MagicMock()
    report.project_id = project_id
    report.report_id = report_id
    report.overall_verdict = overall_verdict
    report.synthesis = synthesis

    evals = []
    for ev_dict in agent_evals or []:
        ev = MagicMock()
        ev.agent_name = ev_dict["agent_name"]
        ev.verdict = ev_dict.get("verdict")
        ev.findings = ev_dict.get("findings", "")
        ev.issues = ev_dict.get("issues", [])
        ev.system_versions_seen = ev_dict.get("system_versions_seen", [])
        evals.append(ev)
    report.agent_evaluations = evals
    return report


# ---------------------------------------------------------------------------
# build_frequency_table
# ---------------------------------------------------------------------------


def test_frequency_table_empty_reports():
    assert build_frequency_table([]) == []


def test_frequency_table_all_not_supervised():
    reports = [_make_report(project_id=i, report_id=i, agent_evals=[]) for i in range(3)]
    table = build_frequency_table(reports)
    assert len(table) > 0
    for entry in table:
        assert entry["not_supervised_pct"] == 100
        assert entry["degraded_pct"] == 0


def test_frequency_table_counts_degraded():
    reports = [
        _make_report(
            project_id=i,
            report_id=i,
            agent_evals=[{"agent_name": "planner", "verdict": "degraded"}],
        )
        for i in range(4)
    ]
    table = build_frequency_table(reports)
    planner = next(e for e in table if e["agent_name"] == "planner")
    assert planner["degraded_pct"] == 100
    assert planner["degraded_count"] == 4
    assert planner["total_projects"] == 4


def test_frequency_table_mixed_verdicts():
    reports = [
        _make_report(
            project_id=0,
            report_id=0,
            agent_evals=[{"agent_name": "orchestrator", "verdict": "degraded"}],
        ),
        _make_report(
            project_id=1,
            report_id=1,
            agent_evals=[{"agent_name": "orchestrator", "verdict": "needs_attention"}],
        ),
        _make_report(
            project_id=2,
            report_id=2,
            agent_evals=[{"agent_name": "orchestrator", "verdict": "healthy"}],
        ),
        _make_report(
            project_id=3,
            report_id=3,
            agent_evals=[],  # not supervised
        ),
    ]
    table = build_frequency_table(reports)
    orch = next(e for e in table if e["agent_name"] == "orchestrator")
    assert orch["degraded_pct"] == 25
    assert orch["needs_attention_pct"] == 25
    assert orch["healthy_pct"] == 25
    assert orch["not_supervised_pct"] == 25


def test_frequency_table_sorted_by_degradation_desc():
    reports = [
        _make_report(
            project_id=i,
            report_id=i,
            agent_evals=[
                {"agent_name": "planner", "verdict": "healthy"},
                {"agent_name": "orchestrator", "verdict": "degraded"},
            ],
        )
        for i in range(5)
    ]
    table = build_frequency_table(reports)
    agent_names = [e["agent_name"] for e in table]
    # orchestrator (100% degraded) should come before planner (0% degraded)
    assert agent_names.index("orchestrator") < agent_names.index("planner")


def test_frequency_table_percentages_sum_to_100():
    reports = [
        _make_report(
            project_id=i,
            report_id=i,
            agent_evals=[{"agent_name": "planner", "verdict": "degraded"}],
        )
        for i in range(3)
    ] + [
        _make_report(
            project_id=3,
            report_id=3,
            agent_evals=[{"agent_name": "planner", "verdict": "healthy"}],
        )
    ]
    table = build_frequency_table(reports)
    planner = next(e for e in table if e["agent_name"] == "planner")
    total = (
        planner["degraded_pct"]
        + planner["needs_attention_pct"]
        + planner["healthy_pct"]
        + planner["not_supervised_pct"]
    )
    assert total == 100


# ---------------------------------------------------------------------------
# build_text_corpus
# ---------------------------------------------------------------------------


def test_corpus_empty_when_all_healthy():
    reports = [
        _make_report(
            project_id=i,
            report_id=i,
            overall_verdict="healthy",
            agent_evals=[{"agent_name": "planner", "verdict": "healthy", "findings": "All good."}],
        )
        for i in range(3)
    ]
    corpus = build_text_corpus(reports)
    # No degraded or needs_attention agents → corpus may be empty or contain only headers
    assert "degraded]" not in corpus
    assert "needs_attention]" not in corpus


def test_corpus_includes_degraded_findings():
    report = _make_report(
        project_id=1,
        report_id=1,
        overall_verdict="degraded",
        synthesis="Project had serious issues.",
        agent_evals=[
            {
                "agent_name": "orchestrator",
                "verdict": "degraded",
                "findings": "Budget exhausted in all runs.",
                "issues": ["Budget exhausted"],
            }
        ],
    )
    corpus = build_text_corpus([report] * 5)  # repeat to avoid min-project issues
    assert "orchestrator" in corpus
    assert "Budget exhausted" in corpus
    assert "degraded]" in corpus


def test_corpus_includes_synthesis_preview():
    report = _make_report(
        project_id=1,
        report_id=1,
        overall_verdict="degraded",
        synthesis="UNIQUE_SYNTHESIS_CONTENT_XYZ",
        agent_evals=[
            {"agent_name": "orchestrator", "verdict": "degraded", "findings": "Bad.", "issues": []}
        ],
    )
    corpus = build_text_corpus([report] * 5)
    assert "UNIQUE_SYNTHESIS_CONTENT_XYZ" in corpus


def test_corpus_truncated_when_too_large():
    long_findings = "X" * 10_000
    report = _make_report(
        project_id=1,
        report_id=1,
        overall_verdict="degraded",
        synthesis="Synthesis.",
        agent_evals=[
            {"agent_name": "orchestrator", "verdict": "degraded", "findings": long_findings}
        ],
    )
    corpus = build_text_corpus([report] * 10)
    assert len(corpus) <= _MAX_CORPUS_CHARS + 100  # small buffer for truncation marker


def test_corpus_degraded_before_needs_attention():
    reports = [
        _make_report(
            project_id=1,
            report_id=1,
            overall_verdict="degraded",
            agent_evals=[
                {"agent_name": "orchestrator", "verdict": "degraded", "findings": "Critical issue."}
            ],
        ),
        _make_report(
            project_id=2,
            report_id=2,
            overall_verdict="needs_attention",
            agent_evals=[
                {
                    "agent_name": "planner",
                    "verdict": "needs_attention",
                    "findings": "Minor concern.",
                }
            ],
        ),
    ] * 3  # repeat to get 6 total
    corpus = build_text_corpus(reports)
    pos_degraded = corpus.find("Critical issue.")
    pos_needs = corpus.find("Minor concern.")
    assert pos_degraded < pos_needs
