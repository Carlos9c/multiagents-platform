"""QA strategy for Android mobile apps."""

from __future__ import annotations

from app.services.qa.strategies.base import QAStrategy


class MobileAndroidStrategy(QAStrategy):
    """Android mobile applications (Kotlin, Java, Flutter, React Native).

    Requires a compiled APK before probing. QABootstrapper ensures
    the APK exists by compiling from source if needed.
    Uses the host Android Studio emulator (ADB bridge) for device interaction.
    """

    @property
    def product_type(self) -> str:
        return "mobile_android"

    @property
    def requires_compiled_artifact(self) -> bool:
        return True

    @property
    def allowed_agents(self) -> list[str]:
        return [
            # Phase 5 — device probes (APK required)
            "apk_installer",
            "functional_tester",
            "security_scanner",
            # Phase 7-9 — source analysis probes
            "functional_qa_agent",
            "boundary_qa_agent",
            "adversarial_qa_agent",
            "security_qa_agent",
            "regression_qa_agent",
            # Synthesis
            "synthesis_agent",
        ]
