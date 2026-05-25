"""
Unit tests for EnvironmentManager and its strategies.

These tests exercise:
- Strategy selection (by RuntimeSpec.runtime_type and by file detection)
- PythonStrategy: exact install, preferred fallback, exact_only escalation
- NodeStrategy: basic install and fallback
- JvmStrategy: dependency resolution command
- GenericStrategy: fallback pip install
- EnvironmentManager.install(): success / partial_success / needs_user_input / failed
- manifest update logic (PinnedDependency added/updated)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.services.environment.contracts import PinnedDependency, RuntimeSpec
from app.services.environment.manager import EnvironmentManager, _select_strategy
from app.services.environment.manager_contracts import (
    EnvironmentManagerRequest,
    PackageRequest,
)
from app.services.environment.manager_strategies.generic import GenericStrategy
from app.services.environment.manager_strategies.jvm import JvmStrategy
from app.services.environment.manager_strategies.node import NodeStrategy
from app.services.environment.manager_strategies.python import PythonStrategy

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _python_spec(**kwargs) -> RuntimeSpec:
    defaults = dict(runtime_type="python_venv", image="python:3.12")
    defaults.update(kwargs)
    return RuntimeSpec(**defaults)


def _node_spec(**kwargs) -> RuntimeSpec:
    defaults = dict(runtime_type="node_npm", image="node:20")
    defaults.update(kwargs)
    return RuntimeSpec(**defaults)


def _maven_spec(**kwargs) -> RuntimeSpec:
    defaults = dict(runtime_type="java_maven", image="maven:3.9")
    defaults.update(kwargs)
    return RuntimeSpec(**defaults)


def _make_request(
    packages: list[PackageRequest], version_constraint: str = "any_compatible"
) -> EnvironmentManagerRequest:
    return EnvironmentManagerRequest(
        packages=packages,
        version_constraint=version_constraint,
        project_id=1,
        workspace_path="/tmp/ws",
    )


def _success_cmd(output: str = "Successfully installed pkg-1.2.3"):
    """Return a CmdFn that always succeeds."""

    def cmd(command: str) -> tuple[int, str, str]:
        return 0, output, ""

    return cmd


def _fail_cmd(error: str = "error"):
    """Return a CmdFn that always fails."""

    def cmd(command: str) -> tuple[int, str, str]:
        return 1, "", error

    return cmd


# ---------------------------------------------------------------------------
# Strategy selection
# ---------------------------------------------------------------------------


def test_select_strategy_by_runtime_type_python(tmp_path):
    spec = _python_spec()
    strategy = _select_strategy(spec, tmp_path)
    assert isinstance(strategy, PythonStrategy)


def test_select_strategy_by_runtime_type_node(tmp_path):
    spec = _node_spec()
    strategy = _select_strategy(spec, tmp_path)
    assert isinstance(strategy, NodeStrategy)


def test_select_strategy_by_runtime_type_maven(tmp_path):
    spec = _maven_spec()
    strategy = _select_strategy(spec, tmp_path)
    assert isinstance(strategy, JvmStrategy)


def test_select_strategy_file_detection_python(tmp_path):
    (tmp_path / "requirements.txt").write_text("requests==2.31.0")
    strategy = _select_strategy(None, tmp_path)
    assert isinstance(strategy, PythonStrategy)


def test_select_strategy_file_detection_node(tmp_path):
    (tmp_path / "package.json").write_text('{"name": "app"}')
    strategy = _select_strategy(None, tmp_path)
    assert isinstance(strategy, NodeStrategy)


def test_select_strategy_file_detection_maven(tmp_path):
    (tmp_path / "pom.xml").write_text("<project/>")
    strategy = _select_strategy(None, tmp_path)
    assert isinstance(strategy, JvmStrategy)


def test_select_strategy_fallback_generic(tmp_path):
    # empty directory → no detection marker → generic
    strategy = _select_strategy(None, tmp_path)
    assert isinstance(strategy, GenericStrategy)


# ---------------------------------------------------------------------------
# PythonStrategy
# ---------------------------------------------------------------------------


class TestPythonStrategy:
    def test_detect_by_requirements_txt(self, tmp_path):
        (tmp_path / "requirements.txt").touch()
        assert PythonStrategy().detect(tmp_path) is True

    def test_detect_by_pyproject(self, tmp_path):
        (tmp_path / "pyproject.toml").touch()
        assert PythonStrategy().detect(tmp_path) is True

    def test_detect_false_empty(self, tmp_path):
        assert PythonStrategy().detect(tmp_path) is False

    def test_install_exact_success(self):
        strategy = PythonStrategy()
        spec = _python_spec()
        request = PackageRequest(name="requests", version="2.31.0")
        result = strategy.install_app_package(
            session=None,
            spec=spec,
            request=request,
            version_constraint="any_compatible",
            run_cmd=_success_cmd("Successfully installed requests-2.31.0"),
        )
        assert result.success is True
        assert result.version_adjusted is False
        assert "2.31.0" in (result.installed_version or "")

    def test_install_exact_fails_preferred_uses_fallback(self):
        """When exact version fails and constraint is 'preferred', try without version."""
        strategy = PythonStrategy()
        spec = _python_spec()
        request = PackageRequest(name="requests", version="0.0.1")
        calls = []

        def cmd(command: str) -> tuple[int, str, str]:
            calls.append(command)
            if "==0.0.1" in command:
                return 1, "", "No matching distribution found for requests==0.0.1"
            return 0, "Successfully installed requests-2.31.0", ""

        result = strategy.install_app_package(
            session=None,
            spec=spec,
            request=request,
            version_constraint="preferred",
            run_cmd=cmd,
        )
        assert result.success is True
        assert result.version_adjusted is True
        assert len(calls) == 2

    def test_install_exact_fails_exact_only_escalates(self):
        """exact_only constraint → failure without fallback."""
        strategy = PythonStrategy()
        spec = _python_spec()
        request = PackageRequest(name="requests", version="0.0.1")
        result = strategy.install_app_package(
            session=None,
            spec=spec,
            request=request,
            version_constraint="exact_only",
            run_cmd=_fail_cmd("No matching distribution found"),
        )
        assert result.success is False
        assert "exact_only" in (result.error or "")

    def test_install_no_version_any_compatible(self):
        """No version specified → plain pip install."""
        strategy = PythonStrategy()
        spec = _python_spec()
        request = PackageRequest(name="pandas")
        result = strategy.install_app_package(
            session=None,
            spec=spec,
            request=request,
            version_constraint="any_compatible",
            run_cmd=_success_cmd("Successfully installed pandas-2.1.0"),
        )
        assert result.success is True

    def test_update_manifest_adds_new(self):
        strategy = PythonStrategy()
        spec = _python_spec()
        updated = strategy.update_manifest(spec=spec, name="pandas", installed_version="2.1.0")
        assert any(d.name == "pandas" and d.version == "2.1.0" for d in updated.dependencies)

    def test_update_manifest_updates_existing(self):
        strategy = PythonStrategy()
        spec = _python_spec(dependencies=[PinnedDependency(name="requests", version="2.28.0")])
        updated = strategy.update_manifest(spec=spec, name="requests", installed_version="2.31.0")
        reqs = [d for d in updated.dependencies if d.name == "requests"]
        assert len(reqs) == 1
        assert reqs[0].version == "2.31.0"


# ---------------------------------------------------------------------------
# NodeStrategy
# ---------------------------------------------------------------------------


class TestNodeStrategy:
    def test_detect(self, tmp_path):
        (tmp_path / "package.json").write_text("{}")
        assert NodeStrategy().detect(tmp_path) is True

    def test_install_success(self):
        strategy = NodeStrategy()
        spec = _node_spec()
        request = PackageRequest(name="lodash", version="4.17.21")
        result = strategy.install_app_package(
            session=None,
            spec=spec,
            request=request,
            version_constraint="any_compatible",
            run_cmd=_success_cmd("added 1 package (lodash@4.17.21)"),
        )
        assert result.success is True


# ---------------------------------------------------------------------------
# JvmStrategy
# ---------------------------------------------------------------------------


class TestJvmStrategy:
    def test_detect_by_pom(self, tmp_path):
        (tmp_path / "pom.xml").write_text("<project/>")
        assert JvmStrategy().detect(tmp_path) is True

    def test_detect_by_gradle(self, tmp_path):
        (tmp_path / "build.gradle").write_text("dependencies {}")
        assert JvmStrategy().detect(tmp_path) is True

    def test_requires_rebuild(self):
        assert JvmStrategy().requires_rebuild is True

    def test_install_triggers_resolution(self):
        strategy = JvmStrategy()
        spec = _maven_spec()
        request = PackageRequest(name="com.google.guava:guava", version="32.0.0")
        result = strategy.install_app_package(
            session=None,
            spec=spec,
            request=request,
            version_constraint="any_compatible",
            run_cmd=_success_cmd("[INFO] BUILD SUCCESS"),
        )
        assert result.success is True
        assert result.installed_version == "32.0.0"


# ---------------------------------------------------------------------------
# EnvironmentManager.install()
# ---------------------------------------------------------------------------


class TestEnvironmentManager:
    """Tests for the orchestrating install() method."""

    def _manager_with_mock_cmd(self, return_code: int, output: str):
        """Create a manager whose command executor always returns the given result."""
        mgr = EnvironmentManager()
        mock_cmd = MagicMock(return_value=(return_code, output, ""))
        return mgr, mock_cmd

    def test_install_success_updates_manifest(self):
        spec = _python_spec()
        request = _make_request([PackageRequest(name="requests", version="2.31.0")])

        with patch(
            "app.services.environment.manager._build_run_cmd",
            return_value=_success_cmd("Successfully installed requests-2.31.0"),
        ):
            mgr = EnvironmentManager()
            output = mgr.install(request=request, spec=spec)

        assert output.status == "success"
        assert len(output.installed_packages) == 1
        assert output.installed_packages[0].name == "requests"
        assert output.updated_runtime_spec_json is not None
        # Verify the spec JSON contains the new package
        updated = RuntimeSpec.model_validate_json(output.updated_runtime_spec_json)
        assert any(d.name == "requests" for d in updated.dependencies)

    def test_install_failure_returns_failed(self):
        spec = _python_spec()
        request = _make_request([PackageRequest(name="nonexistent-pkg-xyz")])

        with patch(
            "app.services.environment.manager._build_run_cmd",
            return_value=_fail_cmd("No matching distribution found"),
        ):
            mgr = EnvironmentManager()
            output = mgr.install(request=request, spec=spec)

        assert output.status == "failed"
        assert output.blocker_message is not None
        assert len(output.installed_packages) == 0

    def test_install_partial_success(self):
        """First package succeeds, second fails → partial_success."""
        spec = _python_spec()
        request = _make_request(
            [
                PackageRequest(name="requests", version="2.31.0"),
                PackageRequest(name="bad-pkg-xyz"),
            ]
        )
        calls = []

        def cmd(command: str) -> tuple[int, str, str]:
            calls.append(command)
            if "requests" in command:
                return 0, "Successfully installed requests-2.31.0", ""
            return 1, "", "No matching distribution found"

        with patch("app.services.environment.manager._build_run_cmd", return_value=cmd):
            mgr = EnvironmentManager()
            output = mgr.install(request=request, spec=spec)

        assert output.status == "partial_success"
        assert len(output.installed_packages) == 1
        assert output.installed_packages[0].name == "requests"
        assert len(output.dependency_conflicts) == 1

    def test_install_exact_only_escalates_to_needs_user_input(self):
        """exact_only version conflict → needs_user_input status."""
        spec = _python_spec()
        request = _make_request(
            [PackageRequest(name="requests", version="0.0.1")],
            version_constraint="exact_only",
        )

        with patch(
            "app.services.environment.manager._build_run_cmd",
            return_value=_fail_cmd("No matching distribution found"),
        ):
            mgr = EnvironmentManager()
            output = mgr.install(request=request, spec=spec)

        assert output.status == "needs_user_input"
        assert output.blocker_message is not None

    def test_install_without_spec_uses_generic(self, tmp_path):
        """No RuntimeSpec provided — manager falls back to generic strategy."""
        request = EnvironmentManagerRequest(
            packages=[PackageRequest(name="requests")],
            version_constraint="any_compatible",
            project_id=1,
            workspace_path=str(tmp_path),
        )

        with patch(
            "app.services.environment.manager._build_run_cmd",
            return_value=_success_cmd("Successfully installed requests-2.31.0"),
        ):
            mgr = EnvironmentManager()
            output = mgr.install(request=request, spec=None)

        # No spec → no manifest update, but status should be success
        assert output.status == "success"
        assert output.updated_runtime_spec_json is None

    def test_install_requires_rebuild_propagated(self, tmp_path):
        """JVM strategy sets requires_rebuild=True — needs a real pom.xml on disk."""
        # The JVM strategy now checks for pom.xml existence before running mvn,
        # so the workspace_path must point to a directory with a real pom.xml.
        pom = tmp_path / "pom.xml"
        pom.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<project xmlns="http://maven.apache.org/POM/4.0.0">'
            "<modelVersion>4.0.0</modelVersion>"
            "<groupId>com.example</groupId>"
            "<artifactId>app</artifactId>"
            "<version>1.0.0</version>"
            "</project>"
        )
        spec = _maven_spec()
        request = EnvironmentManagerRequest(
            packages=[PackageRequest(name="com.google.guava:guava", version="32.0.0")],
            version_constraint="any_compatible",
            project_id=1,
            workspace_path=str(tmp_path),
        )

        with patch(
            "app.services.environment.manager._build_run_cmd",
            return_value=_success_cmd("[INFO] BUILD SUCCESS"),
        ):
            mgr = EnvironmentManager()
            output = mgr.install(request=request, spec=spec)

        assert output.requires_rebuild is True

    def test_install_observation_for_version_adjusted(self):
        """A version_adjusted install produces an observation message."""
        spec = _python_spec()
        request = _make_request(
            [PackageRequest(name="requests", version="0.0.1")],
            version_constraint="preferred",
        )
        calls = []

        def cmd(command: str) -> tuple[int, str, str]:
            calls.append(command)
            if "==0.0.1" in command:
                return 1, "", "No matching distribution found"
            return 0, "Successfully installed requests-2.31.0", ""

        with patch("app.services.environment.manager._build_run_cmd", return_value=cmd):
            mgr = EnvironmentManager()
            output = mgr.install(request=request, spec=spec)

        assert output.status == "success"
        assert output.installed_packages[0].version_adjusted is True
        assert any("requests" in obs for obs in output.observations)
