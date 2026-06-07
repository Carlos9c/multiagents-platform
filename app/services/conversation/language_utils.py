"""Shared language detection utilities for the Aria conversational layer.

Provides a heuristic that detects the dominant language of a conversation from
character-set fingerprints, covering the six most common European languages.
Results are used to inject an explicit OUTPUT LANGUAGE prefix into evaluator
prompts so LLMs never have to re-detect the language from context.

Detection order (from most-unique to least-unique char sets):
  Spanish   — ¿ ¡ ñ (unambiguous)
  Portuguese — ã õ (without the Spanish-unique chars above)
  German    — ß (sharp s — unambiguous)
  French    — œ ê (without ã/õ)
  Italian   — à è ì ò ù (without the more-specific chars above)
  English   — ASCII fallback
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# ── Character fingerprints ────────────────────────────────────────────────────

# Detection fingerprints — ordered to minimise false positives.
# Step 1: unambiguous Spanish markers (¿¡ñ)
_SPANISH_UNIQUE = frozenset("¿¡ñÑ")
# Step 2-5: language-specific chars checked before the shared Spanish vowels
_GERMAN_UNIQUE = frozenset("ßẞäöÄÖ")  # ü omitted (shared with Spanish loanwords)
_FRENCH_UNIQUE = frozenset("œêëŒÊËùÙ")  # é/è omitted (shared); œ/ê/ë are truly French
_PORTUGUESE_UNIQUE = frozenset("ãõÃÕ")
_ITALIAN_UNIQUE = frozenset("àìÀÌ")  # à/ì clearly Italian; è/ò omitted (shared)
# Step 6: common Spanish accented vowels — broad net, fires after other languages checked
_SPANISH_ACCENTS = frozenset("áéíóúÁÉÍÓÚüÜ")

# ── Public types ──────────────────────────────────────────────────────────────

SupportedLanguage = Literal["Spanish", "Portuguese", "German", "French", "Italian", "English"]


@dataclass
class ConversationTurn:
    """Lightweight turn representation for language detection.

    Mirrors the ConversationTurn in requirements_evaluator.py so callers
    can use either — both are structurally identical.
    """

    role: Literal["user", "assistant", "system"]
    content: str


# ── Detection logic ───────────────────────────────────────────────────────────


def _detect_from_text(text: str) -> SupportedLanguage | None:
    """Return a language if the text contains one of the fingerprint sets.

    Detection order:
      1. Unambiguous Spanish markers (¿ ¡ ñ)
      2. Language-specific markers for German/French/Portuguese/Italian
         (checked before the broad Spanish accents to avoid false positives)
      3. Common Spanish accented vowels (broad net — fires last before English fallback)

    Returns None if no fingerprint is found (caller falls through to English default).
    """
    # 1. Unambiguous Spanish
    if any(c in _SPANISH_UNIQUE for c in text):
        return "Spanish"
    # 2a. German-specific (ß, ä, ö)
    if any(c in _GERMAN_UNIQUE for c in text):
        return "German"
    # 2b. French-specific (œ, ê, ë)
    if any(c in _FRENCH_UNIQUE for c in text):
        return "French"
    # 2c. Portuguese-specific (ã, õ)
    if any(c in _PORTUGUESE_UNIQUE for c in text):
        return "Portuguese"
    # 2d. Italian-specific (à, ì)
    if any(c in _ITALIAN_UNIQUE for c in text):
        return "Italian"
    # 3. Common Spanish accented vowels (á é í ó ú) — catches most remaining Spanish
    if any(c in _SPANISH_ACCENTS for c in text):
        return "Spanish"
    return None


def detect_language(history: list[ConversationTurn]) -> SupportedLanguage:
    """Detect the dominant language from a list of conversation turns.

    Scans user messages from most-recent to oldest. Returns the language of
    the first user turn that matches a fingerprint, or "English" as fallback.
    Non-user messages (assistant, system) are ignored.
    """
    for turn in reversed(history):
        if turn.role == "user" and turn.content:
            lang = _detect_from_text(turn.content)
            if lang is not None:
                return lang
    return "English"


def detect_language_from_text(text: str) -> SupportedLanguage:
    """Detect language from a single text string.

    Used when only a single message or string is available (e.g. user_clarification
    in ResumptionService or query hint in QueryTool).
    """
    return _detect_from_text(text) or "English"
