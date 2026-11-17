"""Synthetic Docker metadata/lifecycle I/O only; never a Docker daemon or child process."""

import copy
import json
import threading
import time
from collections.abc import Callable
from pathlib import Path

from jeff.application.execution import (
    DockerBackendFault,
    DockerCLIIdentity,
    DockerLaunchIntent,
    DockerOutputSink,
    ExecutionStopSignal,
)


class Docker:
    def __init__(self) -> None:
        self.identity = DockerCLIIdentity(path=Path("/synthetic/docker"), sha256="a" * 64, size=10)
        self.engine = {
            "ID": "synthetic-engine",
            "OSType": "linux",
            "Architecture": "aarch64",
            "OperatingSystem": "Docker Desktop",
            "ServerVersion": "29.9.3",
            "SecurityOptions": ["name=seccomp,profile=builtin", "name=cgroupns"],
            "MemoryLimit": True,
            "PidsLimit": True,
            "CPUCfsPeriod": True,
            "CPUCfsQuota": True,
        }
        self.image = {
            "ID": "sha256:" + "1" * 64,
            "OS": "linux",
            "Architecture": "arm64",
            "Variant": "",
            "Config": {
                "Env": ["LANG=C.UTF-8", "PATH=/usr/local/bin:/usr/bin:/bin"],
                "User": "",
                "Entrypoint": None,
                "Cmd": ["python3"],
                "WorkingDir": "",
                "Volumes": None,
                "Healthcheck": None,
                "OnBuild": None,
            },
        }
        self.intents: list[DockerLaunchIntent] = []
        self.calls: list[str] = []
        self.queries: list[str] = []
        self.state = "absent"
        self.cid = ""
        self.raw: dict[str, object] = {}
        self.failure: str | None = None
        self.cancel_mode = False
        self.output = b"public output\n"
        self.exit_code = 0
        self.copied: list[bytes] = []
        self.entered: threading.Event | None = None
        self.release: threading.Event | None = None

    def check(self) -> None:
        pass

    def read(self, query: str, *, endpoint: str, image_id: str | None, deadline: float) -> bytes:
        self.queries.append(query)
        return json.dumps(
            "29.9.3"
            if query == "client.version"
            else self.image
            if query == "image.inspect"
            else self.engine
        ).encode()

    def engine_id(self, endpoint: str, *, deadline: float) -> str:
        return str(self.engine["ID"])

    def create(self, intent: DockerLaunchIntent, *, deadline: float) -> str:
        self.calls.append("create")
        self.intents.append(intent)
        if self.failure == "create":
            raise DockerBackendFault("docker_mutation_unconfirmed")
        self.cid = f"{len(self.intents):064x}"
        self.state = "created"
        self.copied.append((intent.inputs.path / "source.py").read_bytes())
        p = intent.record.manifest.profile
        self.raw = {
            "Id": self.cid,
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
                "Labels": {
                    "jeff.job": str(intent.record.job_id),
                    "jeff.claim": str(intent.record.claim_id),
                    "jeff.attempt": str(intent.attempt_id),
                    "jeff.nonce": intent.nonce,
                },
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
                "Runtime": "runc",
                "PublishAllPorts": False,
                "Tmpfs": {
                    "/scratch": (
                        f"rw,noexec,nosuid,nodev,size={p.scratch_bytes},"
                        "nr_inodes=4096,uid=65532,gid=65532,mode=0700"
                    )
                },
            },
            "Mounts": [
                {
                    "Type": "bind",
                    "Destination": "/project",
                    "Source": str(intent.inputs.path),
                    "RW": False,
                    "Propagation": "rprivate",
                },
                # Shape consistent with live diagnostic booleans, not a raw replay:
                # legacy tmpfs stays in exact HostConfig.Tmpfs, not this listing.
            ],
        }
        return self.cid

    def inspect(self, endpoint: str, container_id: str, *, deadline: float) -> dict[str, object]:
        if self.failure == "cleanup":
            raise DockerBackendFault("docker_ownership_unconfirmed")
        assert container_id == self.cid
        raw = copy.deepcopy(self.raw)
        raw["State"] = {
            "Status": self.state,
            "Running": self.state == "running",
            "ExitCode": self.exit_code,
        }
        return raw

    def attach(
        self, endpoint: str, container_id: str, sink: DockerOutputSink, *, deadline: float
    ) -> int:
        self.calls.append("attach")
        self.state = "running"
        if self.failure == "timeout":
            raise DockerBackendFault("docker_timeout")
        if self.entered is not None and self.release is not None:
            self.entered.set()
            assert self.release.wait(3)
        sink.accept("stdout", self.output)
        sink.finish("stdout")
        sink.finish("stderr")
        self.state = "exited"
        if self.failure == "after_eof":
            raise DockerBackendFault("docker_target_mismatch")
        return self.exit_code

    def attach_cancellable(
        self,
        endpoint: str,
        container_id: str,
        sink: DockerOutputSink,
        stop: "ExecutionStopSignal",
        observe_running: "Callable[[], None]",
        *,
        deadline: float,
    ) -> int:
        if not self.cancel_mode:
            return self.attach(endpoint, container_id, sink, deadline=deadline)
        self.calls.append("attach")
        self.state = "running"
        sink.accept("stdout", self.output)
        observe_running()
        if self.entered is not None:
            self.entered.set()
        end = time.monotonic() + 3
        while not stop.requested and time.monotonic() < end:
            threading.Event().wait(0.005)
        stop.check()
        raise AssertionError("Finite fake expected a stop request")

    def stop_gracefully(
        self, endpoint: str, container_id: str, *, grace_seconds: int, deadline: float
    ) -> None:
        assert 0 <= grace_seconds <= 5
        self.stop(endpoint, container_id, deadline=deadline)

    def stop(self, endpoint: str, container_id: str, *, deadline: float) -> None:
        self.calls.append("stop")
        self.state = "exited"

    def kill(self, endpoint: str, container_id: str, *, deadline: float) -> None:
        self.calls.append("kill")
        self.state = "exited"

    def remove(self, endpoint: str, container_id: str, *, deadline: float) -> None:
        self.calls.append("remove")
        self.state = "absent"

    def absent(self, endpoint: str, container_id: str, *, deadline: float) -> bool:
        return self.state == "absent"
