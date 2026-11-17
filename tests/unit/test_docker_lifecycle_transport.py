"""Fixed transport argv and streaming through real pipes, never a native child or Docker."""

import os
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, NoReturn
from uuid import uuid4

import pytest

from jeff.application.execution import (
    AuthorityRecord,
    DockerBackendFault,
    DockerLaunchIntent,
    ExecutionManifest,
    ExecutionProfile,
    ExecutionStopSignal,
    PreparedInputBinding,
    PreparedInputs,
    VerifiedDockerTarget,
)
from jeff.domain.execution import AuthorityState
from jeff.infrastructure.docker_cli import DockerLifecycleCLI


def test_cli_transport(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    binary = tmp_path / "docker"
    binary.write_bytes(b"synthetic never executed")
    binary.chmod(0o700)
    config = tmp_path / "cli"
    config.mkdir(mode=0o700)
    (config / "config.json").write_bytes(b"{}")
    (config / "config.json").chmod(0o600)
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    path = tmp_path / "s"
    endpoint = "unix://" + str(path)
    identifier = "c" * 64
    calls: list[list[str]] = []
    bad_listing = False
    sink_failure = False
    cancel_signal: ExecutionStopSignal | None = None
    killed: list[str] = []

    class Child:
        def __init__(self, output: bytes, error: bytes, code: int, alive: bool = False) -> None:
            self.returncode = code
            self.alive = alive
            r, w = os.pipe()
            os.write(w, output)
            os.close(w)
            self.stdout = os.fdopen(r, "rb")
            r, w = os.pipe()
            os.write(w, error)
            os.close(w)
            self.stderr = os.fdopen(r, "rb")

        def poll(self) -> int | None:
            return None if self.alive else self.returncode

        def wait(self, timeout: float) -> int:
            assert not self.alive
            return self.returncode

        def terminate(self) -> NoReturn:
            raise AssertionError("No live child.")

        def kill(self) -> None:
            killed.append("captured-cli")
            self.alive = False
            self.returncode = -9

    def spawn(argv: list[str], **kwargs: Any) -> Child:
        calls.append(argv)
        assert argv[:5] == [str(binary), "--config", str(config), "--host", endpoint]
        assert kwargs["env"]["HOME"] == str(home) and kwargs["env"]["DOCKER_CONFIG"] == str(config)
        assert kwargs["stdin"] == subprocess.DEVNULL and kwargs["close_fds"] is True
        assert not {"DOCKER_CONTEXT", "PYTHONPATH", "SSH_AUTH_SOCK"} & set(kwargs["env"])
        (home / "Library").mkdir(exist_ok=True)
        if argv[5:7] == ["container", "start"]:
            assert argv[7:] == ["--attach", "--", identifier]
            return Child(
                b"public-out", b"public-err", 7, alive=sink_failure or cancel_signal is not None
            )
        assert "--force" not in argv and "-f" not in argv and "--volumes" not in argv
        if argv[5] == "create":
            assert "--pull=never" in argv and "--read-only" in argv
            (config / "unexpected").mkdir()
            return Child((identifier + "\n").encode(), b"", 0)
        if argv[5:7] == ["container", "ls"]:
            assert argv[-2:] == ["--filter", "id=" + identifier]
            return Child(b"unexpected" if bad_listing else b"", b"", 0)
        assert argv[-2:] == ["--", identifier]
        return Child((identifier + "\n").encode(), b"", 0)

    class Sink:
        retention_limit = 1048576

        def __init__(self) -> None:
            self.counts: dict[str, int] = {}
            self.finished: list[str] = []

        def accept(self, stream: str, data: bytes) -> None:
            if sink_failure:
                raise ValueError("synthetic output must not escape")
            self.counts[stream] = self.counts.get(stream, 0) + len(data)
            if cancel_signal is not None:
                cancel_signal.request(uuid4(), "cancel")

        def finish(self, stream: str) -> None:
            self.finished.append(stream)

    monkeypatch.setattr(subprocess, "Popen", spawn)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(path))
        backend = DockerLifecycleCLI(binary, config, home)
        sink = Sink()
        assert backend.attach(endpoint, identifier, sink, deadline=time.monotonic() + 5) == 7
        assert sink.counts == {"stdout": 10, "stderr": 10} and sorted(sink.finished) == [
            "stderr",
            "stdout",
        ]
        backend.stop(endpoint, identifier, deadline=time.monotonic() + 5)
        backend.kill(endpoint, identifier, deadline=time.monotonic() + 5)
        backend.remove(endpoint, identifier, deadline=time.monotonic() + 5)
        assert backend.absent(endpoint, identifier, deadline=time.monotonic() + 5)
        assert calls[1][5:] == ["container", "stop", "--timeout", "1", "--", identifier]
        assert calls[2][5:] == ["container", "kill", "--signal", "KILL", "--", identifier]
        assert calls[3][5:] == ["container", "rm", "--", identifier]
        before = len(calls)
        with pytest.raises(DockerBackendFault):
            backend.remove(endpoint, "not-a-full-container-id", deadline=time.monotonic() + 5)
        assert len(calls) == before
        bad_listing = True
        with pytest.raises(DockerBackendFault) as ambiguous:
            backend.absent(endpoint, identifier, deadline=time.monotonic() + 5)
        assert ambiguous.value.code == "docker_protocol_error"
        sink_failure = True
        with pytest.raises(DockerBackendFault) as output_failed:
            backend.attach(endpoint, identifier, Sink(), deadline=time.monotonic() + 5)
        assert output_failed.value.code == "docker_output_failed"
        assert killed == ["captured-cli"]  # No terminate/SIGTERM forwarding through start --attach.
        assert "synthetic output" not in str(output_failed.value)
        assert sorted(p.name for p in config.iterdir()) == ["config.json"]
        profile = ExecutionProfile(
            name="python",
            project_id=uuid4(),
            canonical_root=tmp_path / "project",
            engine_id="synthetic",
            endpoint=endpoint,
            image_id="sha256:" + "1" * 64,
            platform="linux/arm64",
            toolchain="synthetic",
            executable="/usr/local/bin/python",
        )
        inputs = PreparedInputs()
        manifest = ExecutionManifest(
            profile=profile, argv=(profile.executable, "-V"), inputs=inputs
        )
        record = AuthorityRecord(
            proposal_id=uuid4(),
            approval_id=uuid4(),
            job_id=uuid4(),
            project_id=profile.project_id,
            canonical_root=profile.canonical_root,
            owner_instance_id=uuid4(),
            manifest=manifest,
            manifest_digest=manifest.fingerprint,
            state=AuthorityState.CONSUMED,
            created_at_ms=100,
            expires_at_ms=600100,
            claim_id=uuid4(),
            claim_state="claimed",
            claimed_at_ms=200,
        )
        sink_failure = False
        cancel_signal = ExecutionStopSignal(record, job_deadline=time.monotonic() + 30)
        before = len(calls)
        with pytest.raises(DockerBackendFault) as cancelled:
            backend.attach_cancellable(
                endpoint,
                identifier,
                Sink(),
                cancel_signal,
                lambda: None,
                deadline=time.monotonic() + 5,
            )
        assert cancelled.value.code == "docker_cancelled"
        assert len(calls) == before + 1 and killed == ["captured-cli", "captured-cli"]
        backend.stop_gracefully(
            endpoint, identifier, grace_seconds=5, deadline=time.monotonic() + 10
        )
        assert calls[-1][5:] == ["container", "stop", "--timeout", "5", "--", identifier]
        target = VerifiedDockerTarget(
            declaration_digest="a" * 64,
            profile_digest=profile.fingerprint,
            binary=backend.identity,
            endpoint=endpoint,
            engine_id=profile.engine_id,
            image_id=profile.image_id,
            platform=profile.platform,
            client_version="29.9.3",
            server_version="29.9.3",
            image_configuration_digest="c" * 64,
            effective_environment=(),
        )
        intent = DockerLaunchIntent(
            attempt_id=uuid4(),
            record=record,
            target=target,
            inputs=PreparedInputBinding(
                snapshot_id=inputs.snapshot_id,
                inventory_digest=inputs.digest,
                path=tmp_path / "prepared",
            ),
            nonce="d" * 32,
        )
        with pytest.raises(DockerBackendFault) as post_check:
            backend.create(intent, deadline=time.monotonic() + 5)
        assert post_check.value.code == "docker_mutation_unconfirmed"
        assert post_check.value.observed_container_id == identifier
        assert sorted(p.name for p in config.iterdir()) == ["config.json", "unexpected"]
