"""
Unit tests for the new and redesigned EnvironmentManager strategies.

Covers:
- GoStrategy: detect, install, version fallback, version extraction
- RustStrategy: detect, install, cargo fetch, version fallback, version extraction
- DotnetStrategy: detect, .csproj finding, install, version extraction, fallback
- JvmStrategy (rewrite): pom.xml editing, build.gradle editing, Gradle KTS, Flutter path
- manifest_files_changed propagation through EnvironmentManager.install()
- Strategy selection for new runtime_types
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from unittest.mock import patch

from app.services.environment.contracts import PinnedDependency, RuntimeSpec
from app.services.environment.manager import EnvironmentManager, _select_strategy
from app.services.environment.manager_contracts import (
    EnvironmentManagerRequest,
    PackageRequest,
)
from app.services.environment.manager_strategies.dotnet import (
    DotnetStrategy,
    _extract_dotnet_version,
    _find_project_file,
)
from app.services.environment.manager_strategies.go import (
    GoStrategy,
    _build_go_pkg_spec,
    _extract_go_version,
)
from app.services.environment.manager_strategies.jvm import (
    JvmStrategy,
    _edit_gradle_file,
    _edit_pom_xml,
    _find_deps_block_end,
    _find_gradle_file,
    _parse_maven_coord,
)
from app.services.environment.manager_strategies.rust import (
    RustStrategy,
    _build_cargo_spec,
    _extract_cargo_version,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _spec(runtime_type: str, image: str = "test:latest") -> RuntimeSpec:
    return RuntimeSpec(runtime_type=runtime_type, image=image)


def _success_cmd(output: str = "ok"):
    def cmd(command: str) -> tuple[int, str, str]:
        return 0, output, ""

    return cmd


def _fail_cmd(stderr: str = "error"):
    def cmd(command: str) -> tuple[int, str, str]:
        return 1, "", stderr

    return cmd


def _switching_cmd(fail_on: str, success_output: str = "ok"):
    """Fails when 'fail_on' is in the command, succeeds otherwise."""

    def cmd(command: str) -> tuple[int, str, str]:
        if fail_on in command:
            return 1, "", f"failed: {fail_on}"
        return 0, success_output, ""

    return cmd


# ---------------------------------------------------------------------------
# Strategy selection — new runtime types
# ---------------------------------------------------------------------------


def test_select_strategy_go(tmp_path):
    from app.services.environment.manager_strategies.go import GoStrategy

    spec = _spec("go")
    strategy = _select_strategy(spec, tmp_path)
    assert isinstance(strategy, GoStrategy)


def test_select_strategy_rust(tmp_path):
    spec = _spec("rust_cargo")
    strategy = _select_strategy(spec, tmp_path)
    assert isinstance(strategy, RustStrategy)


def test_select_strategy_dotnet(tmp_path):
    spec = _spec("dotnet")
    strategy = _select_strategy(spec, tmp_path)
    assert isinstance(strategy, DotnetStrategy)


def test_select_strategy_go_file_detection(tmp_path):
    from app.services.environment.manager_strategies.go import GoStrategy

    (tmp_path / "go.mod").write_text("module example.com/myapp\n\ngo 1.21\n")
    strategy = _select_strategy(None, tmp_path)
    assert isinstance(strategy, GoStrategy)


def test_select_strategy_rust_file_detection(tmp_path):
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "myapp"\n')
    strategy = _select_strategy(None, tmp_path)
    assert isinstance(strategy, RustStrategy)


def test_select_strategy_dotnet_file_detection(tmp_path):
    (tmp_path / "MyApp.csproj").write_text("<Project/>")
    strategy = _select_strategy(None, tmp_path)
    assert isinstance(strategy, DotnetStrategy)


# ---------------------------------------------------------------------------
# GoStrategy
# ---------------------------------------------------------------------------


class TestGoStrategy:
    def test_detect_by_go_mod(self, tmp_path):
        (tmp_path / "go.mod").write_text("module example.com/x\n\ngo 1.21\n")
        assert GoStrategy().detect(tmp_path) is True

    def test_detect_false_empty(self, tmp_path):
        assert GoStrategy().detect(tmp_path) is False

    def test_runtime_types(self):
        assert "go" in GoStrategy().runtime_types

    def test_install_success(self, tmp_path):
        (tmp_path / "go.mod").write_text("module example.com/x\n\ngo 1.21\n")
        strategy = GoStrategy()
        spec = _spec("go")
        request = PackageRequest(name="github.com/gin-gonic/gin", version="v1.9.0")
        output = "go: added github.com/gin-gonic/gin v1.9.0"
        result = strategy.install_app_package(
            session=None,
            spec=spec,
            request=request,
            version_constraint="any_compatible",
            run_cmd=_success_cmd(output),
            workspace_path=tmp_path,
        )
        assert result.success is True
        assert result.version_adjusted is False
        assert result.installed_version == "v1.9.0"
        assert "go.mod" in result.manifest_files_changed
        assert "go.sum" in result.manifest_files_changed

    def test_install_no_go_mod(self, tmp_path):
        """Missing go.mod returns failure without running any command."""
        strategy = GoStrategy()
        spec = _spec("go")
        request = PackageRequest(name="github.com/gin-gonic/gin")
        result = strategy.install_app_package(
            session=None,
            spec=spec,
            request=request,
            version_constraint="any_compatible",
            run_cmd=_success_cmd(),
            workspace_path=tmp_path,
        )
        assert result.success is False
        assert "go.mod" in (result.error or "")

    def test_install_fallback_to_latest(self, tmp_path):
        """When versioned get fails with 'preferred', falls back to @latest."""
        (tmp_path / "go.mod").write_text("module example.com/x\n\ngo 1.21\n")
        strategy = GoStrategy()
        spec = _spec("go")
        request = PackageRequest(name="github.com/gin-gonic/gin", version="v0.0.1")
        calls = []

        def cmd(command: str) -> tuple[int, str, str]:
            calls.append(command)
            if "@v0.0.1" in command or "@vv0.0.1" in command:
                return 1, "", "no matching version"
            return 0, "go: added github.com/gin-gonic/gin v1.9.0", ""

        result = strategy.install_app_package(
            session=None,
            spec=spec,
            request=request,
            version_constraint="preferred",
            run_cmd=cmd,
            workspace_path=tmp_path,
        )
        assert result.success is True
        assert result.version_adjusted is True
        assert "latest" in (result.installed_version or "")

    def test_install_no_fallback_with_exact_only(self, tmp_path):
        """exact_only: no fallback attempt when versioned install fails."""
        (tmp_path / "go.mod").write_text("module example.com/x\n\ngo 1.21\n")
        strategy = GoStrategy()
        spec = _spec("go")
        request = PackageRequest(name="github.com/gin-gonic/gin", version="v0.0.1")
        calls = []

        def cmd(command: str) -> tuple[int, str, str]:
            calls.append(command)
            return 1, "", "no matching version"

        result = strategy.install_app_package(
            session=None,
            spec=spec,
            request=request,
            version_constraint="exact_only",
            run_cmd=cmd,
            workspace_path=tmp_path,
        )
        assert result.success is False
        # Only one command attempted (no fallback)
        assert len([c for c in calls if "go get" in c]) == 1

    def test_requires_rebuild_false(self):
        assert GoStrategy().requires_rebuild is False

    def test_update_manifest_adds(self):
        spec = _spec("go")
        updated = GoStrategy().update_manifest(spec=spec, name="gin", installed_version="v1.9.0")
        assert any(d.name == "gin" and d.version == "v1.9.0" for d in updated.dependencies)

    def test_update_manifest_replaces_existing(self):
        spec = RuntimeSpec(
            runtime_type="go",
            image="golang:1.21",
            dependencies=[PinnedDependency(name="gin", version="v1.8.0")],
        )
        updated = GoStrategy().update_manifest(spec=spec, name="gin", installed_version="v1.9.0")
        gins = [d for d in updated.dependencies if d.name == "gin"]
        assert len(gins) == 1
        assert gins[0].version == "v1.9.0"


class TestBuildGoPkgSpec:
    def test_with_v_prefix(self):
        assert (
            _build_go_pkg_spec("github.com/gin-gonic/gin", "v1.9.0")
            == "github.com/gin-gonic/gin@v1.9.0"
        )

    def test_adds_v_prefix(self):
        assert (
            _build_go_pkg_spec("github.com/gin-gonic/gin", "1.9.0")
            == "github.com/gin-gonic/gin@v1.9.0"
        )

    def test_no_version(self):
        assert (
            _build_go_pkg_spec("github.com/gin-gonic/gin", None)
            == "github.com/gin-gonic/gin@latest"
        )


class TestExtractGoVersion:
    def test_added(self):
        output = "go: added github.com/gin-gonic/gin v1.9.0"
        assert _extract_go_version(output, "github.com/gin-gonic/gin") == "v1.9.0"

    def test_upgraded(self):
        output = "go: upgraded github.com/gin-gonic/gin v1.8.0 => v1.9.0"
        assert _extract_go_version(output, "github.com/gin-gonic/gin") == "v1.9.0"

    def test_no_match(self):
        assert _extract_go_version("some other output", "github.com/gin-gonic/gin") is None


# ---------------------------------------------------------------------------
# RustStrategy
# ---------------------------------------------------------------------------


class TestRustStrategy:
    def test_detect_by_cargo_toml(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text('[package]\nname = "x"\n')
        assert RustStrategy().detect(tmp_path) is True

    def test_detect_false_empty(self, tmp_path):
        assert RustStrategy().detect(tmp_path) is False

    def test_runtime_types(self):
        assert "rust_cargo" in RustStrategy().runtime_types

    def test_install_success(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text('[package]\nname = "x"\n')
        strategy = RustStrategy()
        spec = _spec("rust_cargo")
        request = PackageRequest(name="serde", version="1.0.196")
        output = "    Adding serde v1.0.196 to dependencies"
        result = strategy.install_app_package(
            session=None,
            spec=spec,
            request=request,
            version_constraint="any_compatible",
            run_cmd=_success_cmd(output),
            workspace_path=tmp_path,
        )
        assert result.success is True
        assert result.version_adjusted is False
        assert result.installed_version == "1.0.196"
        assert "Cargo.toml" in result.manifest_files_changed
        assert "Cargo.lock" in result.manifest_files_changed

    def test_install_no_cargo_toml(self, tmp_path):
        """Missing Cargo.toml returns failure without running any command."""
        strategy = RustStrategy()
        spec = _spec("rust_cargo")
        request = PackageRequest(name="serde")
        result = strategy.install_app_package(
            session=None,
            spec=spec,
            request=request,
            version_constraint="any_compatible",
            run_cmd=_success_cmd(),
            workspace_path=tmp_path,
        )
        assert result.success is False
        assert "Cargo.toml" in (result.error or "")

    def test_install_fallback_to_latest(self, tmp_path):
        """When versioned add fails with 'preferred', falls back to unversioned."""
        (tmp_path / "Cargo.toml").write_text('[package]\nname = "x"\n')
        strategy = RustStrategy()
        spec = _spec("rust_cargo")
        request = PackageRequest(name="serde", version="0.0.1")
        calls = []

        def cmd(command: str) -> tuple[int, str, str]:
            calls.append(command)
            if "@0.0.1" in command:
                return 1, "", "no such version"
            return 0, "    Adding serde v1.0.196 to dependencies", ""

        result = strategy.install_app_package(
            session=None,
            spec=spec,
            request=request,
            version_constraint="preferred",
            run_cmd=cmd,
            workspace_path=tmp_path,
        )
        assert result.success is True
        assert result.version_adjusted is True
        assert result.installed_version == "1.0.196"

    def test_install_no_fallback_exact_only(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text('[package]\nname = "x"\n')
        strategy = RustStrategy()
        spec = _spec("rust_cargo")
        request = PackageRequest(name="serde", version="0.0.1")
        calls = []

        def cmd(command: str) -> tuple[int, str, str]:
            calls.append(command)
            return 1, "", "no such version"

        result = strategy.install_app_package(
            session=None,
            spec=spec,
            request=request,
            version_constraint="exact_only",
            run_cmd=cmd,
            workspace_path=tmp_path,
        )
        assert result.success is False
        assert len([c for c in calls if "cargo add" in c]) == 1

    def test_requires_rebuild_false(self):
        # cargo fetch downloads; compile is left to command_runner_agent
        assert RustStrategy().requires_rebuild is False

    def test_update_manifest_adds(self):
        spec = _spec("rust_cargo")
        updated = RustStrategy().update_manifest(
            spec=spec, name="serde", installed_version="1.0.196"
        )
        assert any(d.name == "serde" and d.version == "1.0.196" for d in updated.dependencies)


class TestBuildCargoSpec:
    def test_with_version(self):
        assert _build_cargo_spec("serde", "1.0") == "serde@1.0"

    def test_no_version(self):
        assert _build_cargo_spec("serde", None) == "serde"


class TestExtractCargoVersion:
    def test_adding_line(self):
        output = "    Adding serde v1.0.196 to dependencies"
        assert _extract_cargo_version(output, "serde") == "1.0.196"

    def test_multiline(self):
        output = "Updating crates.io index\n    Adding tokio v1.36.0 to dependencies"
        assert _extract_cargo_version(output, "tokio") == "1.36.0"

    def test_no_match(self):
        assert _extract_cargo_version("some output", "serde") is None


# ---------------------------------------------------------------------------
# DotnetStrategy
# ---------------------------------------------------------------------------


class TestDotnetStrategy:
    def test_detect_by_csproj(self, tmp_path):
        (tmp_path / "MyApp.csproj").write_text("<Project/>")
        assert DotnetStrategy().detect(tmp_path) is True

    def test_detect_by_fsproj(self, tmp_path):
        (tmp_path / "MyApp.fsproj").write_text("<Project/>")
        assert DotnetStrategy().detect(tmp_path) is True

    def test_detect_csproj_one_level_deep(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "MyLib.csproj").write_text("<Project/>")
        assert DotnetStrategy().detect(tmp_path) is True

    def test_detect_false_empty(self, tmp_path):
        assert DotnetStrategy().detect(tmp_path) is False

    def test_runtime_types(self):
        assert "dotnet" in DotnetStrategy().runtime_types

    def test_requires_rebuild_false(self):
        # dotnet add package runs NuGet restore automatically
        assert DotnetStrategy().requires_rebuild is False

    def test_install_success(self, tmp_path):
        (tmp_path / "MyApp.csproj").write_text("<Project/>")
        strategy = DotnetStrategy()
        spec = _spec("dotnet")
        request = PackageRequest(name="Newtonsoft.Json", version="13.0.3")
        output = "info : PackageReference for package 'Newtonsoft.Json' version '13.0.3' added to file 'MyApp.csproj'."
        result = strategy.install_app_package(
            session=None,
            spec=spec,
            request=request,
            version_constraint="any_compatible",
            run_cmd=_success_cmd(output),
            workspace_path=tmp_path,
        )
        assert result.success is True
        assert result.version_adjusted is False
        assert result.installed_version == "13.0.3"
        assert "MyApp.csproj" in result.manifest_files_changed

    def test_install_no_csproj(self, tmp_path):
        """Missing project file returns failure without running any command."""
        strategy = DotnetStrategy()
        spec = _spec("dotnet")
        request = PackageRequest(name="Newtonsoft.Json")
        result = strategy.install_app_package(
            session=None,
            spec=spec,
            request=request,
            version_constraint="any_compatible",
            run_cmd=_success_cmd(),
            workspace_path=tmp_path,
        )
        assert result.success is False
        assert "csproj" in (result.error or "").lower() or "fsproj" in (result.error or "").lower()

    def test_install_fallback_to_latest(self, tmp_path):
        """When versioned install fails with 'preferred', retries without version."""
        (tmp_path / "MyApp.csproj").write_text("<Project/>")
        strategy = DotnetStrategy()
        spec = _spec("dotnet")
        request = PackageRequest(name="Newtonsoft.Json", version="0.0.1")
        calls = []

        def cmd(command: str) -> tuple[int, str, str]:
            calls.append(command)
            if "--version" in command:
                return 1, "", "Package version 0.0.1 not found"
            return (
                0,
                "info : PackageReference for package 'Newtonsoft.Json' version '13.0.3' added",
                "",
            )

        result = strategy.install_app_package(
            session=None,
            spec=spec,
            request=request,
            version_constraint="preferred",
            run_cmd=cmd,
            workspace_path=tmp_path,
        )
        assert result.success is True
        assert result.version_adjusted is True
        assert result.installed_version in ("13.0.3", "latest")

    def test_install_no_fallback_exact_only(self, tmp_path):
        (tmp_path / "MyApp.csproj").write_text("<Project/>")
        strategy = DotnetStrategy()
        spec = _spec("dotnet")
        request = PackageRequest(name="Newtonsoft.Json", version="0.0.1")
        calls = []

        def cmd(command: str) -> tuple[int, str, str]:
            calls.append(command)
            return 1, "", "error"

        result = strategy.install_app_package(
            session=None,
            spec=spec,
            request=request,
            version_constraint="exact_only",
            run_cmd=cmd,
            workspace_path=tmp_path,
        )
        assert result.success is False
        assert len([c for c in calls if "dotnet add" in c]) == 1

    def test_update_manifest_adds(self):
        spec = _spec("dotnet")
        updated = DotnetStrategy().update_manifest(
            spec=spec, name="Newtonsoft.Json", installed_version="13.0.3"
        )
        assert any(
            d.name == "Newtonsoft.Json" and d.version == "13.0.3" for d in updated.dependencies
        )


class TestFindProjectFile:
    def test_finds_csproj(self, tmp_path):
        (tmp_path / "MyApp.csproj").write_text("<Project/>")
        found = _find_project_file(tmp_path)
        assert found is not None
        assert found.name == "MyApp.csproj"

    def test_finds_fsproj(self, tmp_path):
        (tmp_path / "MyLib.fsproj").write_text("<Project/>")
        found = _find_project_file(tmp_path)
        assert found is not None
        assert found.name == "MyLib.fsproj"

    def test_finds_one_level_deep(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "App.csproj").write_text("<Project/>")
        found = _find_project_file(tmp_path)
        assert found is not None
        assert found.name == "App.csproj"

    def test_returns_none_when_missing(self, tmp_path):
        assert _find_project_file(tmp_path) is None


class TestExtractDotnetVersion:
    def test_with_single_quotes(self):
        output = "info : PackageReference for package 'Newtonsoft.Json' version '13.0.3' added to file '...'"
        assert _extract_dotnet_version(output, "Newtonsoft.Json") == "13.0.3"

    def test_case_insensitive(self):
        output = "info : packagereference for PACKAGE 'NEWTONSOFT.JSON' VERSION '13.0.3' added"
        assert _extract_dotnet_version(output, "Newtonsoft.Json") == "13.0.3"

    def test_no_match(self):
        assert _extract_dotnet_version("some output", "Newtonsoft.Json") is None


# ---------------------------------------------------------------------------
# JvmStrategy — Maven pom.xml editing
# ---------------------------------------------------------------------------

_MINIMAL_POM = """\
<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0"
         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 http://maven.apache.org/xsd/maven-4.0.0.xsd"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
    <modelVersion>4.0.0</modelVersion>
    <groupId>com.example</groupId>
    <artifactId>myapp</artifactId>
    <version>1.0.0</version>
