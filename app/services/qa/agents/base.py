"""BaseQAAgent ABC.

All QA agents must inherit from this class and implement probe().
Each agent is responsible for a specific type of adversarial probing
(functional, security, performance, etc.).

Design rules (see AGENT_DEVELOPMENT_GUIDE.md §10):
- Category: qa_agent
- Each agent reads a YAML prompt via prompt_loader.get("qa/<name>")
- Agents do NOT write to DB — they return updated QASession
- Agents MUST call prompt_loader.validate_builder_inputs() before building prompts
- Every agent must have a supervisor evaluator reading from qa_sessions/qa_findings
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from sqlalchemy.orm import Session

from app.services.qa.contracts import QARequest
from app.services.qa.qa_session import QASession


class BaseQAAgent(ABC):
    """Abstract base for all QA probing agents."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique agent identifier. Must match the strategy's allowed_agents list."""
        ...

    @abstractmethod
    def probe(
        self,
        *,
        db: Session,
        request: QARequest,
        session: QASession,
    ) -> QASession:
        """Execute the probe and return the updated QASession.

        Agents must:
        - Append ProbeRecord(s) to session.probes for every probe attempt.
        - Append QAFindingDetail(s) to session.findings for every bug found.
        - Call session.record_agent_call(self.name) exactly once before returning.
        - Never raise exceptions for expected probe failures — record them as findings.
        - Raise QAAgentError only for unrecoverable infrastructure failures.
        """
        ...


class QAAgentError(Exception):
    """Unrecoverable infrastructure failure inside a QA agent."""
