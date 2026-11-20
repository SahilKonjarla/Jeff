"""Actual branch/config/changes dispatch, immutable receipts and child-reaped timeout facts."""

import os
import time
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from jeff.application.dispatch import ProjectCall, ProjectDispatcher, ReceiptQuery
from jeff.application.projects import Failure, ProjectService
from jeff.application.questions import QuestionService, QuestionSuccess
from jeff.application.runtime import RuntimeIdentityData, RuntimeLayout
from jeff.application.storage import ReceiptScope
from jeff.domain.projects import Project
from jeff.infrastructure.project_files import FileProjectStore
from jeff.infrastructure.runtime_state import RuntimeStateStore
from jeff.infrastructure.runtime_storage import SQLiteStorage
from jeff.integrations import git as git_adapter
from jeff.integrations.git import GitProjectDiscovery, GitRepositoryRead


def test_branch_receipt_duplicate_and_scope_without_answer_archive(
    git_repo: Path, tmp_path: Path
) -> None:
    projects = ProjectService(GitProjectDiscovery(), FileProjectStore())
    assert projects.execute("init", git_repo).ok
    identity = RuntimeIdentityData(instance_id=uuid4(), pid=os.getpid())
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    with owner:
        owner.store.credential(owner)
        storage = SQLiteStorage(owner, instance_id=identity.instance_id)
        storage.start()
        try:
            assert storage.wait_ready(5).state == "ready"
            dispatch = ProjectDispatcher(
                projects, QuestionService(projects, GitRepositoryRead()), storage, identity
            )
            call = ProjectCall(operation="ask", directory=git_repo, question="what branch am I on?")
            first = dispatch.execute(call, time.monotonic() + 10)
            assert isinstance(first.result, QuestionSuccess) and first.accepted
            assert first.result.data.kind == "branch" and first.result.data.branch.name == "main"
            assert first.receipt is not None and first.receipt.state == "succeeded"
            again = dispatch.execute(call, time.monotonic() + 10)
            assert (
                isinstance(again.result, Failure) and again.result.error.code == "duplicate_request"
            )
            assert again.receipt == first.receipt
            inspected = dispatch.inspect_receipt(
                ReceiptQuery(directory=git_repo, request_id=call.request_id), time.monotonic() + 10
            )
            assert inspected.receipt == first.receipt
            _, trace = storage.trace(
                ReceiptScope(
                    request_id=call.request_id,
                    canonical_root=git_repo,
                    project_id=UUID(first.result.data.project_id),
                ),
                deadline=time.monotonic() + 5,
            )
            assert trace is not None
            observed = [event.git for event in trace.events if event.git is not None]
            assert [event.category for event in observed] == ["filters.inspect", "status.branch"]
            assert all(event.code == "ok" and event.elapsed_ms >= 0 for event in observed)
            assert observed[-1].output_bytes > 0
            wrong = dispatch.execute(
                call.model_copy(update={"request_id": uuid4(), "expected_project_id": uuid4()}),
                time.monotonic() + 10,
            )
            assert (
                isinstance(wrong.result, Failure)
                and wrong.result.error.code == "project_target_mismatch"
            )
            assert wrong.accepted is False
        finally:
            storage.close()
    assert b"what branch am I on" not in layout.database.read_bytes()