</project>
"""

_POM_WITH_EXISTING_DEP = """\
<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
    <modelVersion>4.0.0</modelVersion>
    <dependencies>
        <dependency>
            <groupId>junit</groupId>
            <artifactId>junit</artifactId>
            <version>4.13.2</version>
        </dependency>
    </dependencies>
</project>
"""

_POM_NO_NAMESPACE = """\
<?xml version="1.0" encoding="UTF-8"?>
<project>
    <modelVersion>4.0.0</modelVersion>
    <groupId>com.example</groupId>
    <artifactId>myapp</artifactId>
</project>
"""


class TestEditPomXml:
    def test_adds_dependency_to_empty_project(self, tmp_path):
        pom = tmp_path / "pom.xml"
        pom.write_text(_MINIMAL_POM)
        _edit_pom_xml(pom, "com.google.guava", "guava", "32.0.0")
        tree = ET.parse(str(pom))
        root = tree.getroot()
        ns = "{http://maven.apache.org/POM/4.0.0}"
        deps = root.find(f"{ns}dependencies")
        assert deps is not None
        dep_list = deps.findall(f"{ns}dependency")
        assert len(dep_list) == 1
        assert dep_list[0].findtext(f"{ns}groupId") == "com.google.guava"
        assert dep_list[0].findtext(f"{ns}artifactId") == "guava"
        assert dep_list[0].findtext(f"{ns}version") == "32.0.0"

    def test_adds_to_existing_dependencies_block(self, tmp_path):
        pom = tmp_path / "pom.xml"
        pom.write_text(_POM_WITH_EXISTING_DEP)
        _edit_pom_xml(pom, "com.google.guava", "guava", "32.0.0")
        tree = ET.parse(str(pom))
        root = tree.getroot()
        ns = "{http://maven.apache.org/POM/4.0.0}"
        deps = root.find(f"{ns}dependencies")
        assert len(deps.findall(f"{ns}dependency")) == 2

    def test_updates_existing_version(self, tmp_path):
        pom = tmp_path / "pom.xml"
        pom.write_text(_POM_WITH_EXISTING_DEP)
        _edit_pom_xml(pom, "junit", "junit", "4.14.0")
        tree = ET.parse(str(pom))
        root = tree.getroot()
        ns = "{http://maven.apache.org/POM/4.0.0}"
        deps = root.find(f"{ns}dependencies")
        dep_list = deps.findall(f"{ns}dependency")
        # Still only one entry
        junit = [d for d in dep_list if d.findtext(f"{ns}artifactId") == "junit"]
        assert len(junit) == 1
        assert junit[0].findtext(f"{ns}version") == "4.14.0"

    def test_no_namespace_pom(self, tmp_path):
        pom = tmp_path / "pom.xml"
        pom.write_text(_POM_NO_NAMESPACE)
        _edit_pom_xml(pom, "com.google.guava", "guava", "32.0.0")
        tree = ET.parse(str(pom))
        root = tree.getroot()
        deps = root.find("dependencies")
        assert deps is not None
        dep_list = deps.findall("dependency")
        assert len(dep_list) == 1

    def test_no_version_arg(self, tmp_path):
        pom = tmp_path / "pom.xml"
        pom.write_text(_MINIMAL_POM)
        _edit_pom_xml(pom, "com.google.guava", "guava", None)
        tree = ET.parse(str(pom))
        root = tree.getroot()
        ns = "{http://maven.apache.org/POM/4.0.0}"
        dep = root.find(f"{ns}dependencies/{ns}dependency")
        assert dep is not None
        # No <version> element when version is None
        assert dep.find(f"{ns}version") is None

    def test_preserves_xml_declaration(self, tmp_path):
        pom = tmp_path / "pom.xml"
        pom.write_text(_MINIMAL_POM)
        _edit_pom_xml(pom, "com.google.guava", "guava", "32.0.0")
        content = pom.read_text(encoding="utf-8")
        assert content.startswith("<?xml")


class TestParseMavenCoord:
    def test_group_artifact_version(self):
        g, a, v = _parse_maven_coord("com.google.guava:guava:32.0.0", None)
        assert g == "com.google.guava"
        assert a == "guava"
        assert v == "32.0.0"

    def test_group_artifact_only(self):
        g, a, v = _parse_maven_coord("com.google.guava:guava", "32.0.0")
        assert g == "com.google.guava"
        assert a == "guava"
        assert v == "32.0.0"

    def test_name_only(self):
        g, a, v = _parse_maven_coord("guava", "32.0.0")
        assert g == "guava"
        assert a == "guava"
        assert v == "32.0.0"

    def test_name_only_no_version(self):
        g, a, v = _parse_maven_coord("guava", None)
        assert g == "guava"
        assert a == "guava"
        assert v is None


# ---------------------------------------------------------------------------
# JvmStrategy — Gradle build file editing
# ---------------------------------------------------------------------------

_GRADLE_WITH_DEPS = """\
plugins {
    id 'java'
}

