"""Public installed daemon lifecycle, stopped/no-fallback policy and unchanged project bodies."""

import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import UUID

from jeff.application.dispatch import ProjectStatusSuccess, ReceiptQuery
from jeff.application.projects import Failure, Success
from jeff.application.questions import QuestionSuccess
from jeff.application.runtime import ControlSuccess
from jeff.bootstrap.runtime import create_project_client, create_runtime_controller


def invoke(directory: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", "-B", "-m", "jeff", *args],
        cwd=directory,
        env={"PATH": os.defpath, "NO_COLOR": "1", **env},
        capture_output=True,
        text=True,
        timeout=45,
    )


def test_stopped_commands_never_autostart_fallback_or_write(
    git_repo: Path, cli_environment: dict[str, str]
) -> None:
    env = cli_environment
    for args in (("init",), ("config",), ("ask", "what changed?")):
        result = invoke(git_repo, env, *args, "--json")
        assert result.returncode == 1, result.stderr
        assert Failure.model_validate_json(result.stdout).error.code == "daemon_not_running"
        assert not result.stderr
    assert not (git_repo / "jeff.local.yaml").exists() and not (git_repo / ".gitignore").exists()
    assert not Path(env["JEFF_DATA_DIR"]).exists() and not Path(env["JEFF_RUNTIME_DIR"]).exists()
    invalid = {**env, "JEFF_DATA_DIR": "relative-invalid-private"}
    for args in (("--help",), ("start", "--help"), ("ask", "--help")):
        assert invoke(git_repo, invalid, *args).returncode == 0
    status = invoke(git_repo, invalid, "status", "--json")
    assert Failure.model_validate_json(status.stdout).error.code == "project_not_initialized"
    assert not Path(env["JEFF_DATA_DIR"]).exists()


def test_public_lifecycle_project_rpc_restart_and_read_only_stopped_status(
    git_repo: Path, tmp_path: Path, cli_environment: dict[str, str]
) -> None:
    env = cli_environment
    control = create_runtime_controller()
    try:
        launched = invoke(tmp_path, env, "start", "--json")
        assert launched.returncode == 0, launched.stderr
        started = ControlSuccess.model_validate_json(launched.stdout)
        assert started.data.application_ready and started.data.identity is not None
        repeated = invoke(tmp_path, env, "start", "--json")
        assert (
            ControlSuccess.model_validate_json(repeated.stdout).data.identity
            == started.data.identity
        )
        result = invoke(git_repo, env, "init", "--json")
        assert result.returncode == 0, result.stderr
        registered = Success.model_validate_json(result.stdout)
        config = git_repo / "jeff.local.yaml"
        config.write_bytes(
            config.read_bytes() + b"# Preserve comment\nprivate: SYNTHETIC_C_PRIVATE\n"
        )
        preserved = config.read_bytes(), (git_repo / ".gitignore").read_bytes()
        db = Path(env["JEFF_DATA_DIR"]) / "state.sqlite3"
        before = db.read_bytes(), db.stat().st_mtime_ns
        for operation in ("config", "status"):
            checked = invoke(git_repo, env, operation, "--json")
            assert checked.returncode == 0, checked.stderr
            if operation == "config":
                data = Success.model_validate_json(checked.stdout).data
            else:
                status = ProjectStatusSuccess.model_validate_json(checked.stdout)
                assert status.data.runtime.application_ready
                data = status.data
            assert data.project_id == registered.data.project_id
        assert (db.read_bytes(), db.stat().st_mtime_ns) == before
        for question in ("what branch am I on?", "what changed?"):
            answer = invoke(git_repo, env, "ask", question, "--json")
            assert answer.returncode == 0, answer.stderr
            assert (
                QuestionSuccess.model_validate_json(answer.stdout).data.project_id
                == registered.data.project_id
            )
        unknown = invoke(git_repo, env, "ask", "SYNTHETIC_PRIVATE_UNSUPPORTED", "--json")
        assert Failure.model_validate_json(unknown.stdout).error.code == "unsupported_intent"
        assert "SYNTHETIC_PRIVATE_UNSUPPORTED" not in unknown.stdout + unknown.stderr
        stopped = invoke(tmp_path, env, "stop", "--json")
        assert (
            stopped.returncode == 0
            and ControlSuccess.model_validate_json(stopped.stdout).data.state.value == "stopped"
        )
        before = db.read_bytes(), db.stat().st_mtime_ns
        diagnostic = invoke(git_repo, env, "status", "--json")
        assert diagnostic.returncode == 0, diagnostic.stderr
        assert (
            ProjectStatusSuccess.model_validate_json(diagnostic.stdout).data.runtime.state.value
            == "stopped"
        )
        assert (db.read_bytes(), db.stat().st_mtime_ns) == before
        assert not (Path(env["JEFF_RUNTIME_DIR"]) / "daemon.sock").exists()
        denied = invoke(git_repo, env, "init", "--json")
        assert Failure.model_validate_json(denied.stdout).error.code == "daemon_not_running"
        assert (config.read_bytes(), (git_repo / ".gitignore").read_bytes()) == preserved
        assert invoke(tmp_path, env, "start", "--json").returncode == 0
        again = invoke(git_repo, env, "init", "--json")
        assert again.returncode == 0, again.stderr
        assert (
            Success.model_validate_json(again.stdout).data.project_id == registered.data.project_id
        )
        assert (config.read_bytes(), (git_repo / ".gitignore").read_bytes()) == preserved
        assert b"SYNTHETIC_C_PRIVATE" not in db.read_bytes()
    finally:
        assert isinstance(control.stop(), ControlSuccess)
        (Path(env["JEFF_DATA_DIR"]) / "ipc.key").unlink(missing_ok=True)


