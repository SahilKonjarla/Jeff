"""Production JFG2 attachment over synthetic local pipes; never spawn a child or Docker."""

import os
import subprocess
import time
from pathlib import Path
from typing import Any, BinaryIO, cast
from uuid import UUID, uuid4

import pytest
from test_guard_execution import guarded as guarded

from jeff.application.execution import (
    AuthorityRecord,
    DockerBackendFault,
    DockerCLIIdentity,
    DockerLaunchIntent,
    DockerOwnedContainer,
    GuardedExecutionManifest,
    GuardV2Challenge,
    GuardV2Dispatch,
    GuardV2Stderr,
    GuardV2Terminal,
    PreparedInputBinding,
    VerifiedDockerTarget,
    encode_guard_v2_frame,
)
from jeff.domain.execution import AuthorityState
from jeff.infrastructure.docker_cli import DockerLifecycleCLI
from jeff.infrastructure.runtime_output import GuardedOutputCollector


def owned(action: GuardedExecutionManifest) -> DockerOwnedContainer:
    p = action.profile
    record = AuthorityRecord(
        proposal_id=uuid4(),
        approval_id=uuid4(),
        job_id=uuid4(),
        project_id=p.project_id,
        canonical_root=p.canonical_root,
        owner_instance_id=uuid4(),
        manifest=action,
        manifest_digest=action.fingerprint,
        state=AuthorityState.CONSUMED,
        created_at_ms=1,
        expires_at_ms=600001,
        claim_id=uuid4(),
        claim_state="claimed",
        claimed_at_ms=2,
    )
    target = VerifiedDockerTarget(
        declaration_digest="1" * 64,
        profile_digest=p.fingerprint,
        binary=DockerCLIIdentity(path=Path("/synthetic/docker"), sha256="2" * 64, size=1),
        endpoint=p.endpoint,
        engine_id=p.engine_id,
        image_id=p.image_id,
        platform=p.platform,
        client_version="29.9.3",
        server_version="29.9.3",
        image_configuration_digest="3" * 64,
        effective_environment=(),
    )
    return DockerOwnedContainer(
        intent=DockerLaunchIntent(
            attempt_id=uuid4(),
            record=record,
            target=target,
            inputs=PreparedInputBinding(
                snapshot_id=action.inputs.snapshot_id,
                inventory_digest=action.inputs.digest,
                path=Path("/synthetic/input"),
            ),
            nonce="4" * 32,
        ),
        container_id="5" * 64,
        created_at="2026-01-01T00:00:00.000000000Z",
    )


@pytest.mark.parametrize(
    "fault", [None, "partial_auth", "missing_terminal", "trailing", "clock_fault"]
)
def test_original_attachment_separates_protocol_and_retains_unknown_pipes(
    guarded: GuardedExecutionManifest,
    monkeypatch: pytest.MonkeyPatch,
    fault: str | None,
) -> None:
    handle = owned(guarded)
    backend = object.__new__(DockerLifecycleCLI)
    backend.binary, backend.config, backend.home = (
        Path("/synthetic/docker"),
        Path("/cfg"),
        Path("/home"),
    )
    backend._guarded = {}
    monkeypatch.setattr(backend, "check", lambda: None)
    monkeypatch.setattr(backend, "_socket", lambda endpoint: (1,))
    identity = dict(nonce=UUID(int=2), manifest_digest=guarded.fingerprint)
    challenge = encode_guard_v2_frame(
        GuardV2Challenge.model_validate(
            identity
            | dict(
                issued_ns=100,
                startup_deadline_ns=100 + guarded.startup_wait_ns,
                max_runtime_ns=guarded.profile.timeout_seconds * 1000000000,
            )
        )
    )
    dispatch = encode_guard_v2_frame(
        GuardV2Dispatch.model_validate(identity | dict(sequence=1, observed_ns=101))
    )
    opaque = b"JFG2-opaque-user-data\n"
    error = encode_guard_v2_frame(
        GuardV2Stderr.model_validate(identity | dict(sequence=2, data=opaque))
    )
    terminal = encode_guard_v2_frame(
        GuardV2Terminal.model_validate(
            identity
            | dict(sequence=3, observed_ns=102, stderr_eof=True, stderr_dropped=False)
            | (
                dict(cause="guard_fault", fault="clock_regression")
                if fault == "clock_fault"
                else dict(cause="workload_exit", exit_code=124)
            )
        )
    )
    control_read, control_write = os.pipe()
    out_read, out_write = os.pipe()
    err_read, err_write = os.pipe()
    pipes: list[BinaryIO] = [
        os.fdopen(control_write, "wb"),
        os.fdopen(out_read, "rb"),
        os.fdopen(err_read, "rb"),
    ]
    original_write = os.write
    original_write(out_write, challenge)
    calls: list[list[str]] = []
    writes: list[bytes] = []

    class Process:
        stdin, stdout, stderr = pipes

        def poll(self) -> int:
            return 124

        def wait(self, timeout: float) -> int:
            return 124

        def kill(self) -> None:
            raise AssertionError("Unknown attachment must not be killed")

    process = Process()

    def spawn(argv: list[str], **kwargs: Any) -> Process:
        calls.append(argv)
        assert kwargs["stdin"] == subprocess.PIPE and kwargs["close_fds"] is True
        assert argv[5:] == [
            "container",
            "start",
            "--attach",
            "--interactive",
            "--",
            handle.container_id,
        ]
        return process

    def write(fd: int, data: bytes) -> int:
        assert fd == control_write
        writes.append(data)
        if fault == "partial_auth":
            return original_write(fd, data[:-1])
        count = original_write(fd, data)
        original_write(out_write, opaque)
        wire = dispatch + error + (b"" if fault == "missing_terminal" else terminal)
        original_write(err_write, wire + (b"x" if fault == "trailing" else b""))
        os.close(out_write)
        os.close(err_write)
        return count

    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setattr(os, "write", write)
    sink = GuardedOutputCollector(guarded.profile.output_bytes)
    try:
        if fault is not None:
            with pytest.raises(DockerBackendFault):
                backend.attach_guarded(handle, sink, None, None, deadline=time.monotonic() + 5)
            capture = backend._guarded[id(handle)]
            assert capture.handle is handle and cast(object, capture.process) is process
            assert capture.unknown
            assert all(not pipe.closed for pipe in pipes)
            assert not backend.release_guarded(handle, deadline=time.monotonic() + 1)
            with pytest.raises(DockerBackendFault):
                backend.attach_guarded(handle, sink, None, None, deadline=time.monotonic() + 5)
        else:
            code, observed = backend.attach_guarded(
                handle, sink, None, None, deadline=time.monotonic() + 5
            )
            assert code == observed.exit_code == 124 and observed.cause == "workload_exit"
            snapshot = sink.seal()
            assert snapshot.stdout == snapshot.stderr == opaque.decode()
            assert snapshot.complete
            assert all(not pipe.closed for pipe in pipes)  # No early control closure.
            # Explicit synthetic owned-terminal confirmation at the release seam.
            assert backend.release_guarded(handle, deadline=time.monotonic() + 1)
        assert len(calls) == len(writes) == 1
    finally:
        # Only this test's SYNTHETIC pipe fixtures, never production captures.
        for pipe in pipes:
            pipe.close()
        for fd in (control_read, out_write, err_write):
            try:
                os.close(fd)
            except OSError:
                pass
