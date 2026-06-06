from __future__ import annotations

from app.execution_engine.contracts import ExecutionEvidence


class TestAddFileDocumentation:
    def test_adds_new_entry(self) -> None:
        evidence = ExecutionEvidence()
        evidence.add_file_documentation(
            path="app/service.py",
            documentation="Core service module",
            change_summary="Initial implementation",
            agent="code_change_agent",
            operation="create",
        )
        assert len(evidence.file_documentations) == 1
        entry = evidence.file_documentations[0]
        assert entry.path == "app/service.py"
        assert entry.documentation == "Core service module"
        assert entry.change_summary == "Initial implementation"
        assert entry.agent == "code_change_agent"
        assert entry.operation == "create"

    def test_upsert_replaces_existing_entry_for_same_path(self) -> None:
        evidence = ExecutionEvidence()
        evidence.add_file_documentation(
            path="app/service.py",
            documentation="Original docs",
            change_summary="First write",
            agent="code_change_agent",
            operation="create",
        )
        evidence.add_file_documentation(
            path="app/service.py",
            documentation="Updated docs with refund support",
            change_summary="Added refund method",
            agent="code_change_agent",
            operation="modify",
        )
        # Should have only one entry (upsert semantics)
        assert len(evidence.file_documentations) == 1
        entry = evidence.file_documentations[0]
        assert entry.documentation == "Updated docs with refund support"
        assert entry.operation == "modify"

    def test_different_paths_accumulate(self) -> None:
        evidence = ExecutionEvidence()
        evidence.add_file_documentation(
            path="app/service.py",
            documentation="Service docs",
            change_summary="Initial",
            agent="code_change_agent",
            operation="create",
        )
        evidence.add_file_documentation(
            path="app/models.py",
            documentation="Model docs",
            change_summary="Initial",
            agent="code_change_agent",
            operation="create",
        )
        assert len(evidence.file_documentations) == 2
        paths = {e.path for e in evidence.file_documentations}
        assert paths == {"app/service.py", "app/models.py"}

    def test_default_file_documentations_is_empty(self) -> None:
        evidence = ExecutionEvidence()
        assert evidence.file_documentations == []