def test_config_is_read_only_unindexed_and_changes_use_existing_policy(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projects = ProjectService(GitProjectDiscovery(), FileProjectStore())
    registered = projects.execute("init", git_repo)
    assert registered.ok
    project_id = UUID(registered.data.project_id)
    identity = RuntimeIdentityData(instance_id=uuid4(), pid=os.getpid())
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    with owner:
        owner.store.credential(owner)
        storage = SQLiteStorage(owner, instance_id=identity.instance_id)
        storage.start()
        try:
            assert storage.wait_ready(5).state == "ready"
            dispatch = ProjectDispatcher(
                projects, QuestionService(projects, GitRepositoryRead()), storage, identity
            )
            config = ProjectCall(operation="config", directory=git_repo)
            before = layout.database.read_bytes(), layout.database.stat().st_mtime_ns
            result = dispatch.execute(config, time.monotonic() + 10)
            assert result.result.ok and result.result.data.project_id == str(project_id), result
            assert result.accepted is False and result.receipt is None
            assert (layout.database.read_bytes(), layout.database.stat().st_mtime_ns) == before
            assert storage.lookup(Project(project_id, git_repo)) is None
            assert (
                storage.inspect(
                    ReceiptScope(
                        request_id=config.request_id, canonical_root=git_repo, project_id=project_id
                    ),
                    deadline=time.monotonic() + 5,
                )
                is None
            )
            (git_repo / "note.txt").write_text("SYNTHETIC_PRIVATE_CONTENT")
            changed = dispatch.execute(
                ProjectCall(operation="ask", directory=git_repo, question="what changed?"),
                time.monotonic() + 10,
            )
            assert (
                isinstance(changed.result, QuestionSuccess)
                and changed.result.data.kind == "changes"
            ), changed
            assert "note.txt" in {change.path for change in changed.result.data.changes}
            assert changed.receipt is not None and changed.receipt.operation == "ask.changes"
            assert changed.receipt.state == "succeeded"
            for number in range(4):
                (git_repo / (str(number) + "x" * 200)).write_text("synthetic")
            with monkeypatch.context() as patch:
                patch.setattr(git_adapter, "GIT_OUTPUT_LIMIT", 512)
                bounded = dispatch.execute(
                    ProjectCall(operation="ask", directory=git_repo, question="what changed?"),
                    time.monotonic() + 10,
                )
            assert not bounded.result.ok and bounded.result.error.code == "git_output_limit", (
                bounded
            )
            assert (
                bounded.accepted
                and bounded.receipt is not None
                and bounded.receipt.state == "failed"
            )
            _, evidence = storage.trace(
                ReceiptScope(
                    request_id=bounded.request_id, canonical_root=git_repo, project_id=project_id
                ),
                deadline=time.monotonic() + 5,
            )
            assert evidence is not None and evidence.events[-1].code == "git_output_limit"
            assert any(
                event.git is not None
                and event.git.process_started
                and event.git.code == "git_output_limit"
                for event in evidence.events
            )
        finally:
            storage.close()
    assert b"SYNTHETIC_PRIVATE_CONTENT" not in layout.database.read_bytes()


def test_real_git_timeout_evidence_is_recorded_after_actual_child_reaping(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    projects = ProjectService(GitProjectDiscovery(), FileProjectStore())
    assert projects.execute("init", git_repo).ok
    identity = RuntimeIdentityData(instance_id=uuid4(), pid=os.getpid())
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    original = subprocess.Popen
    children: list[subprocess.Popen[bytes]] = []

    def launch(command: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        child = original(command, **kwargs)
        children.append(child)
        if "status" in command:
            patch.setattr(git_adapter, "GIT_TIMEOUT", 0)
        return child

    with owner:
        owner.store.credential(owner)
        store = SQLiteStorage(owner, instance_id=identity.instance_id)
        store.start()
        try:
            assert store.wait_ready(5).state == "ready"
            dispatch = ProjectDispatcher(
                projects, QuestionService(projects, GitRepositoryRead()), store, identity
            )
            call = ProjectCall(operation="ask", directory=git_repo, question="what branch am I on?")
            with monkeypatch.context() as patch:
                patch.setattr(subprocess, "Popen", launch)
                result = dispatch.execute(call, time.monotonic() + 10)
            assert not result.result.ok and result.result.error.code == "git_timeout", result
            assert children and all(child.poll() is not None for child in children)
            assert result.receipt is not None
            _, trace = store.trace(
                ReceiptScope(
                    request_id=call.request_id,
                    canonical_root=git_repo,
                    project_id=result.receipt.project_id,
                ),
                deadline=time.monotonic() + 5,
            )
            assert trace is not None and trace.events[-1].code == "git_timeout"
            assert any(
                event.git is not None
                and event.git.code == "git_timeout"
                and event.git.process_started
                for event in trace.events
            )
        finally:
            store.close()
