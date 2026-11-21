"""Actual project RPC receipts, stable init/config/changes identity and refusal contracts."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from jeff.application.dispatch import ProjectCall, ReceiptQuery
from jeff.application.projects import Failure, ProjectService, Success
from jeff.application.questions import QuestionSuccess
from jeff.application.runtime import ControlSuccess, RuntimeLayout
from jeff.domain.repository import escape_metadata
from jeff.infrastructure.project_files import FileProjectStore
from jeff.infrastructure.runtime_ipc import RuntimeProjectClient
from jeff.infrastructure.runtime_process import RuntimeController
from jeff.integrations.git import GitProjectDiscovery

pytestmark = pytest.mark.usefixtures("owned_runtime_launches")


def test_installed_branch_rpc_receipt_and_target_binding(git_repo: Path, tmp_path: Path) -> None:
    projects = ProjectService(GitProjectDiscovery(), FileProjectStore())
    registered = projects.execute("init", git_repo)
    assert registered.ok
    config = git_repo / "jeff.local.yaml"
    config.write_bytes(config.read_bytes() + b"private: SYNTHETIC_PRIVATE_RPC\n")
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    controller = RuntimeController(layout)
    try:
        started = controller.start()
        assert isinstance(started, ControlSuccess), started
        assert started.data.application_ready is True
        assert started.data.storage.schema_version == 12
        assert started.data.available_operations == (
            "ask.branch",
            "ask.changes",
            "init",
            "config",
            "receipt.inspect",
            "activity.trace",
            "activity.replay",
            "debug.last",
            "history.list",
            "activity.subscribe",
            "execution.review",
        )
        client = RuntimeProjectClient(layout)
        call = ProjectCall(
            operation="ask",
            directory=git_repo,
            expected_project_id=UUID(registered.data.project_id),
            question="what branch am I on?",
        )
        result = client.execute(call)
        assert isinstance(result.result, QuestionSuccess), result
        assert result.result.data.kind == "branch" and result.result.data.branch.name == "main"
        assert result.identity == started.data.identity and result.identity.pid != os.getpid()
        assert result.receipt is not None and result.receipt.state == "succeeded"
        again = client.execute(call)
        assert isinstance(again.result, Failure) and again.result.error.code == "duplicate_request"
        queried = client.inspect_receipt(
            ReceiptQuery(directory=git_repo, request_id=call.request_id)
        )
        assert queried.receipt == result.receipt
        forged = client.execute(
            call.model_copy(update={"request_id": uuid4(), "expected_project_id": uuid4()})
        )
        assert (
            isinstance(forged.result, Failure)
            and forged.result.error.code == "project_target_mismatch"
        )
    finally:
        assert isinstance(controller.stop(), ControlSuccess)
        layout.credential.unlink(missing_ok=True)
    assert b"SYNTHETIC_PRIVATE_RPC" not in layout.database.read_bytes()
    assert b"what branch am I on" not in layout.database.read_bytes()


def test_installed_init_config_changes_and_concurrent_duplicate_identity(
    git_repo: Path, tmp_path: Path, git_run: Callable[..., subprocess.CompletedProcess[bytes]]
) -> None:
    (git_repo / "before.txt").write_text("synthetic rename source")
    git_run("add", "--", "before.txt")
    git_run("commit", "-qm", "Owned RPC fixture")
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    control = RuntimeController(layout)
    try:
        started = control.start()
        assert isinstance(started, ControlSuccess) and started.data.application_ready
        client = RuntimeProjectClient(layout)
        call = ProjectCall(operation="init", directory=git_repo)
        with ThreadPoolExecutor(max_workers=4) as callers:
            replies = list(callers.map(lambda _: client.execute(call), range(4)))
        successes = [reply for reply in replies if reply.result.ok]
        assert len(successes) == 1, replies
        initial = successes[0]
        assert isinstance(initial.result, Success) and initial.result.data.created
        assert initial.identity == started.data.identity and initial.identity.pid != os.getpid()
        stable_duplicate = client.execute(call)
        assert (
            not stable_duplicate.result.ok
            and stable_duplicate.result.error.code == "duplicate_request"
        )
        assert (
            initial.receipt is not None
            and str(initial.receipt.project_id) == initial.result.data.project_id
        )
        config = git_repo / "jeff.local.yaml"
        config.write_bytes(config.read_bytes() + b"private: SYNTHETIC_PRIVATE_INSTALLED_B\n")
        before_yaml, before_ignore = config.read_bytes(), (git_repo / ".gitignore").read_bytes()
        before_db = layout.database.read_bytes(), layout.database.stat().st_mtime_ns
        configured = client.execute(ProjectCall(operation="config", directory=git_repo))
        assert (
            isinstance(configured.result, Success)
            and configured.result.data.project_id == initial.result.data.project_id
        )
        assert not configured.accepted and configured.receipt is None
        assert (layout.database.read_bytes(), layout.database.stat().st_mtime_ns) == before_db
        (git_repo / "before.txt").rename(git_repo / "after.txt")
        git_run("add", "--", "before.txt", "after.txt")
        unusual = "line\n\x1b[31m\u202e.txt"
        (git_repo / unusual).write_text("synthetic")
        note = git_repo / "visible.txt"
        note.write_text("SYNTHETIC_PRIVATE_FILE_BODY")
        secret = git_repo / "secrets"
        secret.mkdir()
        (secret / "private.txt").write_text("SYNTHETIC_PROTECTED_BODY")
        changes = client.execute(
            ProjectCall(operation="ask", directory=git_repo, question="what changed?")
        )
        assert (
            isinstance(changes.result, QuestionSuccess) and changes.result.data.kind == "changes"
        ), changes
        paths = {change.path for change in changes.result.data.changes}
        assert escape_metadata(unusual) in paths
        renamed = next(
            change for change in changes.result.data.changes if change.path == "after.txt"
        )
        assert renamed.original_path == "before.txt" and renamed.index_status == "R"
        assert "visible.txt" in paths and not any(
            "private.txt" in path or "jeff.local.yaml" in path for path in paths
        )
        assert (
            changes.receipt is not None
            and changes.receipt.operation == "ask.changes"
            and changes.receipt.state == "succeeded"
        )
        assert isinstance(control.stop(), ControlSuccess)
        assert isinstance(control.start(), ControlSuccess)
        again = client.execute(ProjectCall(operation="init", directory=git_repo))
        assert isinstance(again.result, Success) and not again.result.data.created
        assert again.result.data.project_id == initial.result.data.project_id
        assert (
            config.read_bytes() == before_yaml
            and (git_repo / ".gitignore").read_bytes() == before_ignore
        )
        assert (
            client.inspect_receipt(
                ReceiptQuery(directory=git_repo, request_id=call.request_id)
            ).receipt
            == initial.receipt
        )
    finally:
        assert isinstance(control.stop(), ControlSuccess)
        layout.credential.unlink(missing_ok=True)
    payload = layout.database.read_bytes()
    assert b"SYNTHETIC_PRIVATE_INSTALLED_B" not in payload
    assert (
        b"SYNTHETIC_PRIVATE_FILE_BODY" not in payload and b"SYNTHETIC_PROTECTED_BODY" not in payload
    )


@pytest.mark.parametrize(
    "kind,code",
    [("malformed", "config_invalid"), ("moved", "project_moved"), ("linked", "unsafe_path")],
)
def test_installed_bad_registration_never_repairs_or_rebinds(
    git_repo: Path, tmp_path: Path, kind: str, code: str
) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    control = RuntimeController(layout)
    try:
        assert isinstance(control.start(), ControlSuccess)
        client = RuntimeProjectClient(layout)
        initialized = client.execute(ProjectCall(operation="init", directory=git_repo))
        assert initialized.result.ok
        config = git_repo / "jeff.local.yaml"
        original = config.read_bytes()
        target = git_repo
        if kind == "malformed":
            config.write_bytes(b"SYNTHETIC_PRIVATE: [broken")
        elif kind == "moved":
            target = git_repo.with_name("moved fixture")
            git_repo.rename(target)
            config = target / "jeff.local.yaml"
        else:
            backup = tmp_path / "synthetic-backup"
            backup.write_bytes(original)
            backup.chmod(0o600)
            config.unlink()
            config.symlink_to(backup)
        before = layout.database.read_bytes(), layout.database.stat().st_mtime_ns
        for operation in ("init", "config", "ask"):
            result = client.execute(
                ProjectCall(
                    operation=operation,
                    directory=target,
                    question="what changed?" if operation == "ask" else None,
                )
            )
            assert not result.result.ok and result.result.error.code == code, result
            assert result.accepted is False and result.receipt is None
        assert (layout.database.read_bytes(), layout.database.stat().st_mtime_ns) == before
        if kind == "linked":
            assert config.is_symlink() and backup.read_bytes() == original
        elif kind == "moved":
            assert config.read_bytes() == original
        else:
            assert config.read_bytes() == b"SYNTHETIC_PRIVATE: [broken"
    finally:
        assert isinstance(control.stop(), ControlSuccess)
        layout.credential.unlink(missing_ok=True)


def test_installed_changes_refuses_filters_without_executing_them(
    git_repo: Path, git_run: Callable[..., subprocess.CompletedProcess[bytes]], tmp_path: Path
) -> None:
    marker = tmp_path / "filter-must-not-run"
    git_run("config", "filter.fixture.clean", f"touch {marker}")
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    control = RuntimeController(layout)
    try:
        assert isinstance(control.start(), ControlSuccess)
        client = RuntimeProjectClient(layout)
        assert client.execute(ProjectCall(operation="init", directory=git_repo)).result.ok
        refused = client.execute(
            ProjectCall(operation="ask", directory=git_repo, question="what changed?")
        )
        assert not refused.result.ok and refused.result.error.code == "git_filters_unsupported", (
            refused
        )
        assert refused.receipt is not None and refused.receipt.state == "failed"
        assert not marker.exists()
        branch = client.execute(
            ProjectCall(operation="ask", directory=git_repo, question="what branch am I on?")
        )
        assert branch.result.ok and not marker.exists()
    finally:
        assert isinstance(control.stop(), ControlSuccess)
        layout.credential.unlink(missing_ok=True)
