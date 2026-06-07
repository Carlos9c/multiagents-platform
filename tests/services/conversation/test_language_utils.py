"""Tests for language_utils — language detection from conversation history and text."""

from __future__ import annotations

from app.services.conversation.language_utils import (
    ConversationTurn,
    detect_language,
    detect_language_from_text,
)


def _turns(*pairs: tuple[str, str]) -> list[ConversationTurn]:
    return [ConversationTurn(role=r, content=c) for r, c in pairs]


class TestDetectLanguage:
    def test_spanish_from_unique_chars(self):
        history = _turns(("user", "Quiero construir una aplicación con autenticación"))
        assert detect_language(history) == "Spanish"

    def test_spanish_from_inverted_punctuation(self):
        assert detect_language(_turns(("user", "¿Puedes ayudarme?"))) == "Spanish"

    def test_spanish_from_enie(self):
        assert detect_language(_turns(("user", "El sistema gestiona la información"))) == "Spanish"

    def test_portuguese_from_tilde_a(self):
        assert (
            detect_language(_turns(("user", "Quero uma solução com autenticação"))) == "Portuguese"
        )

    def test_portuguese_from_tilde_o(self):
        assert detect_language(_turns(("user", "O sistema de gestão de tarefas"))) == "Portuguese"

    def test_german_from_sharp_s(self):
        # ß is unambiguously German
        assert (
            detect_language(_turns(("user", "Das System soll eine Straße enthalten"))) == "German"
        )

    def test_german_from_umlaut(self):
        # ä/ö are in _GERMAN_UNIQUE (ü omitted to avoid overlap with Spanish loanwords)
        assert detect_language(_turns(("user", "Benutzer müssen sich anmelden können"))) == "German"

    def test_french_from_oe_ligature(self):
        # œ is unambiguously French
        assert detect_language(_turns(("user", "Le système avec un cœur logique"))) == "French"

    def test_french_from_circumflex(self):
        # ê is unambiguously French
        assert (
            detect_language(_turns(("user", "Le projet doit être bien conçu et sûr"))) == "French"
        )

    def test_italian_from_grave_accent(self):
        assert (
            detect_language(_turns(("user", "Voglio un'applicazione con autenticità"))) == "Italian"
        )

    def test_english_as_fallback_for_ascii(self):
        assert detect_language(_turns(("user", "I want to build a REST API"))) == "English"

    def test_empty_history_returns_english(self):
        assert detect_language([]) == "English"

    def test_assistant_messages_ignored(self):
        history = _turns(
            ("assistant", "¡Hola! ¿Qué proyecto quieres construir?"),
            ("user", "I want a simple CLI tool"),
        )
        # Last user message is English — assistant Spanish chars must be ignored
        assert detect_language(history) == "English"

    def test_scans_from_most_recent_user_message(self):
        history = _turns(
            ("user", "I want a web app"),
            ("assistant", "Sure! What tech stack?"),
            ("user", "Quiero usar FastAPI con autenticación JWT"),
        )
        assert detect_language(history) == "Spanish"

    def test_only_system_messages_returns_english(self):
        history = _turns(
            ("system", "Task blocked: auth module"),
            ("system", "Workflow resumed"),
        )
        assert detect_language(history) == "English"

    def test_user_with_no_fingerprint_falls_through_to_english(self):
        assert detect_language(_turns(("user", "hello world"))) == "English"


class TestDetectLanguageFromText:
    def test_spanish_text(self):
        assert detect_language_from_text("Quiero autenticación con JWT") == "Spanish"

    def test_portuguese_text(self):
        assert detect_language_from_text("Preciso de autenticação") == "Portuguese"

    def test_german_text(self):
        # ß unambiguously German
        assert detect_language_from_text("Das System muss eine Straße enthalten") == "German"

    def test_french_text(self):
        # ê unambiguously French
        assert detect_language_from_text("Le projet doit être bien structuré") == "French"

    def test_italian_text(self):
        assert detect_language_from_text("Il sistema con autenticità") == "Italian"

    def test_english_fallback(self):
        assert detect_language_from_text("Use Redis for caching") == "English"

    def test_empty_string_returns_english(self):
        assert detect_language_from_text("") == "English"


class TestOutputLanguagePrefixInjection:
    """Verify that the OUTPUT LANGUAGE prefix reaches evaluator prompts."""

    def test_review_evaluator_prefix_injected(self):
        from unittest.mock import MagicMock, patch

        from app.services.conversation.review_evaluator import (
            BlockedTaskContext,
            ReviewEvaluatorInput,
            evaluate_review,
        )

        inp = ReviewEvaluatorInput(
            project_goal="Build an API",
            blocked_task=BlockedTaskContext(
                task_id=1,
                title="Setup DB",
                description="desc",
                validation_notes=None,
            ),
            episode_history=[ConversationTurn(role="user", content="Quiero PostgreSQL")],
            language="Spanish",
        )

        mock_provider = MagicMock()
        mock_provider.generate_structured.return_value = {
            "status": "insufficient",
            "next_question": "¿Qué versión de PostgreSQL?",
            "clarification_summary": None,
            "action_summary": None,
            "reasoning": "r",
        }

        captured_prompt: list[str] = []

        def _capture(**kwargs):
            captured_prompt.append(kwargs.get("user_prompt", ""))
            return mock_provider.generate_structured.return_value

        mock_provider.generate_structured.side_effect = _capture

        with patch(
            "app.services.conversation.review_evaluator.get_llm_provider",
            return_value=mock_provider,
        ):
            evaluate_review(inp)

        assert "OUTPUT LANGUAGE: Spanish" in captured_prompt[0]

    def test_confirmation_evaluator_prefix_injected(self):
        from unittest.mock import MagicMock, patch

        from app.services.conversation.confirmation_evaluator import (
            ConfirmationEvaluatorInput,
            evaluate_confirmation,
        )

        inp = ConfirmationEvaluatorInput(
            action_summary="Use PostgreSQL",
            user_response="Sí, adelante",
            language="Spanish",
        )

        mock_provider = MagicMock()
        mock_provider.generate_structured.return_value = {
            "confirmed": True,
            "follow_up": None,
            "reasoning": "r",
        }

        captured_prompt: list[str] = []

        def _capture(**kwargs):
            captured_prompt.append(kwargs.get("user_prompt", ""))
            return mock_provider.generate_structured.return_value

        mock_provider.generate_structured.side_effect = _capture

        with patch(
            "app.services.conversation.confirmation_evaluator.get_llm_provider",
            return_value=mock_provider,
        ):
            evaluate_confirmation(inp)

        assert "OUTPUT LANGUAGE: Spanish" in captured_prompt[0]
