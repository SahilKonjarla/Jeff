"""Canonical synthetic backend, journal, input, output and target-verification ports."""

import copy
import threading
import time
from pathlib import Path

from jeff.application.execution import (
    DockerBackendFault,
    DockerLaunchEvent,
    DockerLaunchIntent,
    DockerOutputSink,
    DockerTargetDeclaration,
    ExecutionManifest,
    ExecutionStopSignal,
    PreparedInputBinding,
    VerifiedDockerTarget,
)


def labels(intent: DockerLaunchIntent) -> dict[str, str]:
    return {
        "jeff.job": str(intent.record.job_id),
        "jeff.claim": str(intent.record.claim_id),
        "jeff.attempt": str(intent.attempt_id),
        "jeff.nonce": intent.nonce,
    }


class Journal:
    def __init__(self) -> None:
        self.events: list[DockerLaunchEvent] = []
        self.claims: set[object] = set()
        self.fail_phase: str | None = None

    def record(self, event: DockerLaunchEvent, *, deadline: float) -> None:
        assert deadline > time.monotonic()
        if event.phase == "create_intent":
            assert event.intent.record.claim_id not in self.claims
            self.claims.add(event.intent.record.claim_id)
        self.events.append(event)
        if event.phase == self.fail_phase:
            raise RuntimeError("synthetic uncertain journal acknowledgement")


class Inputs:
    changed = False

    def verify(self, manifest: ExecutionManifest, *, deadline: float) -> PreparedInputBinding:
        return PreparedInputBinding(
            snapshot_id=manifest.inputs.snapshot_id,
            inventory_digest=manifest.inputs.digest,
            path=Path("/synthetic/prepared/input"),
        )

    def check(
        self, binding: PreparedInputBinding, manifest: ExecutionManifest, *, deadline: float
    ) -> None:
        if self.changed:
            raise ValueError("synthetic changed prepared view")
        assert binding.snapshot_id == manifest.inputs.snapshot_id
        assert binding.inventory_digest == manifest.inputs.digest


class Sink:
    retention_limit = 1048576

    def __init__(self) -> None:
        self.count = 0

    def accept(self, stream: str, data: bytes) -> None:
        self.count += len(data)  # Discard synthetic raw bytes; never a persistence implementation.

    def finish(self, stream: str) -> None:
        pass


