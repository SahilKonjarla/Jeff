"""Synthetic lifecycle ports: no actual Docker, SQL, prepared copy or raw-log persistence."""

import copy
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from jeff.application.execution import (
    AuthorityRecord,
    AuthorityResult,
    DockerBackendFault,
    DockerCLIIdentity,
    DockerLaunchEvent,
    DockerLaunchIntent,
    DockerOutputSink,
    DockerTargetDeclaration,
    ExecutionManifest,
    ExecutionProfile,
    PreparedInputBinding,
    PreparedInputs,
    VerifiedDockerTarget,
)
from jeff.domain.execution import AuthorityState
from jeff.infrastructure.runtime_execution import DockerJobController, create_arguments


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

    def verify_target(
        self, declaration: DockerTargetDeclaration, *, deadline: float
    ) -> VerifiedDockerTarget:
        assert declaration.fingerprint == self.target.declaration_digest
        return self.target


def setup_controller() -> tuple[
    DockerJobController, Backend, Journal, AuthorityResult, DockerTargetDeclaration
]:
    profile = ExecutionProfile(
        name="python",
        project_id=uuid4(),
        canonical_root=Path("/synthetic/project"),
        engine_id="synthetic-engine",
        endpoint="unix:///synthetic/docker.sock",
        image_id="sha256:" + "1" * 64,
        platform="linux/arm64",
        toolchain="synthetic",
        executable="/usr/local/bin/python",
    )
    declaration = DockerTargetDeclaration(profile=profile)
    manifest = ExecutionManifest(
        profile=profile, argv=(profile.executable, "-V"), inputs=PreparedInputs()
    )
    claim = AuthorityResult(
        record=AuthorityRecord(
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
    )
    claim = claim.model_copy(update={"launch_claim": claim.record.claim_id})
    target = VerifiedDockerTarget(
        declaration_digest=declaration.fingerprint,
        profile_digest=profile.fingerprint,
        binary=DockerCLIIdentity(path=Path("/synthetic/docker"), sha256="a" * 64, size=100),
        endpoint=profile.endpoint,
        engine_id=profile.engine_id,
        image_id=profile.image_id,
        platform=profile.platform,
        client_version="29.9.3",
        server_version="29.9.3",
        image_configuration_digest="b" * 64,
        effective_environment=(),
    )
    journal = Journal()
    backend = Backend(target, journal)
    controller = DockerJobController(
        Verifier(target),
        backend,
        Inputs(),
        journal,
        owner_instance_id=claim.record.owner_instance_id,
    )
    return controller, backend, journal, claim, declaration


def test_owned_nonzero_run_records_intents_and_removal_is_verified() -> None:
    controller, backend, journal, claim, declaration = setup_controller()
    handle = controller.create(
        claim, declaration, deadline=time.monotonic() + 5, cleanup_deadline=time.monotonic() + 10
    )
    args = create_arguments(handle.intent)
    assert args[0] == "create" and "--pull=never" in args
    assert "--read-only" in args and "--cap-drop=ALL" in args and "--no-healthcheck" in args
    assert "none" in args and "65532:65532" in args
    sink = Sink()
    result = controller.run(
        handle, sink, deadline=time.monotonic() + 5, cleanup_deadline=time.monotonic() + 10
    )
    assert result.exit_code == 7 and result.cleanup_verified and not result.outcome_unknown
    assert sink.count == len(b"synthetic-output")
    assert backend.calls == ["create", "attach", "remove"]
    assert [event.phase for event in journal.events] == [
        "create_intent",
        "created",
        "start_intent",
        "exited",
        "cleaned",
    ]


def test_claim_replay_and_uncertain_journal_never_create_again() -> None:
    controller, backend, journal, claim, declaration = setup_controller()
    with pytest.raises(DockerBackendFault):
        controller.create(
            claim.model_copy(update={"launch_claim": None}),
            declaration,
            deadline=time.monotonic() + 5,
            cleanup_deadline=time.monotonic() + 10,
        )
    assert backend.calls == [] and journal.events == []
    journal.fail_phase = "create_intent"
    with pytest.raises(DockerBackendFault) as failed:
        controller.create(
            claim,
            declaration,
            deadline=time.monotonic() + 5,
            cleanup_deadline=time.monotonic() + 10,
        )
    assert failed.value.code == "docker_journal_unconfirmed" and backend.calls == []
    journal.fail_phase = None
    other = DockerJobController(
        Verifier(backend.target),
        backend,
        Inputs(),
        journal,
        owner_instance_id=claim.record.owner_instance_id,
    )
    with pytest.raises(DockerBackendFault):
        other.create(
            claim,
            declaration,
            deadline=time.monotonic() + 5,
            cleanup_deadline=time.monotonic() + 10,
        )
    assert (
        backend.calls == []
    )  # External journal's unique claim reservation survives controller replacement.

    intent = journal.events[-1].intent
    birth = "2026-01-01T00:00:00.000000000Z"
    with pytest.raises(ValidationError):
        DockerLaunchEvent(intent=intent, phase="create_intent", created_at=birth)
    with pytest.raises(ValidationError):
        DockerLaunchEvent(intent=intent, phase="unknown", container_id="c" * 64)
    with pytest.raises(ValidationError):
        DockerLaunchEvent(
            intent=intent, phase="created", container_id="c" * 64, created_at="not-a-birth"
        )
    observed = DockerLaunchEvent(intent=intent, phase="unknown", observed_container_id="c" * 64)
    assert (
        observed.container_id is None
    )  # Partial create response remains observation, not ownership.


def test_ambiguous_create_and_copied_handles_never_adopt_or_restart() -> None:
    controller, backend, journal, claim, declaration = setup_controller()
    backend.uncertain_create = True
    with pytest.raises(DockerBackendFault):
        controller.create(
            claim,
            declaration,
            deadline=time.monotonic() + 5,
            cleanup_deadline=time.monotonic() + 10,
        )
    with pytest.raises(DockerBackendFault):
        controller.create(
            claim,
            declaration,
            deadline=time.monotonic() + 5,
            cleanup_deadline=time.monotonic() + 10,
        )
    assert backend.calls == ["create"] and journal.events[-1].phase == "unknown"
    controller, backend, journal, claim, declaration = setup_controller()
    handle = controller.create(
        claim, declaration, deadline=time.monotonic() + 5, cleanup_deadline=time.monotonic() + 10
    )
    with pytest.raises(DockerBackendFault) as forged:
        controller.run(
            handle.model_copy(),
            Sink(),
            deadline=time.monotonic() + 5,
            cleanup_deadline=time.monotonic() + 10,
        )
    assert forged.value.code == "docker_ownership_unconfirmed" and backend.calls == ["create"]
    controller.run(
        handle, Sink(), deadline=time.monotonic() + 5, cleanup_deadline=time.monotonic() + 10
    )
    with pytest.raises(DockerBackendFault):
        controller.run(
            handle, Sink(), deadline=time.monotonic() + 5, cleanup_deadline=time.monotonic() + 10
        )
    with pytest.raises(DockerBackendFault):
        controller.create(
            claim,
            declaration,
            deadline=time.monotonic() + 5,
            cleanup_deadline=time.monotonic() + 10,
        )
    assert backend.calls.count("attach") == backend.calls.count("create") == 1
    controller, backend, journal, claim, declaration = setup_controller()
    backend.inspect_unavailable = True
    with pytest.raises(DockerBackendFault):
        controller.create(
            claim,
            declaration,
            deadline=time.monotonic() + 5,
            cleanup_deadline=time.monotonic() + 10,
        )
    assert journal.events[-1].observed_container_id == "c" * 64
    assert journal.events[-1].container_id is None and backend.calls == ["create"]
    controller, backend, journal, claim, declaration = setup_controller()
    handle = controller.create(
        claim, declaration, deadline=time.monotonic() + 5, cleanup_deadline=time.monotonic() + 10
    )
    backend.attach_status = 125
    outcome = controller.run(
        handle, Sink(), deadline=time.monotonic() + 5, cleanup_deadline=time.monotonic() + 10
    )
    assert outcome.exit_code == 7 and outcome.outcome_unknown and outcome.cleanup_verified


def test_configuration_input_and_ownership_drift_fail_closed() -> None:
    controller, backend, journal, claim, declaration = setup_controller()
    handle = controller.create(
        claim, declaration, deadline=time.monotonic() + 5, cleanup_deadline=time.monotonic() + 10
    )
    config = backend.raw["Config"]
    assert isinstance(config, dict)
    config["User"] = "0"
    outcome = controller.run(
        handle, Sink(), deadline=time.monotonic() + 5, cleanup_deadline=time.monotonic() + 10
    )
    assert outcome.outcome_unknown and outcome.cleanup_verified
    assert backend.calls == [
        "create",
        "remove",
    ]  # Unsafe configuration does not prevent owned cleanup.
    controller, backend, journal, claim, declaration = setup_controller()
    handle = controller.create(
        claim, declaration, deadline=time.monotonic() + 5, cleanup_deadline=time.monotonic() + 10
    )
    changed = Inputs()
    changed.changed = True
    controller.inputs = changed
    outcome = controller.run(
        handle, Sink(), deadline=time.monotonic() + 5, cleanup_deadline=time.monotonic() + 10
    )
    assert outcome.cleanup_verified and backend.calls == ["create", "remove"]
    controller, backend, journal, claim, declaration = setup_controller()
    handle = controller.create(
        claim, declaration, deadline=time.monotonic() + 5, cleanup_deadline=time.monotonic() + 10
    )
    config = backend.raw["Config"]
    assert isinstance(config, dict)
    actual_labels = config["Labels"]
    assert isinstance(actual_labels, dict)
    actual_labels["jeff.nonce"] = "f" * 32
    outcome = controller.run(
        handle, Sink(), deadline=time.monotonic() + 5, cleanup_deadline=time.monotonic() + 10
    )
    assert outcome.outcome_unknown and not outcome.cleanup_verified and backend.calls == ["create"]

    for running in (True, None, "false"):
        controller, backend, journal, claim, declaration = setup_controller()
        handle = controller.create(
            claim,
            declaration,
            deadline=time.monotonic() + 5,
            cleanup_deadline=time.monotonic() + 10,
        )
        backend.state_override = {"Status": "created", "Running": running}
        outcome = controller.run(
            handle, Sink(), deadline=time.monotonic() + 5, cleanup_deadline=time.monotonic() + 10
        )
        assert outcome.outcome_unknown and "attach" not in backend.calls

    controller, backend, journal, claim, declaration = setup_controller()
    handle = controller.create(
        claim, declaration, deadline=time.monotonic() + 5, cleanup_deadline=time.monotonic() + 10
    )
    sink = Sink()
    sink.retention_limit = 2097152
    outcome = controller.run(
        handle, sink, deadline=time.monotonic() + 5, cleanup_deadline=time.monotonic() + 10
    )
    assert outcome.outcome_unknown and outcome.cleanup_verified and "attach" not in backend.calls


def test_timeout_has_bounded_exact_id_cleanup_and_remove_is_not_replayed() -> None:
    controller, backend, journal, claim, declaration = setup_controller()
    handle = controller.create(
        claim, declaration, deadline=time.monotonic() + 5, cleanup_deadline=time.monotonic() + 10
    )
    backend.timeout_attach = True
    backend.stop_leaves_running = True
    outcome = controller.run(
        handle, Sink(), deadline=time.monotonic() + 5, cleanup_deadline=time.monotonic() + 10
    )
    assert outcome.timed_out and outcome.outcome_unknown and outcome.cleanup_verified
    assert backend.calls == ["create", "attach", "stop", "kill", "remove"]
    assert controller.terminate_remove_owned(handle, deadline=time.monotonic() + 5)
    assert backend.calls.count("remove") == 1
    controller, backend, journal, claim, declaration = setup_controller()
    handle = controller.create(
        claim, declaration, deadline=time.monotonic() + 5, cleanup_deadline=time.monotonic() + 10
    )
    backend.remove_reply_lost = True
    outcome = controller.run(
        handle, Sink(), deadline=time.monotonic() + 5, cleanup_deadline=time.monotonic() + 10
    )
    assert outcome.exit_code == 7 and outcome.outcome_unknown and not outcome.cleanup_verified
    assert controller.terminate_remove_owned(handle, deadline=time.monotonic() + 5)
    assert backend.calls.count("remove") == 1  # Reconcile absence, never repeat rm after ACK loss.


def test_transition_serialization_and_uncertain_start_never_launch_twice() -> None:
    controller, backend, journal, claim, declaration = setup_controller()
    handle = controller.create(
        claim, declaration, deadline=time.monotonic() + 5, cleanup_deadline=time.monotonic() + 10
    )
    backend.entered = threading.Event()
    backend.release = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as workers:
        pending = workers.submit(
            controller.run,
            handle,
            Sink(),
            deadline=time.monotonic() + 5,
            cleanup_deadline=time.monotonic() + 10,
        )
        try:
            assert backend.entered.wait(1)
            with pytest.raises(DockerBackendFault) as busy:
                controller.terminate_remove_owned(handle, deadline=time.monotonic() + 5)
            assert busy.value.code == "docker_busy"
        finally:
            backend.release.set()
        assert pending.result(timeout=2).cleanup_verified
    assert backend.calls == ["create", "attach", "remove"]
    controller, backend, journal, claim, declaration = setup_controller()
    handle = controller.create(
        claim, declaration, deadline=time.monotonic() + 5, cleanup_deadline=time.monotonic() + 10
    )
    journal.fail_phase = "start_intent"
    outcome = controller.run(
        handle, Sink(), deadline=time.monotonic() + 5, cleanup_deadline=time.monotonic() + 10
    )
    assert outcome.outcome_unknown and outcome.cleanup_verified
    with pytest.raises(DockerBackendFault):
        controller.run(
            handle, Sink(), deadline=time.monotonic() + 5, cleanup_deadline=time.monotonic() + 10
        )
    assert backend.calls == ["create", "remove"]