def test_actual_cli_lost_reply_and_duplicate_keep_json_body_and_stderr_correlation(
    git_repo: Path, cli_environment: dict[str, str], running_cli: dict[str, str]
) -> None:
    env = cli_environment
    assert invoke(git_repo, env, "init", "--json").returncode == 0
    script = r"""
import socket,subprocess,sys
original_socket=socket.socket
class DropReply(original_socket):
    def sendall(self,data,*args,**kwargs):
        result=super().sendall(data,*args,**kwargs)
        if b'"kind":"project"' in data:self.drop=True
        return result
    def recv(self,*args,**kwargs):
        if getattr(self,'drop',False):
            self.drop=False
            raise OSError('SYNTHETIC_LOST_REPLY')
        return super().recv(*args,**kwargs)
socket.socket=DropReply
original_popen=subprocess.Popen
def no_local_git(command,*args,**kwargs):
    if command[0]=='/usr/bin/git':raise AssertionError('Client must not execute Git')
    return original_popen(command,*args,**kwargs)
subprocess.Popen=no_local_git
from jeff.bootstrap.main import main
sys.argv=['jeff','ask','what branch am I on?','--json']
main()
"""
    lost = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script],
        cwd=git_repo,
        env={"PATH": os.defpath, "NO_COLOR": "1", **env},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert lost.returncode == 1, lost.stderr
    assert Failure.model_validate_json(lost.stdout).error.code == "request_outcome_unknown"
    assert lost.stderr.startswith("Request: ") and len(lost.stderr.splitlines()) == 1
    request_id = UUID(lost.stderr.removeprefix("Request: ").strip())
    assert (
        str(request_id) not in lost.stdout
        and "SYNTHETIC_LOST_REPLY" not in lost.stdout + lost.stderr
    )
    client = create_project_client()
    until = time.monotonic() + 5
    while True:
        found = client.inspect_receipt(ReceiptQuery(directory=git_repo, request_id=request_id))
        if found.receipt is not None and found.receipt.state == "succeeded":
            break
        assert time.monotonic() < until, found
        time.sleep(0.01)
    # OS-random boundary controls only the caller's request UUID; no credential creation.
    repeated_script = f"""
import os,sys
original=os.urandom
os.urandom=lambda n: {request_id.bytes!r} if n==16 else original(n)
from jeff.bootstrap.main import main
sys.argv=['jeff','ask','what branch am I on?','--json']
main()
"""
    repeated = subprocess.run(
        [sys.executable, "-I", "-B", "-c", repeated_script],
        cwd=git_repo,
        env={"PATH": os.defpath, "NO_COLOR": "1", **env},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert repeated.returncode == 1, repeated.stderr
    assert Failure.model_validate_json(repeated.stdout).error.code == "duplicate_request"
    assert repeated.stderr == f"Request: {request_id}\n" and str(request_id) not in repeated.stdout
    assert (
        client.inspect_receipt(ReceiptQuery(directory=git_repo, request_id=request_id)).receipt
        == found.receipt
    )
