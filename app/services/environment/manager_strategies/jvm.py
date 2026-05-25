"""JVM (Maven / Gradle) and Flutter dependency strategy.

Design contract
--------------
Unlike pip/npm, JVM build systems (Maven, Gradle) and Flutter cannot install a
new dependency via a single command — the dependency must first be declared in
the project's manifest file before the build system can download it:

  Maven  → add <dependency> block to pom.xml    → mvn dependency:resolve -q
  Gradle → add implementation() line to build.gradle → ./gradlew build -q
  Flutter → flutter pub add <pkg>  (updates pubspec.yaml atomically)

This strategy performs BOTH steps:
  1. Edit the manifest file in-place on the host filesystem (which is the same
     file the container sees via the read-write volume mount at /project).
  2. Run the build-system command to download / resolve the dependency.

Manifest files modified during installation are reported in
InstallResult.manifest_files_changed so the orchestrator can register them
as repository file changes in evidence.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from pathlib import Path

from app.services.environment.contracts import EnvironmentSession, PinnedDependency, RuntimeSpec
from app.services.environment.manager_contracts import PackageRequest
from app.services.environment.manager_strategies.base import (
    BasePackageStrategy,
    CmdFn,
    InstallResult,
)

logger = logging.getLogger(__name__)

# Maven POM namespace — must be registered before parsing so ElementTree
# uses "" prefix (default namespace) instead of "ns0:" when writing back.
_MAVEN_NS = "http://maven.apache.org/POM/4.0.0"
_XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
ET.register_namespace("", _MAVEN_NS)
ET.register_namespace("xsi", _XSI_NS)


# ---------------------------------------------------------------------------
# Maven coordinate helpers
# ---------------------------------------------------------------------------


def _parse_maven_coord(name: str, version: str | None) -> tuple[str, str, str | None]:
    """Parse a Maven coordinate string into (groupId, artifactId, version).

    Accepts:
      "com.google.guava:guava"           → ("com.google.guava", "guava", version_arg)
      "com.google.guava:guava:32.0.0"    → ("com.google.guava", "guava", "32.0.0")
      "guava"                            → ("guava", "guava", version_arg)   # fallback
    """
    parts = name.strip().split(":")
    if len(parts) >= 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 2:
        return parts[0], parts[1], version
    return name, name, version


def _edit_pom_xml(pom_path: Path, group_id: str, artifact_id: str, version: str | None) -> None:
    """Add or update a <dependency> in pom.xml using ElementTree.

    Preserves existing dependencies; idempotent (updates version if already present).
    Reformats the XML with consistent indentation (Python 3.9+ ET.indent).
    """
    tree = ET.parse(str(pom_path))
    root = tree.getroot()

    # Detect whether the file uses the Maven namespace or is namespace-free.
    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}")[0] + "}"

    def _tag(local: str) -> str:
        return f"{ns}{local}"

    # Find or create <dependencies>
    deps_elem = root.find(_tag("dependencies"))
    if deps_elem is None:
        deps_elem = ET.SubElement(root, _tag("dependencies"))

    # Check for an existing entry matching groupId + artifactId
    for dep in deps_elem.findall(_tag("dependency")):
        existing_gid = dep.findtext(_tag("groupId"), "")
        existing_aid = dep.findtext(_tag("artifactId"), "")
        if existing_gid == group_id and existing_aid == artifact_id:
            if version:
                ver_elem = dep.find(_tag("version"))
                if ver_elem is not None:
                    ver_elem.text = version
                else:
                    ET.SubElement(dep, _tag("version")).text = version
            logger.info(
                "pom_xml_dependency_updated group=%s artifact=%s version=%s",
                group_id,
                artifact_id,
                version,
            )
            ET.indent(tree, space="    ")
            tree.write(str(pom_path), xml_declaration=True, encoding="UTF-8")
            return

    # New dependency
    dep = ET.SubElement(deps_elem, _tag("dependency"))
    ET.SubElement(dep, _tag("groupId")).text = group_id
    ET.SubElement(dep, _tag("artifactId")).text = artifact_id
    if version:
        ET.SubElement(dep, _tag("version")).text = version

    logger.info(
        "pom_xml_dependency_added group=%s artifact=%s version=%s", group_id, artifact_id, version
    )
    ET.indent(tree, space="    ")
    tree.write(str(pom_path), xml_declaration=True, encoding="UTF-8")


# ---------------------------------------------------------------------------
# Gradle build file helpers
# ---------------------------------------------------------------------------


def _find_gradle_file(workspace_path: Path, prefer_app_subdir: bool = False) -> Path | None:
    """Locate the primary build.gradle (or .kts) to edit.

    For Android projects the app-level build file takes priority over the
    project-level one because module dependencies go in app/build.gradle.
    """
    candidates = []
    if prefer_app_subdir:
        app_dir = workspace_path / "app"
        candidates += [
            app_dir / "build.gradle.kts",
            app_dir / "build.gradle",
        ]
    candidates += [
        workspace_path / "build.gradle.kts",
        workspace_path / "build.gradle",
    ]
    for path in candidates:
        if path.exists():
            return path
    return None


def _find_deps_block_end(content: str) -> int | None:
    """Return the index of the closing '}' of the outermost 'dependencies { }' block.

    Uses brace counting to handle nested blocks (e.g. excludes { }).
    Returns None if no dependencies block is found.
    """
    search_start = 0
    while True:
        idx = content.find("dependencies", search_start)
        if idx == -1:
            return None
        # Skip 'dependenciesResolutionStrategy' and similar false matches
        after = content[idx + len("dependencies") :].lstrip()
        if not after.startswith("{"):
            search_start = idx + len("dependencies")
            continue
        brace_start = content.index("{", idx)
        depth = 0
        for i in range(brace_start, len(content)):
            if content[i] == "{":
                depth += 1
            elif content[i] == "}":
                depth -= 1
                if depth == 0:
                    return i
        return None  # Unbalanced braces


def _edit_gradle_file(
    gradle_path: Path,
    group_id: str,
    artifact_id: str,
    version: str | None,
) -> None:
    """Add an implementation() line to the dependencies {} block.

    Handles both Groovy DSL (.gradle) and Kotlin DSL (.gradle.kts).
    Idempotent: skips the edit if the coordinate already appears in the file.
    If no dependencies block exists, appends one.
    """
    content = gradle_path.read_text(encoding="utf-8")
    is_kts = gradle_path.suffix == ".kts"

    coord = f"{group_id}:{artifact_id}"
    if version:
        coord = f"{group_id}:{artifact_id}:{version}"

    # Idempotency check — use artifact_id as the unique key (avoids false misses
    # when the existing entry has a different version format).
    if f"{group_id}:{artifact_id}" in content:
        logger.info("gradle_dependency_already_present coord=%s", coord)
        return

    if is_kts:
        dep_line = f'    implementation("{coord}")\n'
    else:
        dep_line = f"    implementation '{coord}'\n"

    end_idx = _find_deps_block_end(content)
    if end_idx is not None:
        new_content = content[:end_idx] + dep_line + content[end_idx:]
    else:
        new_content = content + f"\ndependencies {{\n{dep_line}}}\n"

    gradle_path.write_text(new_content, encoding="utf-8")
    logger.info("gradle_dependency_added coord=%s file=%s", coord, gradle_path.name)


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------


class JvmStrategy(BasePackageStrategy):
    """Installs JVM (Maven / Gradle) dependencies and handles Flutter projects.

    Covers runtime_types: java_maven, java_gradle, android_gradle.

    For Maven:   edits pom.xml → mvn dependency:resolve
    For Gradle:  edits build.gradle(.kts) → ./gradlew build
    For Flutter: flutter pub add (auto-edits pubspec.yaml)
    """

    @property
    def runtime_types(self) -> frozenset[str]:
        return frozenset({"java_maven", "java_gradle", "android_gradle"})

    def detect(self, project_root: Path) -> bool:
        return (
            (project_root / "pom.xml").exists()
            or (project_root / "pubspec.yaml").exists()
            or any((project_root / f).exists() for f in ["build.gradle", "build.gradle.kts"])
        )

    def install_app_package(
        self,
        *,
        session: EnvironmentSession,
        spec: RuntimeSpec,
        request: PackageRequest,
        version_constraint: str,
        run_cmd: CmdFn,
        workspace_path: Path | None = None,
    ) -> InstallResult:
        runtime = spec.runtime_type if spec else None

        # Flutter projects use pubspec.yaml regardless of runtime_type label.
        if workspace_path and (workspace_path / "pubspec.yaml").exists():
            return self._install_flutter(
                request=request,
                run_cmd=run_cmd,
                workspace_path=workspace_path,
            )

        if runtime == "java_maven" or (
            workspace_path and (workspace_path / "pom.xml").exists() and runtime != "java_gradle"
        ):
            return self._install_maven(
                request=request,
                version_constraint=version_constraint,
                run_cmd=run_cmd,
                workspace_path=workspace_path,
            )

        # java_gradle / android_gradle / fallback
        return self._install_gradle(
            request=request,
            version_constraint=version_constraint,
            run_cmd=run_cmd,
            workspace_path=workspace_path,
            is_android=(runtime == "android_gradle"),
        )

    # ------------------------------------------------------------------
    # Maven
    # ------------------------------------------------------------------

    def _install_maven(
        self,
        *,
        request: PackageRequest,
        version_constraint: str,
        run_cmd: CmdFn,
        workspace_path: Path | None,
    ) -> InstallResult:
        group_id, artifact_id, version = _parse_maven_coord(request.name, request.version)
        manifest_changed: list[str] = []

        if workspace_path is not None:
            pom_path = workspace_path / "pom.xml"
            if not pom_path.exists():
                return InstallResult(
                    success=False,
                    installed_version=None,
                    version_adjusted=False,
                    output="",
                    error=(
                        f"pom.xml not found at {pom_path}. "
                        "Ensure the Maven project is initialised before adding dependencies."
                    ),
                )
            try:
                _edit_pom_xml(pom_path, group_id, artifact_id, version)
                manifest_changed.append("pom.xml")
            except Exception as exc:
                return InstallResult(
                    success=False,
                    installed_version=None,
                    version_adjusted=False,
                    output="",
                    error=f"Failed to edit pom.xml: {exc}",
                )

        # Resolve / download the dependency
        exit_code, stdout, stderr = run_cmd("mvn dependency:resolve -q 2>&1 | tail -30")
        combined = f"{stdout}\n{stderr}".strip()
        if exit_code != 0:
            return InstallResult(
                success=False,
                installed_version=None,
                version_adjusted=False,
                output=combined,
                error=f"mvn dependency:resolve failed: {stderr or stdout}",
            )

        return InstallResult(
            success=True,
            installed_version=version or "declared",
            version_adjusted=False,
            output=combined,
            manifest_files_changed=manifest_changed,
        )

    # ------------------------------------------------------------------
    # Gradle
    # ------------------------------------------------------------------

    def _install_gradle(
        self,
        *,
        request: PackageRequest,
        version_constraint: str,
        run_cmd: CmdFn,
        workspace_path: Path | None,
        is_android: bool = False,
    ) -> InstallResult:
        group_id, artifact_id, version = _parse_maven_coord(request.name, request.version)
        manifest_changed: list[str] = []

        if workspace_path is not None:
            gradle_path = _find_gradle_file(workspace_path, prefer_app_subdir=is_android)
            if gradle_path is None:
                return InstallResult(
                    success=False,
                    installed_version=None,
                    version_adjusted=False,
                    output="",
                    error=(
                        "No build.gradle or build.gradle.kts found. "
                        "Ensure the Gradle project is initialised before adding dependencies."
                    ),
                )
            try:
                _edit_gradle_file(gradle_path, group_id, artifact_id, version)
                # Record relative path (e.g. "app/build.gradle" or "build.gradle")
                manifest_changed.append(str(gradle_path.relative_to(workspace_path)))
            except Exception as exc:
                return InstallResult(
                    success=False,
                    installed_version=None,
                    version_adjusted=False,
                    output="",
                    error=f"Failed to edit {gradle_path.name}: {exc}",
                )

        # Use the Gradle wrapper if available, otherwise system gradle
        wrapper = (
            "./gradlew" if (workspace_path and (workspace_path / "gradlew").exists()) else "gradle"
        )
        exit_code, stdout, stderr = run_cmd(
            f"{wrapper} dependencies --configuration compileClasspath -q 2>&1 | tail -30"
        )
        combined = f"{stdout}\n{stderr}".strip()
        if exit_code != 0:
            return InstallResult(
                success=False,
                installed_version=None,
                version_adjusted=False,
                output=combined,
                error=f"Gradle dependency resolution failed: {stderr or stdout}",
            )

        return InstallResult(
            success=True,
            installed_version=version or "declared",
            version_adjusted=False,
            output=combined,
            manifest_files_changed=manifest_changed,
        )

    # ------------------------------------------------------------------
    # Flutter
    # ------------------------------------------------------------------

    def _install_flutter(
        self,
        *,
        request: PackageRequest,
        run_cmd: CmdFn,
        workspace_path: Path | None,
    ) -> InstallResult:
        pkg_spec = request.name
        if request.version:
            pkg_spec = f"{request.name}:{request.version}"

        # flutter pub add updates pubspec.yaml and runs flutter pub get atomically
        exit_code, stdout, stderr = run_cmd(f"flutter pub add {pkg_spec} 2>&1")
        combined = f"{stdout}\n{stderr}".strip()
        if exit_code != 0:
            return InstallResult(
                success=False,
                installed_version=None,
                version_adjusted=False,
                output=combined,
                error=f"flutter pub add failed: {stderr or stdout}",
            )

        return InstallResult(
            success=True,
            installed_version=request.version or "latest",
            version_adjusted=False,
            output=combined,
            manifest_files_changed=["pubspec.yaml", "pubspec.lock"],
        )

    # ------------------------------------------------------------------
    # Manifest (RuntimeSpec DB record)
    # ------------------------------------------------------------------

    def update_manifest(
        self,
        *,
        spec: RuntimeSpec,
        name: str,
        installed_version: str,
    ) -> RuntimeSpec:
        deps = list(spec.dependencies)
        for i, dep in enumerate(deps):
            if dep.name.lower() == name.lower():
                deps[i] = PinnedDependency(name=dep.name, version=installed_version)
                return spec.model_copy(update={"dependencies": deps})
        deps.append(PinnedDependency(name=name, version=installed_version))
        return spec.model_copy(update={"dependencies": deps})

    @property
    def requires_rebuild(self) -> bool:
        # JVM and Gradle projects always need a full build after dependency changes.
        # Flutter pub add already runs flutter pub get, so no additional step.
        return True

    @property
    def strategy_name(self) -> str:
        return "jvm"
