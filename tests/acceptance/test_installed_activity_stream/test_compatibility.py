"""Actual retained B/C versus current wire refusal without rekey, replay or downgrade."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from jeff.application.dispatch import ProjectCall, SubscriptionQuery
from jeff.application.runtime import ControlFault, ControlSuccess, RuntimeLayout
from jeff.infrastructure.runtime_ipc import RuntimeProjectClient
from jeff.infrastructure.runtime_process import RuntimeController

pytestmark = pytest.mark.usefixtures("owned_runtime_launches")


@pytest.mark.parametrize("legacy_env", ["JEFF_TEST_B_PYTHON", "JEFF_TEST_C_PYTHON"])
def test_wire_five_and_six_refuse_both_directions_without_rekey_or_downgrade(
    git_repo: Path,
    tmp_path: Path,
    legacy_env: str,
) -> None:
    old = os.environ.get(legacy_env)
    assert old is not None, "Requires explicitly hash-verified retained B/C wheel"
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    env = {
        "PATH": os.defpath,
        "HOME": str(tmp_path),
        "JEFF_DATA_DIR": str(layout.data_dir),
        "JEFF_RUNTIME_DIR": str(layout.runtime_dir),
        "PYTHONDONTWRITEBYTECODE": "1",
    }

    def legacy(operation: str) -> dict[str, Any]:
        code = """
import sys
from pathlib import Path
from jeff.bootstrap.runtime import create_runtime_controller,create_project_client
from jeff.application.dispatch import ProjectCall
from jeff.application.runtime import PROTOCOL_REVISION,ControlFault
assert PROTOCOL_REVISION==5
try:
    if sys.argv[1]=='init':
        r=create_project_client().execute(ProjectCall(operation='init',directory=Path(sys.argv[2])))
    else:r=getattr(create_runtime_controller(),sys.argv[1])()
except ControlFault as error:
    import json
    print(json.dumps({'raised':error.code}))
else:print(r.model_dump_json())
"""
        child = subprocess.run(
            [old, "-I", "-B", "-c", code, operation, str(git_repo)],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            timeout=15,
        )
        assert child.returncode == 0, "Owned legacy helper failed; raw output suppressed"
        result: dict[str, Any] = json.loads(child.stdout)
        return result

    control, client = RuntimeController(layout), RuntimeProjectClient(layout)
    try:
        assert legacy("start")["ok"]
        initial = legacy("init")
        assert initial["result"]["ok"]
        registration = (git_repo / "jeff.local.yaml").read_bytes()
        before = layout.credential.stat().st_ino, layout.database.read_bytes()
        refused = control.start()
        assert not refused.ok and refused.error.code == "runtime_protocol_incompatible"
        with pytest.raises(ControlFault) as missing:
            client.subscribe(SubscriptionQuery(directory=git_repo))
        assert missing.value.code == "runtime_protocol_incompatible"
        assert before == (layout.credential.stat().st_ino, layout.database.read_bytes())
    finally:
        assert legacy("stop")["ok"]  # matching old caller, not guessed owner replacement
    try:
        started = control.start()
        assert isinstance(started, ControlSuccess) and started.data.storage.schema_version == 4
        configured = client.execute(ProjectCall(operation="config", directory=git_repo))
        assert configured.result.ok
        assert configured.result.data.project_id == initial["result"]["data"]["project_id"]
        before = layout.credential.stat().st_ino, layout.database.read_bytes()
        for operation in ("start", "status"):
            refused_old = legacy(operation)
            assert not refused_old["ok"]
            assert refused_old["error"]["code"] == "runtime_protocol_incompatible"
        refused_init = legacy("init")
        # Frozen B/C project clients raise their fixed preflight fault rather than an envelope.
        assert refused_init == {"raised": "runtime_protocol_incompatible"}
        assert before == (layout.credential.stat().st_ino, layout.database.read_bytes())
        assert (git_repo / "jeff.local.yaml").read_bytes() == registration
    finally:
        assert isinstance(control.stop(), ControlSuccess)
    # An offline old reader must also refuse schema4, not adopt/downgrade/reset it.
    code = """
import os
from pathlib import Path
from jeff.application.runtime import RuntimeLayout
from jeff.infrastructure.runtime_state import RuntimeStateStore
from jeff.infrastructure.runtime_storage import SQLiteStorage
layout=RuntimeLayout(data_dir=Path(os.environ['JEFF_DATA_DIR']),
                     runtime_dir=Path(os.environ['JEFF_RUNTIME_DIR']),uid=os.geteuid())
owner=RuntimeStateStore(layout).acquire(create=False)
assert owner is not None
with owner:
    store=SQLiteStorage(owner);store.start()
    try:print(store.wait_ready(5).model_dump_json())
    finally:store.close()
"""
    try:
        before_offline = layout.database.read_bytes()
        result = subprocess.run(
            [old, "-I", "-B", "-c", code], env=env, capture_output=True, timeout=10
        )
        assert result.returncode == 0, "Owned offline legacy reader failed; raw output suppressed"
        state = json.loads(result.stdout)
        assert state["state"] == "failed" and state["error"] == "storage_schema_unsupported"
        assert layout.database.read_bytes() == before_offline
    finally:
        layout.credential.unlink(missing_ok=True)
