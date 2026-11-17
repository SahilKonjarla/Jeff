"""Actual init identity binding, deleted registration and partial YAML/index publication truth."""

import os
import sqlite3
import time
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from jeff.application.dispatch import EvidenceQuery, ProjectCall, ProjectDispatcher, ReceiptQuery
from jeff.application.projects import ProjectService, Success
from jeff.application.questions import QuestionService
from jeff.application.runtime import RuntimeIdentityData, RuntimeLayout
from jeff.domain.projects import Project
from jeff.infrastructure.project_files import FileProjectStore
from jeff.infrastructure.runtime_state import RuntimeStateStore
from jeff.infrastructure.runtime_storage import SQLiteStorage
from jeff.integrations.git import GitProjectDiscovery, GitRepositoryRead


def test_unregistered_init_claims_root_then_associates_real_identity(
    git_repo: Path, tmp_path: Path
) -> None:
    projects = ProjectService(GitProjectDiscovery(), FileProjectStore())
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
            forged = dispatch.execute(
                ProjectCall(operation="init", directory=git_repo, expected_project_id=uuid4()),
                time.monotonic() + 10,
            )
            assert not forged.result.ok and forged.result.error.code == "project_target_mismatch", (
                forged
            )
            assert (
                not (git_repo / "jeff.local.yaml").exists()
                and not (git_repo / ".gitignore").exists()
            )
            call = ProjectCall(operation="init", directory=git_repo)
            created = dispatch.execute(call, time.monotonic() + 10)
            assert isinstance(created.result, Success) and created.result.data.created, created
            assert created.receipt is not None and created.receipt.state == "succeeded"
            assert str(created.receipt.project_id) == created.result.data.project_id
            config = git_repo / "jeff.local.yaml"
            preserved = (
                config.read_bytes() + b"# keep me\npreferences: {private: SYNTHETIC_PRIVATE_INIT}\n"
            )
            config.write_bytes(preserved)
            ignore = (git_repo / ".gitignore").read_bytes()
            repeated = dispatch.execute(
                ProjectCall(operation="init", directory=git_repo), time.monotonic() + 10
            )
            assert isinstance(repeated.result, Success) and not repeated.result.data.created, (
                repeated
            )
            assert repeated.result.data.project_id == created.result.data.project_id
            assert (
                config.read_bytes() == preserved
                and (git_repo / ".gitignore").read_bytes() == ignore
            )
            duplicate = dispatch.execute(call, time.monotonic() + 10)
            assert not duplicate.result.ok and duplicate.result.error.code == "duplicate_request", (
                duplicate
            )
            assert (
                dispatch.inspect_receipt(
                    ReceiptQuery(directory=git_repo, request_id=call.request_id),
                    time.monotonic() + 10,
                ).receipt
                == created.receipt
            )
        finally:
            storage.close()
    assert b"SYNTHETIC_PRIVATE_INIT" not in layout.database.read_bytes()


def test_deleted_registration_with_indexed_root_never_generates_replacement(
    git_repo: Path, tmp_path: Path
) -> None:
    projects = ProjectService(GitProjectDiscovery(), FileProjectStore())
    registered = projects.execute("init", git_repo)
    assert registered.ok
    project = Project(UUID(registered.data.project_id), git_repo)
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
            storage.index(project)
            config = git_repo / "jeff.local.yaml"
            config.unlink()
            before = layout.database.read_bytes(), (git_repo / ".gitignore").read_bytes()
            dispatch = ProjectDispatcher(
                projects, QuestionService(projects, GitRepositoryRead()), storage, identity
            )
            refused = dispatch.execute(
                ProjectCall(operation="init", directory=git_repo), time.monotonic() + 10
            )
            assert not refused.result.ok and not refused.accepted, refused
            assert not config.exists()
            assert (layout.database.read_bytes(), (git_repo / ".gitignore").read_bytes()) == before
            assert storage.lookup(project) == project
        finally:
            storage.close()


