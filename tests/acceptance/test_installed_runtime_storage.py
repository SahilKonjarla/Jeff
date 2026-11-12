"""Installed v2 storage persistence and hard old/new compatibility in owned fixtures."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from jeff.application.projects import Failure, ProjectService, Success
from jeff.application.runtime import ControlSuccess, RuntimeLayout
from jeff.application.storage import RegistryService, RegistrySuccess
from jeff.infrastructure.project_files import FileProjectStore
from jeff.infrastructure.runtime_process import RuntimeController
from jeff.infrastructure.runtime_state import RuntimeStateStore
from jeff.infrastructure.runtime_storage import SQLiteStorage
from jeff.integrations.git import GitProjectDiscovery

pytestmark = pytest.mark.usefixtures("owned_runtime_launches")


def test_installed_storage_registry_survives_actual_daemon_restart(
    git_repo: Path, tmp_path: Path
) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    projects = ProjectService(GitProjectDiscovery(), FileProjectStore())
    registration = projects.execute("init", git_repo)
    assert isinstance(registration, Success)
    controller = RuntimeController(layout)
    try:
        first = controller.start()
        assert isinstance(first, ControlSuccess) and first.data.storage.schema_version == 4
        assert first.data.application_ready is True
        assert isinstance(controller.stop(), ControlSuccess)
        for operation in ("index", "lookup"):
            owner = RuntimeStateStore(layout).acquire(create=False)
            assert owner is not None
            with owner:
                storage = SQLiteStorage(owner)
                storage.start()
                try:
                    assert storage.wait_ready(5).state == "ready"
                    registry = RegistryService(projects, storage)
                    result = registry.execute(
                        "index" if operation == "index" else "lookup", git_repo
                    )
                    assert isinstance(result, RegistrySuccess) and result.data.indexed
                    assert str(result.data.project_id) == registration.data.project_id
                    assert result.data.created == (operation == "index")
                finally:
                    storage.close()
        again = controller.start()
        assert isinstance(again, ControlSuccess) and again.data.storage.state == "ready"
        assert again.data.identity != first.data.identity
        assert again.data.storage.settings_source == "existing_file"
    finally:
        assert isinstance(controller.stop(), ControlSuccess)
        layout.credential.unlink(missing_ok=True)


def test_retained_v1_and_current_v2_refuse_without_downgrade_or_replacement(tmp_path: Path) -> None:
    old = os.environ.get("JEFF_TEST_V1_PYTHON")
    if old is None:
        pytest.skip("Owned retained-v1 interpreter required; mandatory in guarded acceptance")
    root = tmp_path
    (root / "home").mkdir(mode=0o700)
    layout = RuntimeLayout(data_dir=root / "d", runtime_dir=root / "r", uid=os.geteuid())
    env = {
        "PATH": os.defpath,
        "HOME": str(root / "home"),
        "JEFF_DATA_DIR": str(layout.data_dir),
        "JEFF_RUNTIME_DIR": str(layout.runtime_dir),
        "PYTHONDONTWRITEBYTECODE": "1",
    }

    def old_call(operation: str) -> dict[str, object]:
        code = """
import json,sys
from jeff.application.runtime import PROTOCOL_REVISION
from jeff.bootstrap.runtime import create_runtime_controller
assert PROTOCOL_REVISION==1
result=getattr(create_runtime_controller(),sys.argv[1])()
print(result.model_dump_json())
"""
        result = subprocess.run(
            [old, "-I", "-B", "-c", code, operation],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=45,
        )
        assert result.returncode == 0, "Owned v1 helper failed without exposing stderr"
        return json.loads(result.stdout)  # type: ignore[no-any-return]

    try:
        assert old_call("start")["ok"] is True
        before = (layout.credential.stat().st_ino, layout.credential.stat().st_mtime_ns)
        for operation in ("status", "start"):
            denied = getattr(RuntimeController(layout), operation)()
            assert (
                isinstance(denied, Failure) and denied.error.code == "runtime_handshake_unconfirmed"
            )
        assert not layout.database.exists() and not layout.settings.exists()
        assert before == (layout.credential.stat().st_ino, layout.credential.stat().st_mtime_ns)
        assert old_call("status")["ok"] is True
    finally:
        assert old_call("stop")["ok"] is True
    controller = RuntimeController(layout)
    try:
        assert isinstance(controller.start(), ControlSuccess)
        rejected = old_call("status")
        assert rejected["ok"] is False
        assert rejected["error"]["code"] == "runtime_protocol_incompatible"  # type: ignore[index]
        assert isinstance(controller.status(), ControlSuccess)
    finally:
        assert isinstance(controller.stop(), ControlSuccess)
        layout.credential.unlink(missing_ok=True)
