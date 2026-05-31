"""Tests for QABootstrapper — Phase 4."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from sqlalchemy.orm import Session

from app.services.qa.contracts import QA_VERDICT_BLOCKED, QARequest
from app.services.qa.qa_bootstrapper import (
    QABootstrapper,
    _find_artifact,
    _list_source_files,
    _read_build_configs,
)
from app.services.qa.strategies.mobile_strategy import MobileAndroidStrategy
from app.services.qa.strategies.web_app_strategy import WebAppStrategy

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_request(
    product_type: str = "mobile_android", source_path: str = "/tmp/proj"
) -> QARequest:
    return QARequest(
        project_id=1,
        qa_session_id=5,
        product_type=product_type,
        project_goal="Build an Android app",
        source_path=source_path,
        workspace_path=source_path,
    )


# ── _find_artifact ────────────────────────────────────────────────────────────


def test_find_artifact_returns_none_when_no_apk(tmp_path: Path):
    result = _find_artifact(str(tmp_path), "mobile_android")
    assert result is None


def test_find_artifact_finds_apk_in_root(tmp_path: Path):
    apk = tmp_path / "app-debug.apk"
    apk.write_text("fake apk")
    result = _find_artifact(str(tmp_path), "mobile_android")
    assert result is not None
    assert result.endswith(".apk")


def test_find_artifact_finds_apk_in_nested_dir(tmp_path: Path):
    nested = tmp_path / "app" / "build" / "outputs" / "apk" / "debug"
    nested.mkdir(parents=True)
    apk = nested / "app-debug.apk"
    apk.write_text("fake apk")
    result = _find_artifact(str(tmp_path), "mobile_android")
    assert result is not None


def test_find_artifact_returns_none_for_non_android_type(tmp_path: Path):
    apk = tmp_path / "app-debug.apk"
    apk.write_text("fake apk")
    # web_app has no artifact patterns
    result = _find_artifact(str(tmp_path), "web_app")
    assert result is None


# ── _list_source_files ────────────────────────────────────────────────────────


def test_list_source_files_returns_files(tmp_path: Path):
    (tmp_path / "build.gradle").write_text("apply plugin: 'com.android.application'")
    (tmp_path / "settings.gradle").write_text("rootProject.name = 'MyApp'")
    files = _list_source_files(str(tmp_path))
    assert "build.gradle" in files
    assert "settings.gradle" in files


def test_list_source_files_excludes_dot_files(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "src").mkdir()
    files = _list_source_files(str(tmp_path))
    assert ".git" not in files
    assert "src" in files


def test_list_source_files_handles_missing_dir():
    files = _list_source_files("/nonexistent/path/xyz")
    assert files == []


# ── _read_build_configs ───────────────────────────────────────────────────────


def test_read_build_configs_reads_gradle(tmp_path: Path):
    gradle = tmp_path / "build.gradle"
    gradle.write_text("android { compileSdk 34 }")
    configs = _read_build_configs(str(tmp_path))
    assert any("build.gradle" in k for k in configs)


def test_read_build_configs_truncates_large_files(tmp_path: Path):
    readme = tmp_path / "README.md"
    readme.write_text("x" * 10000)
    configs = _read_build_configs(str(tmp_path))
    for content in configs.values():
        assert len(content) <= 3001  # _MAX_FILE_CHARS + some slack


# ── QABootstrapper.bootstrap — no artifact required ───────────────────────────


def test_bootstrap_noop_when_no_artifact_required(tmp_path: Path):
    db = MagicMock(spec=Session)
    strategy = WebAppStrategy()
    request = _make_request(product_type="web_app", source_path=str(tmp_path))
    bootstrapper = QABootstrapper()

    updated_request, blocked = bootstrapper.bootstrap(db=db, request=request, strategy=strategy)

    assert blocked is None
    assert updated_request.artifact_path is None


# ── QABootstrapper.bootstrap — artifact required, existing APK ────────────────


def test_bootstrap_uses_existing_apk(tmp_path: Path):
    db = MagicMock(spec=Session)
    strategy = MobileAndroidStrategy()
    apk = tmp_path / "app-debug.apk"
    apk.write_text("fake")
    request = _make_request(source_path=str(tmp_path))
    bootstrapper = QABootstrapper()

    updated_request, blocked = bootstrapper.bootstrap(db=db, request=request, strategy=strategy)

    assert blocked is None
    assert updated_request.artifact_path is not None
    assert updated_request.artifact_path.endswith(".apk")


# ── QABootstrapper.bootstrap — LLM detection failure ─────────────────────────


def test_bootstrap_blocked_when_llm_cannot_detect(tmp_path: Path):
    db = MagicMock(spec=Session)
    strategy = MobileAndroidStrategy()
    request = _make_request(source_path=str(tmp_path))
    bootstrapper = QABootstrapper()

    with patch(
        "app.services.qa.qa_bootstrapper._detect_build_command",
        return_value=None,
    ):
        updated_request, blocked = bootstrapper.bootstrap(db=db, request=request, strategy=strategy)

    assert blocked is not None
    assert blocked.verdict == QA_VERDICT_BLOCKED
    assert "Could not detect" in (blocked.error_message or "")


def test_bootstrap_blocked_when_llm_returns_cannot_detect(tmp_path: Path):
    from app.services.qa.qa_bootstrapper import _BuildDetectionOutput

    db = MagicMock(spec=Session)
    strategy = MobileAndroidStrategy()
    request = _make_request(source_path=str(tmp_path))
    bootstrapper = QABootstrapper()

    cannot_detect = _BuildDetectionOutput(
        can_detect=False,
        build_command=None,
        output_pattern=None,
        reasoning="No build files found",
    )
    with patch(
        "app.services.qa.qa_bootstrapper._detect_build_command",
        return_value=cannot_detect,
    ):
        _, blocked = bootstrapper.bootstrap(db=db, request=request, strategy=strategy)

    assert blocked is not None
    assert blocked.verdict == QA_VERDICT_BLOCKED


# ── QABootstrapper.bootstrap — build success ──────────────────────────────────


def test_bootstrap_sets_artifact_path_after_successful_build(tmp_path: Path):
    from app.services.qa.qa_bootstrapper import _BuildDetectionOutput

    db = MagicMock(spec=Session)
    strategy = MobileAndroidStrategy()
    request = _make_request(source_path=str(tmp_path))
    bootstrapper = QABootstrapper()

    # Simulate LLM returning a build command
    build_info = _BuildDetectionOutput(
        can_detect=True,
        build_command="./gradlew assembleDebug",
        output_pattern="app/build/outputs/apk/debug/*.apk",
        reasoning="Detected Gradle project",
    )

    # Simulate the build "succeeding" and producing an APK
    def fake_build(request, cmd):
        apk_dir = tmp_path / "app" / "build" / "outputs" / "apk" / "debug"
        apk_dir.mkdir(parents=True, exist_ok=True)
        (apk_dir / "app-debug.apk").write_text("fake apk")
        return True, ""

    with (
        patch("app.services.qa.qa_bootstrapper._detect_build_command", return_value=build_info),
        patch("app.services.qa.qa_bootstrapper._run_build_in_container", side_effect=fake_build),
    ):
        updated_request, blocked = bootstrapper.bootstrap(db=db, request=request, strategy=strategy)

    assert blocked is None
    assert updated_request.artifact_path is not None
    assert updated_request.artifact_path.endswith(".apk")


# ── QABootstrapper.bootstrap — build failure ──────────────────────────────────


def test_bootstrap_blocked_when_build_fails(tmp_path: Path):
    from app.services.qa.qa_bootstrapper import _BuildDetectionOutput

    db = MagicMock(spec=Session)
    strategy = MobileAndroidStrategy()
    request = _make_request(source_path=str(tmp_path))
    bootstrapper = QABootstrapper()

    build_info = _BuildDetectionOutput(
        can_detect=True,
        build_command="./gradlew assembleDebug",
        output_pattern=None,
        reasoning="Detected Gradle project",
    )

    with (
        patch("app.services.qa.qa_bootstrapper._detect_build_command", return_value=build_info),
        patch(
            "app.services.qa.qa_bootstrapper._run_build_in_container",
            return_value=(False, "JAVA_HOME not set"),
        ),
    ):
        _, blocked = bootstrapper.bootstrap(db=db, request=request, strategy=strategy)

    assert blocked is not None
    assert blocked.verdict == QA_VERDICT_BLOCKED
    assert "JAVA_HOME" in (blocked.error_message or "")