dependencies {
    testImplementation 'junit:junit:4.13.2'
}
"""

_GRADLE_WITHOUT_DEPS = """\
plugins {
    id 'java'
}
"""

_GRADLE_KTS = """\
plugins {
    kotlin("jvm") version "1.9.0"
}

dependencies {
    testImplementation("junit:junit:4.13.2")
}
"""

_GRADLE_WITH_NESTED = """\
dependencies {
    implementation('org.slf4j:slf4j-api') {
        exclude group: 'commons-logging'
    }
}
"""


class TestFindDepsBlockEnd:
    def test_simple_block(self):
        content = "dependencies {\n    impl 'x'\n}\n"
        idx = _find_deps_block_end(content)
        assert idx is not None
        assert content[idx] == "}"

    def test_nested_block(self):
        idx = _find_deps_block_end(_GRADLE_WITH_NESTED)
        assert idx is not None
        # Should be the last closing brace
        assert _GRADLE_WITH_NESTED[idx] == "}"
        assert _GRADLE_WITH_NESTED[idx + 1 :].strip() == ""

    def test_no_block(self):
        assert _find_deps_block_end("plugins { id 'java' }") is None


class TestEditGradleFile:
    def test_adds_to_existing_deps_block_groovy(self, tmp_path):
        gradle = tmp_path / "build.gradle"
        gradle.write_text(_GRADLE_WITH_DEPS)
        _edit_gradle_file(gradle, "com.google.guava", "guava", "32.0.0")
        content = gradle.read_text()
        assert "com.google.guava:guava:32.0.0" in content
        assert "implementation" in content

    def test_adds_to_kotlin_kts(self, tmp_path):
        gradle = tmp_path / "build.gradle.kts"
        gradle.write_text(_GRADLE_KTS)
        _edit_gradle_file(gradle, "com.google.guava", "guava", "32.0.0")
        content = gradle.read_text()
        assert 'implementation("com.google.guava:guava:32.0.0")' in content

    def test_creates_deps_block_if_missing(self, tmp_path):
        gradle = tmp_path / "build.gradle"
        gradle.write_text(_GRADLE_WITHOUT_DEPS)
        _edit_gradle_file(gradle, "com.google.guava", "guava", "32.0.0")
        content = gradle.read_text()
        assert "dependencies {" in content
        assert "com.google.guava:guava:32.0.0" in content

    def test_idempotent_skips_if_already_present(self, tmp_path):
        gradle = tmp_path / "build.gradle"
        # Already has the coordinate
        content = _GRADLE_WITH_DEPS + "    implementation 'com.google.guava:guava:31.0.0'\n"
        gradle.write_text(content)
        _edit_gradle_file(gradle, "com.google.guava", "guava", "32.0.0")
        # Should not add a second implementation line
        assert gradle.read_text().count("com.google.guava:guava") == 1

    def test_groovy_uses_single_quotes(self, tmp_path):
        gradle = tmp_path / "build.gradle"
        gradle.write_text(_GRADLE_WITH_DEPS)
        _edit_gradle_file(gradle, "org.springframework", "spring-core", "6.0.0")
        content = gradle.read_text()
        assert "implementation 'org.springframework:spring-core:6.0.0'" in content

    def test_kts_uses_double_quotes(self, tmp_path):
        gradle = tmp_path / "build.gradle.kts"
        gradle.write_text(_GRADLE_KTS)
        _edit_gradle_file(gradle, "org.springframework", "spring-core", "6.0.0")
        content = gradle.read_text()
        assert 'implementation("org.springframework:spring-core:6.0.0")' in content


class TestFindGradleFile:
    def test_finds_root_build_gradle(self, tmp_path):
        (tmp_path / "build.gradle").write_text("dependencies {}")
        found = _find_gradle_file(tmp_path)
        assert found is not None
        assert found.name == "build.gradle"

    def test_prefers_kts(self, tmp_path):
        (tmp_path / "build.gradle").write_text("dependencies {}")
        (tmp_path / "build.gradle.kts").write_text("dependencies {}")
        found = _find_gradle_file(tmp_path)
        assert found is not None
        assert found.name == "build.gradle.kts"

    def test_prefers_app_subdir_for_android(self, tmp_path):
        app = tmp_path / "app"
        app.mkdir()
        (app / "build.gradle").write_text("dependencies {}")
        (tmp_path / "build.gradle").write_text("dependencies {}")
        found = _find_gradle_file(tmp_path, prefer_app_subdir=True)
        assert found is not None
        assert found.parent.name == "app"

    def test_returns_none_when_missing(self, tmp_path):
        assert _find_gradle_file(tmp_path) is None


class TestJvmStrategyInstall:
    def test_install_maven_with_real_pom(self, tmp_path):
        pom = tmp_path / "pom.xml"
        pom.write_text(_MINIMAL_POM)
        strategy = JvmStrategy()
        spec = _spec("java_maven")
        request = PackageRequest(name="com.google.guava:guava", version="32.0.0")
        result = strategy.install_app_package(
            session=None,
            spec=spec,
            request=request,
            version_constraint="any_compatible",
            run_cmd=_success_cmd("[INFO] BUILD SUCCESS"),
            workspace_path=tmp_path,
        )
        assert result.success is True
        assert result.installed_version == "32.0.0"
        assert "pom.xml" in result.manifest_files_changed
        # pom.xml should now contain the dependency
        tree = ET.parse(str(pom))
        root = tree.getroot()
        ns = "{http://maven.apache.org/POM/4.0.0}"
        dep = root.find(f"{ns}dependencies/{ns}dependency")
        assert dep is not None
        assert dep.findtext(f"{ns}artifactId") == "guava"

    def test_install_gradle_with_real_file(self, tmp_path):
        gradle = tmp_path / "build.gradle"
        gradle.write_text(_GRADLE_WITH_DEPS)
        strategy = JvmStrategy()
        spec = _spec("java_gradle")
        request = PackageRequest(name="com.google.guava:guava", version="32.0.0")
        result = strategy.install_app_package(
            session=None,
            spec=spec,
            request=request,
            version_constraint="any_compatible",
            run_cmd=_success_cmd("BUILD SUCCESSFUL"),
            workspace_path=tmp_path,
        )
        assert result.success is True
        assert "build.gradle" in result.manifest_files_changed
        assert "com.google.guava:guava:32.0.0" in gradle.read_text()

    def test_install_flutter_pubspec(self, tmp_path):
        (tmp_path / "pubspec.yaml").write_text("name: myapp\n")
        strategy = JvmStrategy()
        spec = _spec("android_gradle")
        request = PackageRequest(name="http", version="1.1.0")
        result = strategy.install_app_package(
            session=None,
            spec=spec,
            request=request,
            version_constraint="any_compatible",
            run_cmd=_success_cmd("+ http 1.1.0"),
            workspace_path=tmp_path,
        )
        assert result.success is True
        assert "pubspec.yaml" in result.manifest_files_changed

    def test_install_maven_no_pom_file(self, tmp_path):
        strategy = JvmStrategy()
        spec = _spec("java_maven")
        request = PackageRequest(name="com.google.guava:guava", version="32.0.0")
        result = strategy.install_app_package(
            session=None,
            spec=spec,
            request=request,
            version_constraint="any_compatible",
            run_cmd=_success_cmd(),
            workspace_path=tmp_path,
        )
        assert result.success is False
        assert "pom.xml" in (result.error or "")

    def test_install_gradle_no_build_file(self, tmp_path):
        strategy = JvmStrategy()
        spec = _spec("java_gradle")
        request = PackageRequest(name="com.google.guava:guava", version="32.0.0")
        result = strategy.install_app_package(
            session=None,
            spec=spec,
            request=request,
            version_constraint="any_compatible",
            run_cmd=_success_cmd(),
            workspace_path=tmp_path,
        )
        assert result.success is False
        assert "build.gradle" in (result.error or "")

    def test_gradle_uses_wrapper_if_present(self, tmp_path):
        gradle = tmp_path / "build.gradle"
        gradle.write_text(_GRADLE_WITH_DEPS)
        gradlew = tmp_path / "gradlew"
        gradlew.write_text("#!/bin/sh\nexec gradle $@\n")
        strategy = JvmStrategy()
        spec = _spec("java_gradle")
        request = PackageRequest(name="com.google.guava:guava", version="32.0.0")
        commands_used = []

        def cmd(command: str) -> tuple[int, str, str]:
            commands_used.append(command)
            return 0, "BUILD SUCCESSFUL", ""

        strategy.install_app_package(
            session=None,
            spec=spec,
            request=request,
            version_constraint="any_compatible",
            run_cmd=cmd,
            workspace_path=tmp_path,
        )
        # The resolution command should use ./gradlew
        assert any("./gradlew" in c for c in commands_used)

    def test_android_gradle_prefers_app_subdir(self, tmp_path):
        app = tmp_path / "app"
        app.mkdir()
        (app / "build.gradle").write_text(_GRADLE_WITH_DEPS)
        (tmp_path / "build.gradle").write_text(_GRADLE_WITH_DEPS)
        strategy = JvmStrategy()
        spec = _spec("android_gradle")
        request = PackageRequest(name="com.google.firebase:firebase-bom", version="32.0.0")
        result = strategy.install_app_package(
            session=None,
            spec=spec,
            request=request,
            version_constraint="any_compatible",
            run_cmd=_success_cmd("BUILD SUCCESSFUL"),
            workspace_path=tmp_path,
        )
        assert result.success is True
        # Manifest file should be the app-level build.gradle
        assert any("app" in f for f in result.manifest_files_changed)


# ---------------------------------------------------------------------------
# manifest_files_changed propagation through EnvironmentManager.install()
# ---------------------------------------------------------------------------


class TestManifestFilesChangedPropagation:
    def test_maven_manifest_files_in_output(self, tmp_path):
        """manifest_files_changed from strategy propagates to EnvironmentManagerOutput."""
        pom = tmp_path / "pom.xml"
        pom.write_text(_MINIMAL_POM)
        spec = RuntimeSpec(runtime_type="java_maven", image="maven:3.9")
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
            output = EnvironmentManager().install(request=request, spec=spec)
        assert output.status == "success"
        assert "pom.xml" in output.manifest_files_changed

    def test_gradle_manifest_files_in_output(self, tmp_path):
        gradle = tmp_path / "build.gradle"
        gradle.write_text(_GRADLE_WITH_DEPS)
        spec = RuntimeSpec(runtime_type="java_gradle", image="gradle:8")
        request = EnvironmentManagerRequest(
            packages=[PackageRequest(name="com.google.guava:guava", version="32.0.0")],
            version_constraint="any_compatible",
            project_id=1,
            workspace_path=str(tmp_path),
        )
        with patch(
            "app.services.environment.manager._build_run_cmd",
            return_value=_success_cmd("BUILD SUCCESSFUL"),
        ):
            output = EnvironmentManager().install(request=request, spec=spec)
        assert output.status == "success"
        assert "build.gradle" in output.manifest_files_changed

    def test_rust_manifest_files_in_output(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text('[package]\nname = "x"\n')
        spec = RuntimeSpec(runtime_type="rust_cargo", image="rust:stable")
        request = EnvironmentManagerRequest(
            packages=[PackageRequest(name="serde", version="1.0")],
            version_constraint="any_compatible",
            project_id=1,
            workspace_path=str(tmp_path),
        )
        with patch(
            "app.services.environment.manager._build_run_cmd",
            return_value=_success_cmd("    Adding serde v1.0.196 to dependencies"),
        ):
            output = EnvironmentManager().install(request=request, spec=spec)
        assert output.status == "success"
        assert "Cargo.toml" in output.manifest_files_changed
        assert "Cargo.lock" in output.manifest_files_changed

    def test_go_manifest_files_in_output(self, tmp_path):
        (tmp_path / "go.mod").write_text("module example.com/x\n\ngo 1.21\n")
        spec = RuntimeSpec(runtime_type="go", image="golang:1.21")
        request = EnvironmentManagerRequest(
            packages=[PackageRequest(name="github.com/gin-gonic/gin", version="v1.9.0")],
            version_constraint="any_compatible",
            project_id=1,
            workspace_path=str(tmp_path),
        )
        with patch(
            "app.services.environment.manager._build_run_cmd",
            return_value=_success_cmd("go: added github.com/gin-gonic/gin v1.9.0"),
        ):
            output = EnvironmentManager().install(request=request, spec=spec)
        assert output.status == "success"
        assert "go.mod" in output.manifest_files_changed

    def test_python_no_manifest_files(self, tmp_path):
        """Python/pip doesn't edit manifest files directly."""
        spec = RuntimeSpec(runtime_type="python_venv", image="python:3.12")
        request = EnvironmentManagerRequest(
            packages=[PackageRequest(name="requests", version="2.31.0")],
            version_constraint="any_compatible",
            project_id=1,
            workspace_path=str(tmp_path),
        )
        with patch(
            "app.services.environment.manager._build_run_cmd",
            return_value=_success_cmd("Successfully installed requests-2.31.0"),
        ):
            output = EnvironmentManager().install(request=request, spec=spec)
        assert output.status == "success"
        assert output.manifest_files_changed == []

    def test_failed_install_still_returns_manifest_files(self, tmp_path):
        """Even on failure, any manifest files changed before failure are included."""
        # First package succeeds (edits pom.xml), second fails
        pom = tmp_path / "pom.xml"
        pom.write_text(_MINIMAL_POM)
        spec = RuntimeSpec(runtime_type="java_maven", image="maven:3.9")
        request = EnvironmentManagerRequest(
            packages=[
                PackageRequest(name="com.google.guava:guava", version="32.0.0"),
                PackageRequest(name="com.bad:package", version="0.0.1"),
            ],
            version_constraint="any_compatible",
            project_id=1,
            workspace_path=str(tmp_path),
        )
        call_count = [0]

        def cmd(command: str) -> tuple[int, str, str]:
            call_count[0] += 1
            # First mvn call succeeds, second fails
            if call_count[0] == 1:
                return 0, "[INFO] BUILD SUCCESS", ""
            return 1, "", "BUILD FAILURE"

        with patch("app.services.environment.manager._build_run_cmd", return_value=cmd):
            output = EnvironmentManager().install(request=request, spec=spec)
        assert output.status == "partial_success"
        assert "pom.xml" in output.manifest_files_changed


# ---------------------------------------------------------------------------
# EnvironmentManager — fix for test_install_requires_rebuild_propagated
# (this test was broken by the new pom.xml existence check in _install_maven)
# ---------------------------------------------------------------------------


def test_install_requires_rebuild_jvm(tmp_path):
    """JVM strategy sets requires_rebuild=True — tested with a real pom.xml."""
    pom = tmp_path / "pom.xml"
    pom.write_text(_MINIMAL_POM)
    spec = RuntimeSpec(runtime_type="java_maven", image="maven:3.9")
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
        output = EnvironmentManager().install(request=request, spec=spec)
    assert output.requires_rebuild is True
