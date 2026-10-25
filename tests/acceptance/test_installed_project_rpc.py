"""Installed interpreter, actual daemon and authenticated project dispatch (no CLI cutover)."""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from jeff.application.dispatch import ProjectCall, ProjectRequest, ReceiptQuery
from jeff.application.projects import Failure, ProjectService, Success
from jeff.application.questions import QuestionSuccess
from jeff.application.runtime import ControlSuccess, RuntimeLayout
from jeff.domain.repository import escape_metadata
from jeff.infrastructure.project_files import FileProjectStore
from jeff.infrastructure.runtime_ipc import ControlClient, RuntimeProjectClient, encode_message
from jeff.infrastructure.runtime_process import RuntimeController
from jeff.infrastructure.runtime_state import RuntimeStateStore
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
        assert started.data.storage.schema_version == 4
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


@pytest.mark.parametrize("point", ["before_claim", "after_publication"])
def test_installed_init_disconnect_is_not_cancellation_or_retry_authority(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, point: str
) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    paused, release, published = (tmp_path / name for name in ("paused", "release", "published"))
    original = subprocess.Popen
    script = f"""
import os,runpy,subprocess,time
from pathlib import Path
point={point!r}
paused,release,published=map(Path,{[str(paused), str(release), str(published)]!r})
original_popen,original_link=subprocess.Popen,os.link
gated=False
def gate():
    paused.write_text('paused')
    until=time.monotonic()+5
    while not release.exists():
        if time.monotonic()>=until:raise TimeoutError('Owned test gate expired')
        time.sleep(.005)
def launch(command,*args,**kwargs):
    global gated
    if (point=='before_claim' and not gated and command[0]=='/usr/bin/git'
            and '--show-toplevel' in command):
        gated=True
        gate()
    return original_popen(command,*args,**kwargs)
def link(source,destination,**kwargs):
    result=original_link(source,destination,**kwargs)
    if destination=='jeff.local.yaml':
        with published.open('a') as out:out.write('1\\n')
        if point=='after_publication':gate()
    return result
subprocess.Popen,os.link=launch,link
runpy.run_module('jeff.bootstrap.runtime',run_name='__main__')
"""

    def launch(command: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        if "jeff.bootstrap.runtime" in command:
            return original([*command[:3], "-c", script, *command[5:]], **kwargs)
        return original(command, **kwargs)

    control = RuntimeController(layout)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(subprocess, "Popen", launch)
            started = control.start()
        assert isinstance(started, ControlSuccess)
        call = ProjectCall(operation="init", directory=git_repo)
        token = RuntimeStateStore(layout).read_credential()
        with ControlClient(layout, token).connect(time.monotonic() + 5) as connection:
            request = ProjectRequest(
                call=call, expected_instance_id=connection.identity.instance_id, remaining_ms=30000
            )
            connection.connection.sendall(encode_message(request.model_dump(mode="json"), 65536))
            until = time.monotonic() + 4
            while not paused.exists():
                assert time.monotonic() < until
                time.sleep(0.005)
            assert (git_repo / "jeff.local.yaml").exists() == (point == "after_publication")
            began = time.monotonic()
            assert isinstance(control.status(), ControlSuccess) and time.monotonic() - began < 1
        release.write_text("released after caller disconnect")
        client = RuntimeProjectClient(layout)
        until = time.monotonic() + 5
        while True:
            found = client.inspect_receipt(
                ReceiptQuery(directory=git_repo, request_id=call.request_id)
            )
            if found.receipt is not None and found.receipt.state == "succeeded":
                break
            assert time.monotonic() < until, found
            time.sleep(0.01)
        assert found.receipt.project_id is not None and published.read_text() == "1\n"
        before = (git_repo / "jeff.local.yaml").read_bytes()
        duplicate = client.execute(call)
        assert not duplicate.result.ok and duplicate.result.error.code == "duplicate_request"
        assert (
            git_repo / "jeff.local.yaml"
        ).read_bytes() == before and published.read_text() == "1\n"
    finally:
        release.write_text("released for owned teardown")
        assert isinstance(control.stop(), ControlSuccess)
        layout.credential.unlink(missing_ok=True)


def test_owned_retained_v2_and_v3_refuse_without_replacement(tmp_path: Path) -> None:
    old = os.environ.get("JEFF_TEST_V2_PYTHON")
    if old is None:
        pytest.skip("Owned verified v2 interpreter required; mandatory in guarded acceptance")
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    env = {
        "PATH": os.defpath,
        "HOME": str(tmp_path),
        "JEFF_DATA_DIR": str(layout.data_dir),
        "JEFF_RUNTIME_DIR": str(layout.runtime_dir),
        "PYTHONDONTWRITEBYTECODE": "1",
    }

    def old_call(operation: str) -> dict[str, object]:
        code = """
import sys
from jeff.application.runtime import PROTOCOL_REVISION
from jeff.bootstrap.runtime import create_runtime_controller
assert PROTOCOL_REVISION==2
print(getattr(create_runtime_controller(),sys.argv[1])().model_dump_json())
"""
        result = subprocess.run(
            [old, "-I", "-B", "-c", code, operation],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=45,
        )
        assert result.returncode == 0, "Owned old helper failed; no raw stderr forwarded"
        data: dict[str, object] = json.loads(result.stdout)
        return data

    try:
        assert old_call("start")["ok"] is True
        before = layout.credential.stat().st_ino, layout.database.read_bytes()
        refused = RuntimeController(layout).start()
        assert not refused.ok and refused.error.code == "runtime_protocol_incompatible"
        assert before == (layout.credential.stat().st_ino, layout.database.read_bytes())
        assert old_call("status")["ok"] is True
    finally:
        assert old_call("stop")["ok"] is True
    control = RuntimeController(layout)
    try:
        assert isinstance(control.start(), ControlSuccess)
        result = old_call("status")
        assert result["ok"] is False
        assert (
            isinstance(result["error"], dict)
            and result["error"]["code"] == "runtime_protocol_incompatible"
        )
        assert isinstance(control.status(), ControlSuccess)
    finally:
        assert isinstance(control.stop(), ControlSuccess)
        layout.credential.unlink(missing_ok=True)