class Backend:
    def __init__(self, target: VerifiedDockerTarget, journal: Journal) -> None:
        self.target, self.journal = target, journal
        self.identity = target.binary
        self.calls: list[str] = []
        self.raw: dict[str, object] = {}
        self.running = False
        self.removed = False
        self.exit_code = 7
        self.uncertain_create = False
        self.timeout_attach = False
        self.stop_leaves_running = False
        self.remove_reply_lost = False
        self.attach_status: int | None = None
        self.fail_after_eof = False
        self.state_override: dict[str, object] | None = None
        self.inspect_unavailable = False
        self.entered: threading.Event | None = None
        self.release: threading.Event | None = None

    def engine_id(self, endpoint: str, *, deadline: float) -> str:
        return self.target.engine_id

    def create(self, intent: DockerLaunchIntent, *, deadline: float) -> str:
        assert self.journal.events[-1].phase == "create_intent"
        self.calls.append("create")
        if self.uncertain_create:
            raise DockerBackendFault("docker_command_failed")
        p = intent.record.manifest.profile
        self.raw = {
            "Id": "c" * 64,
            "Created": "2026-01-01T00:00:00.000000000Z",
            "Image": p.image_id,
            "Path": p.executable,
            "Args": list(intent.record.manifest.argv[1:]),
            "Config": {
                "User": "65532:65532",
                "WorkingDir": "/project",
                "Entrypoint": [p.executable],
                "Cmd": list(intent.record.manifest.argv[1:]),
                "Env": [f"{k}={v}" for k, v in intent.target.effective_environment],
                "Labels": labels(intent),
                "OpenStdin": False,
                "AttachStdin": False,
                "Tty": False,
                "Healthcheck": {"Test": ["NONE"]},
                "StopSignal": "SIGTERM",
            },
            "HostConfig": {
                "ReadonlyRootfs": True,
                "Privileged": False,
                "NetworkMode": "none",
                "IpcMode": "private",
                "PidMode": "",
                "UTSMode": "",
                "CgroupnsMode": "private",
                "RestartPolicy": {"Name": "no"},
                "AutoRemove": False,
                "CapDrop": ["ALL"],
                "CapAdd": None,
                "SecurityOpt": ["no-new-privileges=true"],
                "Memory": p.memory_bytes,
                "MemorySwap": p.memory_bytes,
                "NanoCpus": p.cpu_millis * 1000000,
                "PidsLimit": p.pids,
                "ShmSize": 4194304,
                "LogConfig": {"Type": "none"},
                "Tmpfs": {
                    "/scratch": (
                        f"rw,noexec,nosuid,nodev,size={p.scratch_bytes},"
                        "nr_inodes=4096,uid=65532,gid=65532,mode=0700"
                    )
                },
                "Runtime": "runc",
                "PublishAllPorts": False,
            },
            "Mounts": [
                {
                    "Type": "bind",
                    "Destination": "/project",
                    "Source": str(intent.inputs.path),
                    "RW": False,
                    "Propagation": "rprivate",
                },
                {"Type": "tmpfs", "Destination": "/scratch", "RW": True},
            ],
        }
        return "c" * 64

    def inspect(self, endpoint: str, container_id: str, *, deadline: float) -> dict[str, object]:
        assert container_id == "c" * 64
        if self.inspect_unavailable:
            raise DockerBackendFault("docker_unavailable")
        raw = copy.deepcopy(self.raw)
        raw["State"] = {
            "Status": "running"
            if self.running
            else "created"
            if "attach" not in self.calls
            else "exited",
            "Running": self.running,
            "ExitCode": self.exit_code,
        }
        if self.state_override is not None:
            state = raw["State"]
            assert isinstance(state, dict)
            state.update(self.state_override)
        return raw

    def attach(
        self, endpoint: str, container_id: str, sink: DockerOutputSink, *, deadline: float
    ) -> int:
        assert self.journal.events[-1].phase == "start_intent"
        self.calls.append("attach")
        if self.timeout_attach:
            self.running = True
            raise DockerBackendFault("docker_timeout")
        if self.entered is not None and self.release is not None:
            self.running = True
            self.entered.set()
            assert self.release.wait(2)
            self.running = False
        sink.accept("stdout", b"synthetic-output")
        sink.finish("stdout")
        sink.finish("stderr")
        if self.fail_after_eof:
            raise DockerBackendFault("docker_target_mismatch")
        return self.exit_code if self.attach_status is None else self.attach_status

    def stop(self, endpoint: str, container_id: str, *, deadline: float) -> None:
        assert container_id == "c" * 64
        self.calls.append("stop")
        self.running = self.stop_leaves_running

    def kill(self, endpoint: str, container_id: str, *, deadline: float) -> None:
        assert container_id == "c" * 64
        self.calls.append("kill")
        self.running = False

    def remove(self, endpoint: str, container_id: str, *, deadline: float) -> None:
        assert not self.running and container_id == "c" * 64
        self.calls.append("remove")
        self.removed = True
        if self.remove_reply_lost:
            raise DockerBackendFault("docker_command_failed")

    def absent(self, endpoint: str, container_id: str, *, deadline: float) -> bool:
        return self.removed


class Verifier:
    def __init__(self, target: VerifiedDockerTarget) -> None:
        self.target = target

    def verify_target_cancellable(
        self, declaration: DockerTargetDeclaration, stop: "ExecutionStopSignal", *, deadline: float
    ) -> VerifiedDockerTarget:
        stop.check()
        return self.verify_target(declaration, deadline=deadline)

    def verify_target(
        self, declaration: DockerTargetDeclaration, *, deadline: float
    ) -> VerifiedDockerTarget:
        assert declaration.fingerprint == self.target.declaration_digest
        return self.target