def test_failed_yaml_publication_preserves_ignore_and_root_bound_unknown_receipt(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projects = ProjectService(GitProjectDiscovery(), FileProjectStore())
    identity = RuntimeIdentityData(instance_id=uuid4(), pid=os.getpid())
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    original = os.link

    def link(source: str, destination: str, **kwargs: Any) -> None:
        if destination == "jeff.local.yaml":
            raise OSError("SYNTHETIC_PRIVATE_PUBLICATION")
        original(source, destination, **kwargs)

    with owner:
        owner.store.credential(owner)
        storage = SQLiteStorage(owner, instance_id=identity.instance_id)
        storage.start()
        try:
            assert storage.wait_ready(5).state == "ready"
            dispatch = ProjectDispatcher(
                projects, QuestionService(projects, GitRepositoryRead()), storage, identity
            )
            call = ProjectCall(operation="init", directory=git_repo)
            with monkeypatch.context() as patch:
                patch.setattr(os, "link", link)
                failed = dispatch.execute(call, time.monotonic() + 10)
            assert not failed.result.ok and failed.result.error.code == "storage_io", failed
            assert (
                failed.receipt is not None
                and failed.receipt.state == "unknown"
                and failed.receipt.project_id is None
            )
            assert "SYNTHETIC_PRIVATE_PUBLICATION" not in failed.model_dump_json()
            assert not (git_repo / "jeff.local.yaml").exists()
            ignore = (git_repo / ".gitignore").read_bytes()
            assert b"/jeff.local.yaml" in ignore
            inspected = dispatch.inspect_receipt(
                ReceiptQuery(directory=git_repo, request_id=call.request_id), time.monotonic() + 10
            )
            assert inspected.receipt == failed.receipt and inspected.error is None, inspected
            evidence = dispatch.inspect_evidence(
                EvidenceQuery(operation="trace", directory=git_repo, request_id=call.request_id),
                time.monotonic() + 10,
            )
            assert evidence.trace is not None and evidence.receipt == failed.receipt
            assert evidence.trace.events[-1].kind == "request.unknown"
            assert all(event.project_id is None for event in evidence.trace.events)
            forged = dispatch.inspect_receipt(
                ReceiptQuery(
                    directory=git_repo, request_id=call.request_id, expected_project_id=uuid4()
                ),
                time.monotonic() + 10,
            )
            assert forged.error is not None and forged.error.error.code == "project_target_mismatch"
            duplicate = dispatch.execute(call, time.monotonic() + 10)
            assert not duplicate.result.ok and duplicate.result.error.code == "duplicate_request"
            assert (
                not (git_repo / "jeff.local.yaml").exists()
                and (git_repo / ".gitignore").read_bytes() == ignore
            )
            assert not list(git_repo.glob(".jeff-write-*.tmp"))
            moved = git_repo.with_name("moved unregistered fixture")
            git_repo.rename(moved)
            before = layout.database.read_bytes()
            refused = dispatch.inspect_receipt(
                ReceiptQuery(directory=moved, request_id=call.request_id), time.monotonic() + 10
            )
            assert (
                refused.receipt is None
                and refused.error is not None
                and refused.error.error.code == "receipt_unavailable"
            )
            evidence = dispatch.inspect_evidence(
                EvidenceQuery(operation="trace", directory=moved, request_id=call.request_id),
                time.monotonic() + 10,
            )
            assert evidence.trace is None and evidence.error is not None
            assert evidence.error.error.code == "receipt_unavailable"
            assert (
                layout.database.read_bytes() == before and not (moved / "jeff.local.yaml").exists()
            )
        finally:
            storage.close()


def test_failed_index_after_actual_yaml_write_does_not_erase_or_regenerate_identity(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projects = ProjectService(GitProjectDiscovery(), FileProjectStore())
    identity = RuntimeIdentityData(instance_id=uuid4(), pid=os.getpid())
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    original = sqlite3.connect

    class FailedIndex(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
            if sql.startswith("INSERT INTO projects"):
                raise sqlite3.OperationalError("SYNTHETIC_PRIVATE_INDEX")
            return super().execute(sql, parameters)

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        return original(*args, **kwargs, factory=FailedIndex)

    call = ProjectCall(operation="init", directory=git_repo)
    with owner:
        with monkeypatch.context() as patch:
            patch.setattr(sqlite3, "connect", connect)
            owner.store.credential(owner)
            storage = SQLiteStorage(owner, instance_id=identity.instance_id)
            storage.start()
            try:
                assert storage.wait_ready(5).state == "ready"
                failed = ProjectDispatcher(
                    projects, QuestionService(projects, GitRepositoryRead()), storage, identity
                ).execute(call, time.monotonic() + 10)
                assert (
                    not failed.result.ok and failed.result.error.code == "request_outcome_unknown"
                ), failed
                assert "SYNTHETIC_PRIVATE_INDEX" not in failed.model_dump_json()
                configured = projects.execute("config", git_repo)
                assert configured.ok
                preserved_id = configured.data.project_id
                preserved = (git_repo / "jeff.local.yaml").read_bytes()
            finally:
                storage.close()
        new_identity = RuntimeIdentityData(instance_id=uuid4(), pid=os.getpid())
        reopened = SQLiteStorage(owner, instance_id=new_identity.instance_id)
        reopened.start()
        try:
            assert reopened.wait_ready(5).state == "ready"
            dispatch = ProjectDispatcher(
                projects, QuestionService(projects, GitRepositoryRead()), reopened, new_identity
            )
            found = dispatch.inspect_receipt(
                ReceiptQuery(directory=git_repo, request_id=call.request_id), time.monotonic() + 10
            )
            assert found.receipt is not None and found.receipt.state == "unknown"
            assert str(found.receipt.project_id) == preserved_id
            duplicate = dispatch.execute(call, time.monotonic() + 10)
            assert not duplicate.result.ok and duplicate.result.error.code == "duplicate_request"
            assert (git_repo / "jeff.local.yaml").read_bytes() == preserved
            explicit = dispatch.execute(
                ProjectCall(operation="init", directory=git_repo), time.monotonic() + 10
            )
            assert explicit.result.ok and explicit.result.data.project_id == preserved_id
            assert (git_repo / "jeff.local.yaml").read_bytes() == preserved
        finally:
            reopened.close()
