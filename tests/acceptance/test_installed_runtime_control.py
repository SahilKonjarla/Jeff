"""Installed-interpreter control outside Git; also run in a newly built noneditable venv."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

import jeff
from jeff.application.runtime import ControlSuccess
from jeff.bootstrap.runtime import create_runtime_controller


def test_installed_control_uses_same_installation_and_leaves_public_cli_unchanged(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    root = tmp_path_factory.mktemp("installed").resolve()
    for name in ("home", "tmp"):
        (root / name).mkdir(mode=0o700)
    env = {
        "PATH": os.defpath,
        "HOME": str(root / "home"),
        "TMPDIR": str(root / "tmp"),
        "JEFF_DATA_DIR": str(root / "d"),
        "JEFF_RUNTIME_DIR": str(root / "r"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "NO_COLOR": "1",
    }
    code = """
import json,subprocess,sys
import jeff
from jeff.application.runtime import ControlSuccess
from jeff.bootstrap.runtime import create_runtime_controller
children=[]
original=subprocess.Popen
def tracked(*args,**kwargs):
    child=original(*args,**kwargs)
    children.append(child)
    return child
subprocess.Popen=tracked
control=create_runtime_controller()
try:
    started=control.start()
    assert isinstance(started,ControlSuccess),started
    assert started.data.identity is not None
    print(json.dumps({"package_file":jeff.__file__,"interpreter":sys.executable,
                      "control_ready":started.data.control_ready,
                      "application_ready":started.data.application_ready,
                      "pid_matches_owned_child":started.data.identity.pid==children[0].pid}))
finally:
    result=control.stop()
    forced=False
    for child in children:
        if child.poll() is None:
            forced=True
            child.kill()
            child.wait(timeout=5)
    assert not forced,"Installed scenario required forced cleanup"
    assert isinstance(result,ControlSuccess),result
"""
    try:
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", code],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        receipt = json.loads(result.stdout)
        assert jeff.__file__ is not None
        assert Path(receipt["package_file"]).resolve() == Path(jeff.__file__).resolve()
        assert receipt["control_ready"] is True and receipt["application_ready"] is False
        assert receipt["pid_matches_owned_child"] is True
        assert not (root / "r/daemon.sock").exists()
        assert not (root / "d/state.sqlite3").exists()
        assert not (root / "d/settings.yaml").exists()
        command = subprocess.run(
            [sys.executable, "-I", "-B", "-m", "jeff", "start"],
            cwd=root,
            env=env,
            capture_output=True,
            timeout=10,
        )
        assert command.returncode == 2
    finally:
        (root / "d/ipc.key").unlink(missing_ok=True)
        (root / "r/daemon.sock").unlink(missing_ok=True)


def test_explicit_overrides_do_not_probe_host_home_or_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JEFF_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("JEFF_RUNTIME_DIR", str(tmp_path / "r"))

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("Bootstrap selection must not probe unused host locations")

    with monkeypatch.context() as patch:
        patch.setattr(tempfile, "gettempdir", forbidden)
        patch.setattr(Path, "home", forbidden)
        create_runtime_controller()
    assert not list(tmp_path.iterdir())


def test_daemon_survives_launcher_exit_and_another_caller_confirms_shutdown(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path_factory.mktemp("detach").resolve()
    (root / "home").mkdir(mode=0o700)
    env = {
        "PATH": os.defpath,
        "HOME": str(root / "home"),
        "JEFF_DATA_DIR": str(root / "d"),
        "JEFF_RUNTIME_DIR": str(root / "r"),
    }
    monkeypatch.setenv("JEFF_DATA_DIR", env["JEFF_DATA_DIR"])
    monkeypatch.setenv("JEFF_RUNTIME_DIR", env["JEFF_RUNTIME_DIR"])
    code = """
from jeff.bootstrap.runtime import create_runtime_controller
from jeff.application.runtime import ControlSuccess
controller=create_runtime_controller()
result=controller.start()
assert isinstance(result,ControlSuccess),result
print(result.data.identity.model_dump_json())
"""
    controller = create_runtime_controller()
    try:
        launched = subprocess.run(
            [sys.executable, "-I", "-B", "-c", code],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert launched.returncode == 0, launched.stderr
        expected = json.loads(launched.stdout)
        current = controller.status()
        assert isinstance(current, ControlSuccess) and current.data.identity is not None
        assert current.data.identity.model_dump(mode="json") == expected
        stopped = controller.stop()
        assert isinstance(stopped, ControlSuccess), stopped
        assert stopped.data.state.value == "stopped"
    finally:
        cleanup = controller.stop()
        assert isinstance(cleanup, ControlSuccess), "Detached owned fixture cleanup unconfirmed"
        (root / "d/ipc.key").unlink(missing_ok=True)


@pytest.mark.parametrize("descriptor", [-1, 999999, 2**40])
def test_internal_child_refuses_invalid_descriptor_before_creating_installation(
    tmp_path: Path,
    descriptor: int,
) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-m",
            "jeff.bootstrap.runtime",
            "--lock-fd",
            str(descriptor),
            "--instance-id",
            "00000000-0000-4000-8000-000000000001",
        ],
        cwd=tmp_path,
        env={
            "PATH": os.defpath,
            "JEFF_DATA_DIR": str(tmp_path / "d"),
            "JEFF_RUNTIME_DIR": str(tmp_path / "r"),
        },
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 1
    assert not list(tmp_path.iterdir())
