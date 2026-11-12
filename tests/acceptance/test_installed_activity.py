"""Installed trace/replay regression; public B diagnostics are exercised separately."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from jeff.application.dispatch import DebugQuery, EvidenceClient, EvidenceQuery, ProjectCall
from jeff.application.questions import QuestionSuccess
from jeff.application.runtime import ControlSuccess, RuntimeLayout
from jeff.infrastructure.runtime_ipc import RuntimeProjectClient
from jeff.infrastructure.runtime_process import RuntimeController

pytestmark = pytest.mark.usefixtures("owned_runtime_launches")


def test_installed_real_git_trace_scoped_replay_restart_and_no_raw_archive(
    git_repo: Path, tmp_path: Path
) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    control = RuntimeController(layout)
    client = RuntimeProjectClient(layout)
    evidence: EvidenceClient = client
    try:
        started = control.start()
        assert isinstance(started, ControlSuccess) and started.data.storage.schema_version == 4
        initialized = client.execute(ProjectCall(operation="init", directory=git_repo))
        assert initialized.result.ok
        call = ProjectCall(operation="ask", directory=git_repo, question="what branch am I on?")
        branch = client.execute(call)
        assert isinstance(branch.result, QuestionSuccess), branch
        query = EvidenceQuery(operation="trace", directory=git_repo, request_id=call.request_id)
        before = layout.database.read_bytes(), layout.database.stat().st_mtime_ns
        trace = evidence.inspect_evidence(query)
        assert trace.error is None and trace.trace is not None and trace.receipt == branch.receipt
        assert not trace.trace.legacy_metadata_only
        observations = [event.git for event in trace.trace.events if event.git is not None]
        assert [event.category for event in observations] == ["filters.inspect", "status.branch"]
        assert (
            observations[-1].output_bytes > 0
            and observations[-1].code == "ok"
            and observations[-1].process_started
        )
        assert trace.trace.events[-1].kind == "request.succeeded"
        page = client.inspect_evidence(
            EvidenceQuery(operation="replay", directory=git_repo, limit=2)
        )
        assert page.page is not None and page.page.has_more and len(page.page.events) == 2
        other = client.inspect_evidence(
            query.model_copy(update={"query_id": uuid4(), "expected_project_id": uuid4()})
        )
        assert other.error is not None and other.receipt is None and other.trace is None
        invalid = client.inspect_evidence(
            EvidenceQuery(operation="replay", directory=git_repo, expected_epoch=uuid4())
        )
        assert invalid.error is not None and invalid.error.error.code == "activity_invalid_cursor"
        assert (layout.database.read_bytes(), layout.database.stat().st_mtime_ns) == before
        assert isinstance(control.stop(), ControlSuccess)
        assert isinstance(control.start(), ControlSuccess)
        again = client.inspect_evidence(query)
        assert again.trace is not None and again.trace.events == trace.trace.events
        assert again.trace.database_epoch == trace.trace.database_epoch
        duplicate = client.execute(call)
        assert not duplicate.result.ok and duplicate.result.error.code == "duplicate_request"
        assert b"what branch am I on" not in layout.database.read_bytes()
    finally:
        assert isinstance(control.stop(), ControlSuccess)
        layout.credential.unlink(missing_ok=True)


def test_owned_v3_v5_refuse_without_rekey_then_preserve_legacy_metadata(
    git_repo: Path, tmp_path: Path
) -> None:
    old = os.environ.get("JEFF_TEST_V3_PYTHON")
    if old is None:
        pytest.skip("Requires explicitly verified owned v3 wheel")
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    env = {
        "PATH": os.defpath,
        "HOME": str(tmp_path),
        "JEFF_DATA_DIR": str(layout.data_dir),
        "JEFF_RUNTIME_DIR": str(layout.runtime_dir),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    request = uuid4()

    def legacy(operation: str) -> dict[str, Any]:
        code = """
import sys
from pathlib import Path
from uuid import UUID
from jeff.application.runtime import PROTOCOL_REVISION
from jeff.application.dispatch import ProjectCall
from jeff.bootstrap.runtime import create_runtime_controller,create_project_client
assert PROTOCOL_REVISION==3
if sys.argv[1]=='init':
    result=create_project_client().execute(ProjectCall(operation='init',directory=Path(sys.argv[2]),request_id=UUID(sys.argv[3])))
else:result=getattr(create_runtime_controller(),sys.argv[1])()
print(result.model_dump_json())
"""
        result = subprocess.run(
            [old, "-I", "-B", "-c", code, operation, str(git_repo), str(request)],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=45,
        )
        assert result.returncode == 0, "Owned legacy helper failed; raw output suppressed"
        data: dict[str, Any] = json.loads(result.stdout)
        return data

    try:
        assert legacy("start")["ok"]
        initialized = legacy("init")
        assert initialized["result"]["ok"]
        before = layout.credential.stat().st_ino, layout.database.read_bytes()
        refused = RuntimeController(layout).start()
        assert not refused.ok and refused.error.code == "runtime_protocol_incompatible"
        assert before == (layout.credential.stat().st_ino, layout.database.read_bytes())
    finally:
        assert legacy("stop")["ok"]
    control = RuntimeController(layout)
    try:
        assert isinstance(control.start(), ControlSuccess)
        traced = RuntimeProjectClient(layout).inspect_evidence(
            EvidenceQuery(operation="trace", directory=git_repo, request_id=request)
        )
        assert (
            traced.trace is not None
            and traced.trace.legacy_metadata_only
            and not traced.trace.events
        )
        assert (
            traced.receipt is not None
            and str(traced.receipt.project_id) == initialized["result"]["data"]["project_id"]
        )
        debug = RuntimeProjectClient(layout).inspect_evidence(
            DebugQuery(directory=git_repo, request_id=request)
        )
        assert (
            debug.error is None and debug.trace == traced.trace and debug.receipt == traced.receipt
        )
        rejected = legacy("status")
        assert not rejected["ok"] and rejected["error"]["code"] == "runtime_protocol_incompatible"
        assert isinstance(control.status(), ControlSuccess)
    finally:
        assert isinstance(control.stop(), ControlSuccess)
        layout.credential.unlink(missing_ok=True)
