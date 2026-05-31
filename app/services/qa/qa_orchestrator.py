"""QAOrchestrator — LLM-driven adversarial probing loop.

The orchestrator drives the QA session through two phases:

  Phase 1 — Probing (LLM-driven)
    Each round: the LLM receives the current session state (findings so far,
    agents already called, budget remaining) and decides which agent to call
    next or when to stop ("finish").

    Hard constraints enforced by the orchestrator — the LLM cannot override:
    ① No consecutive calls to the same agent (no-repeat rule).
    ② Agent must be in the strategy's allowed list.
    ③ Agent must be registered.
    ④ Per-agent probe limit (max_probes_per_agent).
    ⑤ Max rounds (max_probe_rounds) — forced termination on budget exhaustion.
    ⑥ Wall-clock timeout (timeout_seconds).
    ⑦ Auto-finish when total findings ≥ max_findings.

  Phase 2 — Synthesis (deterministic)
    Calls synthesis_agent (if registered) to aggregate all findings into a
    final verdict and remediation plan.  Falls back to deterministic rules
    when synthesis_agent is absent or its internal LLM call fails.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Literal

from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from app.services.llm.factory import get_llm_provider
from app.services.prompt_loader import prompt_loader
from app.services.qa.agents.base import QAAgentError
from app.services.qa.agents.registry import QAAgentRegistry
from app.services.qa.budget import QABudget
from app.services.qa.contracts import (
    QA_VERDICT_FAILED,
    QA_VERDICT_PARTIAL,
    QA_VERDICT_PASSED,
    QARequest,
    QAResult,
)
from app.services.qa.qa_session import QASession
from app.services.qa.strategies.base import QAStrategy

logger = logging.getLogger(__name__)

_SYNTHESIS_AGENT_NAME = "synthesis_agent"
_SYSTEM_PROMPT = prompt_loader.get("qa/qa_orchestrator", "decide")


# ── LLM output schema ─────────────────────────────────────────────────────────


class OrchestratorDecision(BaseModel):
    """LLM output for each probing-round decision."""

    action: Literal["call_agent", "finish"]
    agent_name: str | None
    reasoning: str


# ── Agent catalogue (shown to LLM to aid its decisions) ───────────────────────

_AGENT_DESCRIPTIONS: dict[str, str] = {
    # Phase 5 — execution-level probes (run commands in Docker)
    "functional_tester": (
        "Tests functional correctness via source analysis and test-suite execution"
    ),
    "security_scanner": (
        "Free-form static analysis for OWASP Top 10 vulnerabilities and common security flaws"
    ),
    "contract_validator": (
        "Validates API contracts (OpenAPI / GraphQL schema) against implementation"
    ),
    "performance_profiler": (
        "Identifies performance bottlenecks via source analysis and timing measurements"
    ),
    "apk_installer": (
        "Installs and smoke-tests the Android APK on a connected emulator (mobile only)"
    ),
    # Phase 7-9 — structured analysis probes (corpus/checklist-driven + LLM analysis)
    "functional_qa_agent": (
        "Two-pass scenario-driven functional analysis: generates prioritised test scenarios "
        "from source code then evaluates each scenario for implementation correctness"
    ),
    "boundary_qa_agent": (
        "Corpus-driven boundary condition testing using a pre-defined catalogue of edge cases "
        "(null/empty inputs, integer limits, oversized payloads, special characters)"
    ),
    "adversarial_qa_agent": (
        "Generates and evaluates product-specific adversarial attack scenarios with "
        "code-level exploitability assessment"
    ),
    "security_qa_agent": (
        "Structured OWASP Top 10 (2021) checklist — assesses all 10 vulnerability categories "
        "against the source code with per-item pass/fail verdicts"
    ),
    "performance_qa_agent": (
        "Deterministic startup/response time threshold checks plus LLM-driven source code "
        "bottleneck analysis (N+1 queries, missing indexes, sync I/O in async contexts)"
    ),
    "regression_qa_agent": (
        "Semantic comparison of current findings against the previous completed QA session "
        "to surface newly introduced regressions — skipped on first run"
    ),
}


# ── Internal helpers ──────────────────────────────────────────────────────────


def _determine_verdict_from_findings(session: QASession) -> str:
    """Derive a verdict from accumulated findings (synthesis-agent fallback)."""
    if not session.findings:
        return QA_VERDICT_PASSED
    if session.budget_exhausted:
        return QA_VERDICT_PARTIAL
    has_critical_or_high = any(f.severity in ("critical", "high") for f in session.findings)
    return QA_VERDICT_FAILED if has_critical_or_high else QA_VERDICT_PARTIAL


def _build_result(session: QASession, duration_seconds: float) -> QAResult:
    """Build the final QAResult from the completed session."""
    verdict = session.synthesis_verdict or _determine_verdict_from_findings(session)
    return QAResult(
        verdict=verdict,  # type: ignore[arg-type]
        findings=session.findings,
        probes=session.probes,
        remediation_tasks=session.remediation_tasks,
        agents_called=list(dict.fromkeys(session.agents_called)),  # dedup, keep order
        duration_seconds=round(duration_seconds, 2),
        error_message=session.synthesis_error,
    )


def _format_available_agents(allowed: list[str]) -> str:
    """Serialise the allowed probing agents (minus synthesis_agent) for the LLM prompt."""
    agents = []
    for name in allowed:
        if name == _SYNTHESIS_AGENT_NAME:
            continue
        agents.append(
            {
                "name": name,
                "description": _AGENT_DESCRIPTIONS.get(name, "QA probing agent"),
            }
        )
    return json.dumps(agents, ensure_ascii=False)


def _format_probe_history(session: QASession) -> str:
    """Serialise completed probes for the LLM prompt."""
    history = [
        {
            "agent": p.agent_name,
            "outcome": p.outcome,
            "findings_count": p.findings_count or 0,
            "summary": (p.notes or "")[:120],
        }
        for p in session.probes
    ]
    return json.dumps(history, ensure_ascii=False)


# ── Orchestrator ──────────────────────────────────────────────────────────────


class QAOrchestrator:
    """Runs the LLM-driven QA probing loop for one QA session."""

    def __init__(self, *, registry: QAAgentRegistry, budget: QABudget) -> None:
        self._registry = registry
        self._budget = budget

    # ── Public entry point ────────────────────────────────────────────────────

    def run(
        self,
        *,
        db: Session,
        request: QARequest,
        strategy: QAStrategy,
    ) -> QAResult:
        start = time.monotonic()
        session = QASession(request=request)

        logger.info(
            "qa_orchestrator_start project_id=%s product_type=%s allowed_agents=%s",
            request.project_id,
            request.product_type,
            strategy.allowed_agents,
        )

        # Phase 1 — Probing (LLM-driven)
        session.transition_to_probing()
        session = self._run_probing_loop(
            db=db,
            request=request,
            session=session,
            strategy=strategy,
            start_time=start,
        )

        # Phase 2 — Synthesis (deterministic)
        session.transition_to_synthesis()
        session = self._run_synthesis(db=db, request=request, session=session)

        session.transition_to_complete()

        duration = time.monotonic() - start
        result = _build_result(session, duration_seconds=duration)

        logger.info(
            "qa_orchestrator_complete project_id=%s verdict=%s findings=%s duration=%.1fs",
            request.project_id,
            result.verdict,
            len(result.findings),
            duration,
        )
        return result

    # ── Probing loop ──────────────────────────────────────────────────────────

    def _run_probing_loop(
        self,
        *,
        db: Session,
        request: QARequest,
        session: QASession,
        strategy: QAStrategy,
        start_time: float,
    ) -> QASession:
        last_agent_called: str | None = None
        round_num = 0

        while True:
            # ── Budget guards (checked before every LLM call) ─────────────────
            elapsed = time.monotonic() - start_time
            if elapsed >= self._budget.timeout_seconds:
                session.mark_budget_exhausted()
                session.add_note(
                    f"Probing stopped: timeout after {elapsed:.1f}s "
                    f"(limit={self._budget.timeout_seconds}s)"
                )
                logger.warning(
                    "qa_orchestrator_timeout project_id=%s elapsed=%.1fs",
                    request.project_id,
                    elapsed,
                )
                break

            if round_num >= self._budget.max_probe_rounds:
                session.mark_budget_exhausted()
                session.add_note(
                    f"Probing stopped: max_probe_rounds={self._budget.max_probe_rounds} reached"
                )
                logger.warning(
                    "qa_orchestrator_max_rounds project_id=%s round=%s",
                    request.project_id,
                    round_num,
                )
                break

            if session.total_findings >= self._budget.max_findings:
                session.add_note(
                    f"Probing stopped: max_findings={self._budget.max_findings} reached "
                    f"(accumulated={session.total_findings})"
                )
                logger.info(
                    "qa_orchestrator_max_findings project_id=%s findings=%s",
                    request.project_id,
                    session.total_findings,
                )
                break

            # ── LLM decision ──────────────────────────────────────────────────
            rounds_remaining = self._budget.max_probe_rounds - round_num
            decision = self._decide(
                request=request,
                session=session,
                strategy=strategy,
                last_agent_called=last_agent_called,
                rounds_remaining=rounds_remaining,
            )

            if decision is None:
                # LLM call failed — terminate probing gracefully
                session.add_note("Probing stopped: LLM decision call failed")
                break

            logger.info(
                "qa_orchestrator_decision project_id=%s round=%s action=%s agent=%s",
                request.project_id,
                round_num,
                decision.action,
                decision.agent_name,
            )

            if decision.action == "finish":
                session.add_note(f"LLM finished probing: {decision.reasoning[:120]}")
                break

            # ── Validate the call_agent decision ──────────────────────────────
            agent_name = decision.agent_name

            if not agent_name:
                session.add_note("LLM returned call_agent with no agent_name — stopping")
                break

            if agent_name == _SYNTHESIS_AGENT_NAME:
                session.add_note(
                    "No-synthesis rule: LLM tried to call synthesis_agent during probing — skipped"
                )
                round_num += 1
                continue

            if agent_name == last_agent_called:
                session.add_note(
                    f"No-consecutive rule: {agent_name!r} was just called — skipping this round"
                )
                round_num += 1
                continue

            if not strategy.is_agent_allowed(agent_name):
                session.add_note(f"Strategy rule: {agent_name!r} not in allowed_agents — skipped")
                round_num += 1
                continue

            if session.call_count(agent_name) >= self._budget.max_probes_per_agent:
                session.add_note(
                    f"Per-agent limit: {agent_name!r} already reached "
                    f"{self._budget.max_probes_per_agent} probes — skipped"
                )
                round_num += 1
                continue

            agent = self._registry.get(agent_name)
            if agent is None:
                session.add_note(f"Registry: {agent_name!r} not registered — skipped")
                logger.debug("qa_orchestrator_agent_not_registered name=%s", agent_name)
                round_num += 1
                continue

            # ── Execute the agent probe ───────────────────────────────────────
            logger.info(
                "qa_orchestrator_calling_agent agent=%s round=%s",
                agent_name,
                round_num,
            )
            try:
                session = agent.probe(db=db, request=request, session=session)
            except QAAgentError as exc:
                session.add_note(f"Agent {agent_name} raised QAAgentError: {exc}")
                logger.warning("qa_orchestrator_agent_error agent=%s exc=%s", agent_name, exc)
            except Exception as exc:
                session.add_note(f"Agent {agent_name} raised unexpected error: {exc}")
                logger.exception("qa_orchestrator_agent_unexpected_error agent=%s", agent_name)

            last_agent_called = agent_name
            round_num += 1

        return session

    # ── LLM decision call ─────────────────────────────────────────────────────

    def _decide(
        self,
        *,
        request: QARequest,
        session: QASession,
        strategy: QAStrategy,
        last_agent_called: str | None,
        rounds_remaining: int,
    ) -> OrchestratorDecision | None:
        available_agents_json = _format_available_agents(strategy.allowed_agents)
        probe_history_json = _format_probe_history(session)

        prompt_loader.validate_builder_inputs(
            "qa/qa_orchestrator",
            "decide",
            {
                "product_type": request.product_type,
                "project_goal": request.project_goal,
                "available_agents": available_agents_json,
                "probe_history": probe_history_json,
                "last_agent_called": last_agent_called or "none",
                "findings_count": session.total_findings,
                "budget_rounds_remaining": rounds_remaining,
            },
        )

        user_prompt = (
            f"product_type: {request.product_type}\n"
            f"project_goal: {request.project_goal}\n\n"
            f"Available probing agents:\n{available_agents_json}\n\n"
            f"Probe history (completed so far):\n{probe_history_json}\n\n"
            f"Last agent called: {last_agent_called or 'none'}\n"
            f"Current total findings: {session.total_findings}\n"
            f"Budget rounds remaining: {rounds_remaining}\n\n"
            "Choose the next action."
        )

        try:
            provider = get_llm_provider()
            raw = provider.generate_structured(
                system_prompt=_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                schema_name="qa_orchestrator_decision",
                json_schema=OrchestratorDecision.model_json_schema(),
            )
            return OrchestratorDecision.model_validate(raw)
        except (ValidationError, Exception) as exc:
            logger.warning("qa_orchestrator_llm_decision_failed exc=%s", exc)
            return None

    # ── Synthesis phase ───────────────────────────────────────────────────────

    def _run_synthesis(
        self,
        *,
        db: Session,
        request: QARequest,
        session: QASession,
    ) -> QASession:
        synthesis_agent = self._registry.get(_SYNTHESIS_AGENT_NAME)
        if synthesis_agent is not None:
            logger.info(
                "qa_orchestrator_calling_synthesis project_id=%s findings=%s",
                request.project_id,
                session.total_findings,
            )
            try:
                session = synthesis_agent.probe(db=db, request=request, session=session)
            except Exception as exc:
                session.synthesis_error = str(exc)
                logger.warning(
                    "qa_orchestrator_synthesis_failed project_id=%s exc=%s",
                    request.project_id,
                    exc,
                )
        else:
            logger.warning(
                "qa_orchestrator_no_synthesis_agent project_id=%s — "
                "deriving verdict deterministically from findings",
                request.project_id,
            )
        return session
