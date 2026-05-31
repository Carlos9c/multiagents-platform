"""APK installer QA agent (mobile_android only).

Installs the compiled APK on the host Android emulator via ADB and
verifies that the application launches without crashing.

Preconditions:
- An Android emulator must be running on the host (Android Studio AVD).
- ADB must be available in PATH on the host running this service.
- request.artifact_path must be set (QABootstrapper ensures this).
"""

from __future__ import annotations

import logging
import subprocess

from sqlalchemy.orm import Session

from app.services.qa.agents.base import BaseQAAgent
from app.services.qa.contracts import ProbeRecord, QAFindingDetail, QARequest
from app.services.qa.qa_session import QASession

logger = logging.getLogger(__name__)

_ADB_TIMEOUT = 60  # seconds per ADB command
_LAUNCH_WAIT = 3  # seconds to wait after launch before checking for crash


def _run_adb(args: list[str], timeout: int = _ADB_TIMEOUT) -> tuple[int, str, str]:
    """Run an ADB command on the host. Returns (exit_code, stdout, stderr)."""
    try:
        proc = subprocess.run(
            ["adb"] + args,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 1, "", f"ADB command timed out after {timeout}s"
    except FileNotFoundError:
        return 1, "", "adb not found in PATH"
    except Exception as exc:
        return 1, "", str(exc)


def _get_package_name(apk_path: str) -> str | None:
    """Extract the package name from the APK using aapt or aapt2."""
    for tool in ["aapt", "aapt2"]:
        code, stdout, _ = _run_adb([], timeout=1)  # dummy — aapt runs standalone
        try:
            proc = subprocess.run(
                [tool, "dump", "badging", apk_path],
                capture_output=True,
                text=True,
                timeout=30,
            )
            for line in proc.stdout.splitlines():
                if line.startswith("package: name="):
                    # package: name='com.example.app' versionCode='1' ...
                    parts = line.split("'")
                    if len(parts) >= 2:
                        return parts[1]
        except (FileNotFoundError, subprocess.TimeoutExpired, Exception):
            continue
    return None


class ApkInstallerAgent(BaseQAAgent):
    """Installs the APK on the Android emulator and verifies launch."""

    @property
    def name(self) -> str:
        return "apk_installer"

    def probe(self, *, db: Session, request: QARequest, session: QASession) -> QASession:
        session.record_agent_call(self.name)

        apk_path = request.artifact_path
        if not apk_path:
            session.add_probe(
                ProbeRecord(
                    agent_name=self.name,
                    probe_type="apk_install",
                    target="",
                    outcome="skipped",
                    notes="No artifact_path set — QABootstrapper did not provide an APK",
                )
            )
            return session

        # ── Step 1: Check for connected emulator ──────────────────────────────
        code, stdout, stderr = _run_adb(["devices"])
        emulator_lines = [
            ln for ln in stdout.splitlines() if "emulator" in ln.lower() and "device" in ln
        ]
        if not emulator_lines:
            session.add_probe(
                ProbeRecord(
                    agent_name=self.name,
                    probe_type="apk_install",
                    target=apk_path,
                    outcome="skipped",
                    notes="No running Android emulator detected (adb devices returned no emulator)",
                )
            )
            return session

        # ── Step 2: Install APK ───────────────────────────────────────────────
        code, stdout, stderr = _run_adb(["install", "-r", apk_path])
        if code != 0 or "Failure" in stdout or "Failure" in stderr:
            error_detail = (stdout + stderr)[:500]
            session.add_finding(
                QAFindingDetail(
                    finding_id="apk-001",
                    severity="critical",
                    category="functional",
                    title="APK installation failed",
                    description=f"The APK could not be installed on the emulator: {error_detail}",
                    reproduction_steps=[f"adb install -r {apk_path}"],
                    affected_component="APK package",
                    auto_remediable=False,
                    remediation_hint="Check the build output for signing or manifest errors",
                )
            )
            session.add_probe(
                ProbeRecord(
                    agent_name=self.name,
                    probe_type="apk_install",
                    target=apk_path,
                    outcome="failed",
                    findings_count=1,
                    notes=f"Install failed: {error_detail[:200]}",
                )
            )
            return session

        session.add_probe(
            ProbeRecord(
                agent_name=self.name,
                probe_type="apk_install",
                target=apk_path,
                outcome="passed",
                notes="APK installed successfully",
            )
        )

        # ── Step 3: Launch and smoke test ─────────────────────────────────────
        package_name = _get_package_name(apk_path)
        if package_name is None:
            session.add_note("Could not extract package name from APK — skipping launch test")
            return session

        # Use monkey to send 1 random event (effectively just launches the app)
        code, stdout, stderr = _run_adb(
            ["shell", "monkey", "-p", package_name, "-c", "android.intent.category.LAUNCHER", "1"]
        )
        launch_output = (stdout + stderr)[:500]

        if code != 0 or "error" in launch_output.lower() or "exception" in launch_output.lower():
            session.add_finding(
                QAFindingDetail(
                    finding_id="apk-002",
                    severity="critical",
                    category="functional",
                    title="Application crashes on launch",
                    description=f"The app crashed immediately after launch: {launch_output}",
                    reproduction_steps=[
                        f"adb install -r {apk_path}",
                        f"adb shell monkey -p {package_name} -c android.intent.category.LAUNCHER 1",
                    ],
                    affected_component=package_name,
                    auto_remediable=False,
                    remediation_hint="Check logcat for crash stacktrace: adb logcat -d | grep -A 20 AndroidRuntime",
                )
            )
            session.add_probe(
                ProbeRecord(
                    agent_name=self.name,
                    probe_type="launch_smoke_test",
                    target=package_name,
                    outcome="failed",
                    findings_count=1,
                    notes=f"App crash on launch: {launch_output[:200]}",
                )
            )
        else:
            session.add_probe(
                ProbeRecord(
                    agent_name=self.name,
                    probe_type="launch_smoke_test",
                    target=package_name,
                    outcome="passed",
                    notes="App launched without crashing",
                )
            )

        logger.info(
            "apk_installer_complete project_id=%s package=%s findings=%s",
            request.project_id,
            package_name,
            sum(1 for p in session.probes if p.agent_name == self.name and p.outcome == "failed"),
        )
        return session
