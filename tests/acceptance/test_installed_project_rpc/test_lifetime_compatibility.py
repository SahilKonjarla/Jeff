"""Actual disconnected-caller and retained-wire refusal without replacement/retry authority."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from jeff.application.dispatch import ProjectCall, ProjectRequest, ReceiptQuery
from jeff.application.runtime import ControlSuccess, RuntimeLayout
from jeff.infrastructure.runtime_ipc import ControlClient, RuntimeProjectClient, encode_message
from jeff.infrastructure.runtime_process import RuntimeController
from jeff.infrastructure.runtime_state import RuntimeStateStore

pytestmark = pytest.mark.usefixtures("owned_runtime_launches")


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
import jeff.bootstrap.runtime as runtime
original_runner,original_link=runtime.OwnedGitRunner,os.link
gated=False
def gate():
    paused.write_text('paused')
    until=time.monotonic()+5
    while not release.exists():
        if time.monotonic()>=until:raise TimeoutError('Owned test gate expired')
        time.sleep(.005)
class GatedRunner(original_runner):
    def __call__(self,executable,directory,*args,**kwargs):
        global gated
        if point=='before_claim' and not gated and args[:2]==('rev-parse','--show-toplevel'):
            gated=True
            gate()
        return super().__call__(executable,directory,*args,**kwargs)
def link(source,destination,**kwargs):
    result=original_link(source,destination,**kwargs)
    if destination=='jeff.local.yaml':
        with published.open('a') as out:out.write('1\\n')
        if point=='after_publication':gate()
    return result
runtime.OwnedGitRunner,os.link=GatedRunner,link
raise SystemExit(runtime.main())
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
        "JEFF_TEST_SQLITE_FIXTURE": "1",
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
