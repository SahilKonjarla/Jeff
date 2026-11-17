"""Internal composition with real SQL/files/output/controller; only discovery/Docker I/O fake."""

import copy
import json
import os
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import NoReturn, cast
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from jeff.application.dispatch import DispatchReply, ProjectCall, ProjectDispatcher
from jeff.application.execution import (
    ActionManifest,
    AuthorityOperation,
    AuthorityRecord,
    AuthorityResult,
    DockerBackendFault,
    DockerCLIIdentity,
    DockerCompletion,
    DockerExecutionController,
    DockerExecutionResult,
    DockerJobOutcome,
    DockerLaunchIntent,
    DockerLaunchScope,
    DockerOutputSink,
    DockerOwnedContainer,
    DockerTargetDeclaration,
    ExecutionCall,
    ExecutionProfile,
    ExecutionSettings,
    ExecutionStopSignal,
    PreparedInputFault,
    ProposalRequestScope,
    ReviewFault,
    ReviewOperation,
)
from jeff.application.execution_coordinator import ExecutionCoordinator
from jeff.application.projects import ProjectService
from jeff.application.questions import QuestionService
from jeff.application.runtime import (
    ControlFault,
    ControlObservation,
    RuntimeIdentityData,
    RuntimeLayout,
)
from jeff.application.storage import DEFAULT_SETTINGS
from jeff.domain.projects import Project
from jeff.infrastructure.docker_cli import DockerTargetInspector
from jeff.infrastructure.prepared_inputs import FilePreparedInputStore
from jeff.infrastructure.project_files import FileProjectStore
from jeff.infrastructure.runtime_dispatch import RuntimeDispatcher
from jeff.infrastructure.runtime_execution import DockerJobController
from jeff.infrastructure.runtime_output import GuardedOutputCollector
from jeff.infrastructure.runtime_settings import load_or_create_settings
from jeff.infrastructure.runtime_state import RuntimeStateStore
from jeff.infrastructure.runtime_storage import SQLiteStorage
from jeff.integrations.git import GitRepositoryRead


class Discovery:
    def find_root(self, directory: Path, *, deadline: float | None = None) -> Path:
        return directory  # Explicit disposable namespace, no Git invocation.


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


@contextmanager
def composed(
    root: Path, *, configured: bool = False, cancellable: bool = False
) -> Iterator[tuple[ExecutionCoordinator, SQLiteStorage, Project, Docker]]:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = root / "p"
    path.mkdir(mode=0o700)
    project = Project(uuid4(), path)
    files = FileProjectStore()
    files.initialize(project)
    (path / "source.py").write_text("public original\n")
    layout = RuntimeLayout(data_dir=root / "d", runtime_dir=root / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    with owner:
        owner.store.credential(owner)
        load_or_create_settings(owner)
        profile = ExecutionProfile(
            name="python",
            project_id=project.project_id,
            canonical_root=path,
            engine_id="synthetic-engine",
            endpoint="unix:///synthetic/docker.sock",
            image_id="sha256:" + "1" * 64,
            platform="linux/arm64",
            toolchain="synthetic",
            executable="/usr/local/bin/python",
        )
        settings = DEFAULT_SETTINGS.model_copy(
            update={"execution": ExecutionSettings(profiles=(profile,))}
        )
        if configured:
            import hashlib

            from jeff.application.execution import DockerBackendConfiguration
            from jeff.application.storage import ConfiguredRuntimeSettings

            binary = root / "configured-docker"
            binary.write_bytes(b"synthetic-cli")
            binary.chmod(0o500)
            config = root / "configured-cfg"
            config.mkdir(mode=0o700)
            (config / "config.json").write_text("{}")
            (config / "config.json").chmod(0o600)
            declared = DockerTargetDeclaration(
                profile=profile,
                image_environment=(("LANG", "C.UTF-8"), ("PATH", "/usr/local/bin:/usr/bin:/bin")),
                image_command=("python3",),
            )
            configured_settings = ConfiguredRuntimeSettings(
                schema_version=3,
                history=settings.history,
                execution=settings.execution,
                backend=DockerBackendConfiguration(
                    binary=DockerCLIIdentity(
                        path=binary,
                        sha256=hashlib.sha256(b"synthetic-cli").hexdigest(),
                        size=len(b"synthetic-cli"),
                    ),
                    config=config,
                    declarations=(declared,),
                ),
            )
            layout.settings.write_text(configured_settings.model_dump_json())
        else:
            layout.settings.write_text(settings.model_dump_json())  # JSON is valid fixture YAML.
        generation = uuid4()
        store = SQLiteStorage(owner, instance_id=generation)
        store.start()
        try:
            assert store.wait_ready(5).state == "ready"
            store.index(project)
            docker = Docker()
            docker.cancel_mode = cancellable
            verifier = DockerTargetInspector(_reader=docker)
            inputs = FilePreparedInputStore(owner)
            controller = DockerJobController(
                verifier, docker, inputs, store, owner_instance_id=generation
            )
            declaration = DockerTargetDeclaration(
                profile=profile,
                image_environment=(("LANG", "C.UTF-8"), ("PATH", "/usr/local/bin:/usr/bin:/bin")),
                image_command=("python3",),
            )
            coordinator = ExecutionCoordinator(
                ProjectService(Discovery(), files),
                store,
                store,
                inputs,
                inputs,
                verifier,
                controller,
                store,
                GuardedOutputCollector,
                profiles=lambda: store.execution_profiles,
                declarations=(declaration,),
                instance_id=generation,
                proposal_lookup=store,
                inspection=store,
                stop_store=store if cancellable else None,
            )
            yield coordinator, store, project, docker
        finally:
            store.close(timeout=5)


def test_cancel_during_preclaim_verification_is_reachable_before_admission_ack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jeff.application.execution import ExecutionCancelCall, ExecutionStopRecord

    with composed(tmp_path, cancellable=True) as (coordinator, store, project, docker):
        record = approved(coordinator, project)
        call = ExecutionCancelCall(
            request_id=uuid4(),
            approval_id=record.approval_id,
            reviewed_manifest_digest=record.manifest_digest,
        )
        original = docker.read
        docker.queries.clear()

        def cancel_on_first_read(
            query: str, *, endpoint: str, image_id: str | None, deadline: float
        ) -> bytes:
            data = original(query, endpoint=endpoint, image_id=image_id, deadline=deadline)
            accepted = coordinator.submit_cancel(call, deadline=time.monotonic() + 2).result(
                timeout=2
            )
            assert accepted.terminal is not None and accepted.terminal.evidence == "not_started"
            return data

        monkeypatch.setattr(docker, "read", cancel_on_first_read)
        acknowledgements: list[object] = []
        result = coordinator.start(
            project.root,
            record.approval_id,
            record.manifest_digest,
            request_id=uuid4(),
            admission_deadline=time.monotonic() + 5,
            on_admitted=acknowledgements.append,
        )
        assert isinstance(result, ExecutionStopRecord) and result.terminal is not None
        assert result.terminal.outcome == "cancelled" and result.terminal.evidence == "not_started"
        assert not acknowledgements and not docker.calls and docker.queries == ["client.version"]
        assert coordinator.shutdown_confirmed
        shown = show(store, project, record.approval_id)
        assert shown.claim_id is None and shown.state.value == "invalidated"


def test_active_cancel_uses_owning_worker_and_inspects_partial_logs(tmp_path: Path) -> None:
    from jeff.application.execution import (
        ExecutionCancelCall,
        ExecutionStopRecord,
        FreshExecutionCall,
    )

    with composed(tmp_path, cancellable=True) as (coordinator, store, project, docker):
        record = approved(coordinator, project)
        docker.entered = threading.Event()
        original_start_id = uuid4()
        with ThreadPoolExecutor(max_workers=1) as worker:
            work = worker.submit(
                coordinator.start,
                project.root,
                record.approval_id,
                record.manifest_digest,
                request_id=original_start_id,
                admission_deadline=time.monotonic() + 5,
                on_admitted=lambda _: None,
            )
            assert docker.entered.wait(2)
            assert coordinator.execution_phase(record.approval_id) == "running_observed"
            call = ExecutionCancelCall(
                request_id=uuid4(),
                approval_id=record.approval_id,
                reviewed_manifest_digest=record.manifest_digest,
            )
            with pytest.raises(ReviewFault):
                coordinator.submit_cancel(
                    call.model_copy(update={"reviewed_manifest_digest": "0" * 64}),
                    deadline=time.monotonic() + 1,
                ).result(timeout=2)
            assert coordinator.execution_phase(record.approval_id) == "running_observed"
            accepted = coordinator.submit_cancel(call, deadline=time.monotonic() + 2).result(
                timeout=3
            )
            assert accepted.request_id == call.request_id
            result = work.result(timeout=5)
        assert isinstance(result, ExecutionStopRecord) and result.terminal is not None
        assert result.terminal.outcome == "cancelled" and result.terminal.cleanup_verified
        assert docker.calls == ["create", "attach", "stop", "remove"]
        assert coordinator.execution_phase(record.approval_id) is None
        for operation in ("show", "logs"):
            reply = coordinator.execute(
                FreshExecutionCall(
                    directory=project.root,
                    request_id=uuid4(),
                    operation=operation,
                    approval_id=record.approval_id,
                ),
                time.monotonic() + 5,
            )
            assert (
                reply.ok and reply.completion_state == "stop_recorded" and reply.completion is None
            )
            assert reply.stop is not None and reply.stop.outcome == "cancelled"
            assert reply.stop.output_facts is not None and reply.stop.output_facts.aborted
            assert (reply.output is not None) == (operation == "logs")
            if reply.output is not None:
                assert reply.output.stdout == "public output\n"
            assert not {
                "nonce",
                "container_id",
                "ownership_digest",
                "attempt_id",
                "owner_instance_id",
            } & set(reply.stop.model_dump())
        assert (
            coordinator.submit_cancel(call, deadline=time.monotonic() + 2).result(timeout=3)
            == result
        )
        coordinator.start(
            project.root,
            record.approval_id,
            record.manifest_digest,
            request_id=original_start_id,
            admission_deadline=time.monotonic() + 5,
            on_admitted=lambda _: None,
        )
        assert docker.calls == ["create", "attach", "stop", "remove"]


def test_confirmed_admission_survives_reply_loss_and_replays_facts_only(tmp_path: Path) -> None:
    def exercise(root: Path, lose_reply: bool) -> None:
        with composed(root) as (coordinator, store, project, docker):
            record = approved(coordinator, project)
            starter = coordinator.start
            operation_id = uuid4()
            response: Future[object] = Future()
            docker.entered = threading.Event()
            docker.release = threading.Event()
            deadline = time.monotonic() + 0.5

            def acknowledged(value: object) -> None:
                assert show(store, project, record.approval_id).claim_state == "claimed"
                assert docker.calls == []  # Durable claim, before any container mutation.
                response.set_result(value)
                if lose_reply:
                    raise RuntimeError("synthetic lost admission observer")

            with ThreadPoolExecutor(max_workers=1) as worker:
                work = worker.submit(
                    starter,
                    project.root,
                    record.approval_id,
                    record.manifest_digest,
                    request_id=operation_id,
                    admission_deadline=deadline,
                    on_admitted=acknowledged,
                )
                admission = response.result(timeout=2)
                from jeff.application.execution import ExecutionAdmission

                assert isinstance(admission, ExecutionAdmission)
                assert (
                    admission.job_id == record.job_id
                    and not admission.replayed
                    and admission.claim_state == "claimed"
                )
                assert not {"launch_claim", "claim_id", "nonce", "container_id", "owned"} & set(
                    admission.model_dump()
                )
                for change in (
                    {"launch_claim": "SYNTHETIC_PRIVATE"},
                    {"replayed": 1},
                    {"claim_state": "finished"},
                    {"authority_kind": "review_only_v1"},
                ):
                    with pytest.raises(ValidationError) as caught:
                        ExecutionAdmission.model_validate({**admission.model_dump(), **change})
                    assert "SYNTHETIC_PRIVATE" not in str(caught.value)
                assert docker.entered.wait(2) and not work.done()
                # Expiring a caller budget must not shorten an already-admitted job.
                threading.Event().wait(max(0, deadline - time.monotonic()) + 0.02)
                docker.release.set()
                result = work.result(timeout=5)
            assert isinstance(result, DockerExecutionResult) and result.disposition == "completed"
            assert not result.outcome.outcome_unknown and result.outcome.cleanup_verified
            assert docker.calls == ["create", "attach", "remove"]
            before = (list(docker.calls), list(docker.queries))
            replays: list[ExecutionAdmission] = []
            replay = starter(
                project.root,
                record.approval_id,
                record.manifest_digest,
                request_id=operation_id,
                admission_deadline=time.monotonic() + 5,
                on_admitted=replays.append,
            )
            assert isinstance(replay, AuthorityRecord) and replay.claim_state == "finished"
            assert (
                len(replays) == 1 and replays[0].replayed and replays[0].claim_state == "finished"
            )
            assert (docker.calls, docker.queries) == before
            with pytest.raises(ReviewFault):
                starter(
                    project.root,
                    record.approval_id,
                    record.manifest_digest,
                    request_id=uuid4(),
                    admission_deadline=time.monotonic() + 5,
                    on_admitted=replays.append,
                )
            assert (docker.calls, docker.queries) == before and len(replays) == 1
            other = approved(coordinator, project)
            with pytest.raises(ReviewFault) as conflict:
                starter(
                    project.root,
                    other.approval_id,
                    other.manifest_digest,
                    request_id=operation_id,
                    admission_deadline=time.monotonic() + 5,
                    on_admitted=replays.append,
                )
            assert conflict.value.code == "execution_conflict"
            assert docker.calls == before[0] and len(replays) == 1
            assert show(store, project, other.approval_id).claim_state is None

    exercise(tmp_path / "a", False)
    exercise(tmp_path / "l", True)

    # A lost SQL claim ACK must never be replaced with a new launch permission.
    class LostClaim:
        def __init__(self, base: SQLiteStorage) -> None:
            self.base = base
            self.once = True

        def authority(self, operation: AuthorityOperation, *, deadline: float) -> AuthorityResult:
            result = self.base.authority(operation, deadline=deadline)
            if operation.operation == "claim" and self.once:
                self.once = False
                raise ReviewFault("storage_outcome_unconfirmed")
            return result

    with composed(tmp_path / "u") as (coordinator, store, project, docker):
        record = approved(coordinator, project)
        coordinator.authority = LostClaim(store)
        operation_id = uuid4()
        observations: list[object] = []
        with pytest.raises(ReviewFault):
            coordinator.start(
                project.root,
                record.approval_id,
                record.manifest_digest,
                request_id=operation_id,
                admission_deadline=time.monotonic() + 5,
                on_admitted=observations.append,
            )
        assert not observations and not docker.calls
        from jeff.application.execution import FreshExecutionCall

        inspected = coordinator.execute(
            FreshExecutionCall(
                request_id=uuid4(),
                directory=project.root,
                operation="show",
                approval_id=record.approval_id,
            ),
            time.monotonic() + 5,
        )
        assert (
            inspected.ok
            and inspected.completion_state == "claim_only"
            and inspected.completion is None
        )
        before = list(docker.queries)
        replay = coordinator.start(
            project.root,
            record.approval_id,
            record.manifest_digest,
            request_id=operation_id,
            admission_deadline=time.monotonic() + 5,
            on_admitted=observations.append,
        )
        assert isinstance(replay, AuthorityRecord) and replay.claim_state == "claimed"
        assert len(observations) == 1 and not docker.calls and docker.queries == before
        assert show(store, project, record.approval_id).claim_id is not None


def test_dispatcher_owns_admitted_job_until_completion_without_an_execution_queue(
    tmp_path: Path,
) -> None:
    from jeff.application.execution import (
        ExecutionStartCall,
        ExecutionStartReply,
        FreshExecutionCall,
    )

    with composed(tmp_path) as (coordinator, store, project, docker):
        record = approved(coordinator, project)
        assert store.instance_id is not None
        identity = RuntimeIdentityData(instance_id=store.instance_id, pid=os.getpid())
        workers = RuntimeDispatcher(
            ProjectDispatcher(
                coordinator.projects,
                QuestionService(coordinator.projects, GitRepositoryRead()),
                store,
                identity,
            ),
            starter=coordinator,
        )
        docker.entered = threading.Event()
        docker.release = threading.Event()
        workers.start()
        try:
            call = ExecutionStartCall(
                directory=project.root,
                approval_id=record.approval_id,
                reviewed_manifest_digest=record.manifest_digest,
            )
            acknowledgement = workers.submit(call, time.monotonic() + 5)
            reply = acknowledgement.result(timeout=2)
            assert (
                isinstance(reply, ExecutionStartReply)
                and reply.admission is not None
                and reply.error is None
            )
            assert reply.request_id == call.request_id and reply.identity == identity.instance_id
            with pytest.raises(ValidationError):
                ExecutionStartReply(
                    request_id=uuid4(), identity=identity.instance_id, admission=reply.admission
                )
            with pytest.raises(ValidationError):
                ExecutionStartReply(request_id=call.request_id, identity=identity.instance_id)
            with pytest.raises(ValidationError):
                ExecutionStartCall.model_validate(
                    {**call.model_dump(), "current_manifest_digest": "a" * 64}
                )
            with pytest.raises(ValidationError):
                ExecutionStartCall.model_validate(
                    {**call.model_dump(), "authority_kind": "review_only_v1"}
                )
            assert docker.entered.wait(2) and not workers.actions_drained
            # Ack completion must not release execution capacity or consume all workers.
            with pytest.raises(ControlFault) as busy:
                workers.submit(call, time.monotonic() + 5)
            assert busy.value.code == "runtime_busy"
            config = workers.submit(
                ProjectCall(directory=project.root, operation="config"), time.monotonic() + 5
            ).result(timeout=2)
            assert isinstance(config, DispatchReply) and config.result.ok
            with pytest.raises(ControlFault) as stopping:
                workers.close(timeout=0)
            assert stopping.value.code == "runtime_stop_unconfirmed" and not workers.closed
            assert not workers.actions_drained
        finally:
            docker.release.set()
            workers.close(timeout=5)
        assert workers.closed and workers.actions_drained
        assert docker.calls == ["create", "attach", "remove"]
        assert show(store, project, record.approval_id).claim_state == "finished"
        assert acknowledgement.result() == reply  # No completion/error overwrites the admission.
        disabled = RuntimeDispatcher(
            ProjectDispatcher(
                coordinator.projects,
                QuestionService(coordinator.projects, GitRepositoryRead()),
                store,
                identity,
            )
        )
        disabled.start()
        try:
            with pytest.raises(ControlFault) as unavailable:
                disabled.submit(call, time.monotonic() + 5)
            assert unavailable.value.code == "runtime_capability_unavailable"
            with pytest.raises(ControlFault) as fresh_unavailable:
                disabled.submit(
                    FreshExecutionCall(
                        request_id=uuid4(),
                        directory=project.root,
                        operation="start",
                        approval_id=record.approval_id,
                        reviewed_manifest_digest=record.manifest_digest,
                    ),
                    time.monotonic() + 5,
                )
            assert fresh_unavailable.value.code == "runtime_capability_unavailable"
        finally:
            disabled.close(timeout=5)
        assert docker.calls == ["create", "attach", "remove"]


def test_shutdown_budget_is_one_absolute_deadline_and_never_success_after_expiry(
    tmp_path: Path,
) -> None:
    from jeff.infrastructure.runtime_ipc import ControlServer

    with composed(tmp_path) as (_, store, _, docker):
        assert store.instance_id is not None
        clock = [100.0]
        server = ControlServer(
            store.owner,
            store.owner.store.read_credential(),
            RuntimeIdentityData(instance_id=store.instance_id, pid=os.getpid()),
            store.snapshot,
            monotonic=lambda: clock[0],
        )
        server.request_stop()
        assert server.stop_deadline == 135.0
        clock[0] = 120.0
        server.request_stop()
        assert server.stop_deadline == 135.0
        server.check_stop_budget()
        clock[0] = 135.0
        with pytest.raises(ControlFault) as expired:
            server.check_stop_budget()
        assert expired.value.code == "runtime_stop_unconfirmed"
        assert not docker.calls


@pytest.mark.parametrize("cleanup_confirmed", (True, False))
def test_authenticated_stop_cancels_active_work_without_observer_cancellation(
    tmp_path: Path, cleanup_confirmed: bool
) -> None:
    import asyncio

    from jeff.application.execution import ExecutionInspectionScope, FreshExecutionCall
    from jeff.domain.runtime import RuntimeState
    from jeff.infrastructure.runtime_ipc import ControlClient, ControlServer

    with composed(tmp_path, cancellable=True) as (coordinator, store, project, docker):
        record = approved(coordinator, project)
        assert store.instance_id is not None
        identity = RuntimeIdentityData(instance_id=store.instance_id, pid=os.getpid())
        dispatcher = RuntimeDispatcher(
            ProjectDispatcher(
                coordinator.projects,
                QuestionService(coordinator.projects, GitRepositoryRead()),
                store,
                identity,
            ),
            fresh=coordinator,
        )
        server = ControlServer(
            store.owner, store.owner.store.read_credential(), identity, store.snapshot
        )
        server.dispatcher = dispatcher
        docker.entered = threading.Event()
        dispatcher.start()

        async def exercise() -> None:
            task = asyncio.create_task(server.serve())
            try:
                for _ in range(200):
                    if task.done():
                        await task
                    if store.owner.store.layout.socket.exists():
                        break
                    await asyncio.sleep(0.005)
                ack = dispatcher.submit(
                    FreshExecutionCall(
                        directory=project.root,
                        request_id=uuid4(),
                        operation="start",
                        approval_id=record.approval_id,
                        reviewed_manifest_digest=record.manifest_digest,
                    ),
                    time.monotonic() + 5,
                )
                await asyncio.to_thread(ack.result, 2)
                assert docker.entered is not None
                assert await asyncio.to_thread(docker.entered.wait, 2)

                def stop() -> ControlObservation:
                    with ControlClient(
                        store.owner.store.layout, store.owner.store.read_credential()
                    ).connect(time.monotonic() + 3) as connection:
                        return connection.request("stop", identity.instance_id)

                if cleanup_confirmed:
                    observation = await asyncio.to_thread(stop)
                    assert observation.state == RuntimeState.STOPPING
                else:
                    docker.failure = "cleanup"
                    with pytest.raises(ControlFault) as unconfirmed:
                        await asyncio.to_thread(stop)
                    assert unconfirmed.value.code == "runtime_stop_unconfirmed"
                await asyncio.wait_for(task, 5)
                assert dispatcher.actions_drained and dispatcher.closed
            finally:
                server.request_stop()
                dispatcher.request_close()
                try:
                    await asyncio.wait_for(task, 5)
                finally:
                    dispatcher.close(timeout=5)

        previous_umask = os.umask(0o177)
        try:
            asyncio.run(exercise())
        finally:
            os.umask(previous_umask)
        final = store.inspect_stop(
            ExecutionInspectionScope(
                project_id=project.project_id,
                canonical_root=project.root,
                owner_instance_id=identity.instance_id,
                approval_id=record.approval_id,
            ),
            deadline=time.monotonic() + 3,
        )
        assert final is not None and final.reason == "shutdown" and final.terminal is not None
        assert final.terminal.outcome == ("cancelled" if cleanup_confirmed else "unknown")
        assert final.terminal.cleanup_verified == cleanup_confirmed
        assert final.terminal.output_facts is not None and final.terminal.output_facts.aborted
        assert docker.calls == (
            ["create", "attach", "stop", "remove"] if cleanup_confirmed else ["create", "attach"]
        )


def test_authenticated_cancel_bypasses_full_ordinary_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from jeff.application.dispatch import dispatch_failure
    from jeff.application.execution import ExecutionCancelCall, FreshExecutionCall
    from jeff.application.runtime import DEFAULT_RUNTIME_LIMITS
    from jeff.infrastructure.runtime_ipc import ControlServer, RuntimeProjectClient

    with composed(tmp_path, cancellable=True) as (coordinator, store, project, docker):
        record = approved(coordinator, project)
        assert store.instance_id is not None
        identity = RuntimeIdentityData(instance_id=store.instance_id, pid=os.getpid())
        dispatcher = RuntimeDispatcher(
            ProjectDispatcher(
                coordinator.projects,
                QuestionService(coordinator.projects, GitRepositoryRead()),
                store,
                identity,
            ),
            fresh=coordinator,
        )
        server = ControlServer(
            store.owner, store.owner.store.read_credential(), identity, store.snapshot
        )
        server.dispatcher = dispatcher
        client = RuntimeProjectClient(store.owner.store.layout)
        docker.entered = threading.Event()
        release = threading.Event()
        entered = [0]
        guard = threading.Lock()

        def slow(call: ProjectCall, deadline: float) -> DispatchReply:
            with guard:
                entered[0] += 1
            assert release.wait(8)
            return DispatchReply(
                request_id=call.request_id,
                identity=identity,
                result=dispatch_failure("runtime_busy"),
            )

        monkeypatch.setattr(dispatcher.dispatch, "execute", slow)
        dispatcher.start()

        async def exercise() -> None:
            task = asyncio.create_task(server.serve())
            try:
                for _ in range(200):
                    if store.owner.store.layout.socket.exists():
                        break
                    if task.done():
                        await task
                    await asyncio.sleep(0.005)
                started = dispatcher.submit(
                    FreshExecutionCall(
                        request_id=uuid4(),
                        directory=project.root,
                        operation="start",
                        approval_id=record.approval_id,
                        reviewed_manifest_digest=record.manifest_digest,
                    ),
                    time.monotonic() + 5,
                )
                await asyncio.to_thread(started.result, 2)
                assert docker.entered is not None
                assert await asyncio.to_thread(docker.entered.wait, 2)
                for _ in range(3):
                    dispatcher.submit(
                        ProjectCall(request_id=uuid4(), directory=project.root, operation="config"),
                        time.monotonic() + 7,
                    )
                for _ in range(200):
                    with guard:
                        if entered[0] == 3:
                            break
                    await asyncio.sleep(0.005)
                assert entered[0] == 3
                for _ in range(DEFAULT_RUNTIME_LIMITS.pending_requests):
                    dispatcher.submit(
                        ProjectCall(request_id=uuid4(), directory=project.root, operation="config"),
                        time.monotonic() + 7,
                    )
                with pytest.raises(ControlFault):
                    dispatcher.submit(
                        ProjectCall(request_id=uuid4(), directory=project.root, operation="config"),
                        time.monotonic() + 7,
                    )
                call = ExecutionCancelCall(
                    request_id=uuid4(),
                    approval_id=record.approval_id,
                    reviewed_manifest_digest=record.manifest_digest,
                )
                reply = await asyncio.wait_for(asyncio.to_thread(client.cancel, call), 3)
                assert reply.ok and reply.stop is not None and reply.stop.reason == "cancel"
                assert (
                    reply.request_id == call.request_id and reply.identity == identity.instance_id
                )
                assert docker.calls[:2] == ["create", "attach"]
            finally:
                release.set()
                server.request_stop()
                dispatcher.request_close()
                try:
                    await asyncio.wait_for(task, 5)
                finally:
                    # Even a listener/assertion failure must close idle non-daemon workers.
                    dispatcher.close(timeout=5)

        previous_umask = os.umask(0o177)
        try:
            asyncio.run(exercise())
        finally:
            os.umask(previous_umask)
        assert docker.calls == ["create", "attach", "stop", "remove"]


def test_fresh_authenticated_rpc_keeps_owned_job_after_caller_disconnect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from typer.testing import CliRunner

    from jeff.application.dispatch import FreshExecutionRequest
    from jeff.application.execution import FreshExecutionCall, FreshExecutionReply
    from jeff.application.runtime import DEFAULT_RUNTIME_LIMITS
    from jeff.infrastructure.runtime_ipc import (
        ControlClient,
        ControlServer,
        RuntimeProjectClient,
        encode_message,
    )
    from jeff.interfaces.cli import create_app

    with composed(tmp_path) as (coordinator, store, project, docker):
        assert store.instance_id is not None
        identity = RuntimeIdentityData(instance_id=store.instance_id, pid=os.getpid())
        workers = RuntimeDispatcher(
            ProjectDispatcher(
                coordinator.projects,
                QuestionService(coordinator.projects, GitRepositoryRead()),
                store,
                identity,
            ),
            fresh=coordinator,
        )
        server = ControlServer(
            store.owner, store.owner.store.read_credential(), identity, store.snapshot
        )
        server.dispatcher = workers
        client = RuntimeProjectClient(store.owner.store.layout)
        monkeypatch.chdir(project.root)

        def forbidden() -> NoReturn:
            raise AssertionError("unrelated CLI factory invoked")

        app = create_app(forbidden, forbidden, fresh_execution=lambda: client)
        runner = CliRunner()
        docker.entered = threading.Event()
        docker.release = threading.Event()
        workers.start()

        async def exercise() -> None:
            assert docker.entered is not None and docker.release is not None
            task = asyncio.create_task(server.serve())
            try:
                for _ in range(200):
                    if store.owner.store.layout.socket.exists():
                        break
                    if task.done():
                        await task
                    await asyncio.sleep(0.01)

                def invoke(operation: str, **fields: object) -> FreshExecutionReply:
                    arguments = ["execution", operation]
                    if "approval_id" in fields:
                        arguments.append(str(fields["approval_id"]))
                    if "reviewed_manifest_digest" in fields:
                        arguments.extend(["--digest", str(fields["reviewed_manifest_digest"])])
                    if "profile_name" in fields:
                        arguments.extend(["--profile", str(fields["profile_name"])])
                    for path in cast(tuple[str, ...], fields.get("paths", ())):
                        arguments.extend(["--input", path])
                    arguments.append("--json")
                    if "argv" in fields:
                        arguments.extend(["--", *cast(tuple[str, ...], fields["argv"])])
                    response = runner.invoke(app, arguments)
                    assert response.exit_code in (0, 1), response.output
                    return FreshExecutionReply.model_validate_json(response.stdout)

                proposal = await asyncio.to_thread(
                    invoke,
                    "propose",
                    profile_name="python",
                    argv=("/usr/local/bin/python", "-V"),
                    paths=("source.py",),
                )
                assert proposal.ok and proposal.view is not None
                target = {
                    "approval_id": proposal.view.approval_id,
                    "reviewed_manifest_digest": proposal.view.manifest_digest,
                }
                assert (await asyncio.to_thread(invoke, "approve", **target)).ok
                ack = await asyncio.to_thread(invoke, "start", **target)
                assert (
                    ack.ok and ack.admission is not None and ack.admission.claim_state == "claimed"
                )
                # Unary client has closed its authenticated connection; worker still owns the job.
                assert await asyncio.to_thread(docker.entered.wait, 2)
                assert not workers.actions_drained
                busy = await asyncio.to_thread(invoke, "start", **target)
                assert not busy.ok
                during = await asyncio.to_thread(
                    invoke, "show", approval_id=proposal.view.approval_id
                )
                assert (
                    during.ok and during.view is not None and during.view.claim_state == "claimed"
                )
                assert (
                    during.completion is None and during.completion_state == "launch_without_result"
                )
                docker.release.set()
                for _ in range(200):
                    if workers.actions_drained:
                        break
                    await asyncio.sleep(0.01)
                result = await asyncio.to_thread(
                    invoke, "logs", approval_id=proposal.view.approval_id
                )
                assert (
                    result.ok and result.view is not None and result.view.claim_state == "finished"
                )
                assert result.output is not None and result.output.stdout == "public output\n"
                # A second synthetic job loses its caller before acknowledgement delivery.
                second = await asyncio.to_thread(
                    invoke,
                    "propose",
                    profile_name="python",
                    argv=("/usr/local/bin/python", "-V"),
                    paths=("source.py",),
                )
                assert second.view is not None
                second_target = {
                    "approval_id": second.view.approval_id,
                    "reviewed_manifest_digest": second.view.manifest_digest,
                }
                assert (await asyncio.to_thread(invoke, "approve", **second_target)).ok
                docker.entered.clear()
                docker.release.clear()
                lost_call = FreshExecutionCall(
                    request_id=uuid4(),
                    directory=project.root,
                    operation="start",
                    approval_id=second.view.approval_id,
                    reviewed_manifest_digest=second.view.manifest_digest,
                )

                def send_then_disconnect() -> None:
                    with ControlClient(
                        store.owner.store.layout, store.owner.store.read_credential()
                    ).connect(time.monotonic() + 5) as connection:
                        frame = FreshExecutionRequest(
                            call=lost_call,
                            expected_instance_id=connection.identity.instance_id,
                            remaining_ms=5000,
                        )
                        connection.connection.sendall(
                            encode_message(
                                frame.model_dump(mode="json"), DEFAULT_RUNTIME_LIMITS.request_bytes
                            )
                        )

                await asyncio.to_thread(send_then_disconnect)
                assert await asyncio.to_thread(docker.entered.wait, 2)
                assert not workers.actions_drained
                docker.release.set()
                for _ in range(200):
                    if workers.actions_drained:
                        break
                    await asyncio.sleep(0.01)
                recovered = await asyncio.to_thread(
                    invoke, "show", approval_id=second.view.approval_id
                )
                assert (
                    recovered.ok
                    and recovered.view is not None
                    and recovered.view.claim_state == "finished"
                )
                duplicate = await asyncio.to_thread(client.fresh, lost_call)
                assert duplicate.admission is not None and duplicate.admission.replayed
                assert duplicate.admission.claim_state == "finished"
            finally:
                docker.release.set()
                server.request_stop()
                await asyncio.wait_for(task, 5)

        previous_umask = os.umask(0o177)
        try:
            asyncio.run(exercise())
        finally:
            try:
                docker.release.set()
                workers.close(timeout=5)
            finally:
                os.umask(previous_umask)
        assert docker.calls == ["create", "attach", "remove"] * 2 and workers.actions_drained


def test_proposal_preparation_preserves_private_modes_under_daemon_umask(tmp_path: Path) -> None:
    for index, mask in enumerate((0o077, 0o177)):
        with composed(tmp_path / str(index)) as (coordinator, store, project, docker):
            (project.root / "nested").mkdir(mode=0o700)
            (project.root / "nested" / "part.py").write_text("nested public\n")
            old = os.umask(mask)
            try:
                proposal = coordinator.propose(
                    project.root,
                    "python",
                    ("/usr/local/bin/python", "-V"),
                    ("source.py", "nested/part.py"),
                    request_id=uuid4(),
                    deadline=time.monotonic() + 5,
                )
                assert proposal.state.value == "pending" and not docker.calls
                jobs = store.owner.store.layout.data_dir / "jobs"
                snapshot = jobs / str(proposal.manifest.inputs.snapshot_id)
                for path, mode in (
                    (jobs, 0o700),
                    (snapshot, 0o700),
                    (snapshot / "input", 0o555),
                    (snapshot / "input" / "nested", 0o555),
                    (snapshot / "input" / "source.py", 0o444),
                    (snapshot / "input" / "nested" / "part.py", 0o444),
                    (snapshot / "prepared.json", 0o600),
                    (jobs / ".prepare.lock", 0o600),
                ):
                    assert path.stat().st_mode & 0o777 == mode
                os.chmod(jobs, 0o600)
                try:
                    with pytest.raises(PreparedInputFault) as unsafe:
                        coordinator.propose(
                            project.root,
                            "python",
                            ("/usr/local/bin/python", "-V"),
                            ("source.py",),
                            request_id=uuid4(),
                            deadline=time.monotonic() + 5,
                        )
                    assert (
                        unsafe.value.code == "input_unsafe" and jobs.stat().st_mode & 0o777 == 0o600
                    )
                finally:
                    os.chmod(jobs, 0o700)
            finally:
                os.umask(old)


def test_fresh_public_service_reviews_exact_action_and_projects_guarded_result(
    tmp_path: Path,
) -> None:
    from jeff.application.execution import ExecutionAdmission, FreshExecutionCall

    with composed(tmp_path) as (coordinator, store, project, docker):

        def call(operation: str, **values: object) -> FreshExecutionCall:
            return FreshExecutionCall.model_validate(
                {"request_id": uuid4(), "directory": project.root, "operation": operation, **values}
            )

        proposed = coordinator.execute(
            call(
                "propose",
                profile_name="python",
                argv=("/usr/local/bin/python", "-V"),
                paths=("source.py",),
            ),
            time.monotonic() + 5,
        )
        assert proposed.ok and proposed.view is not None
        target = {
            "approval_id": proposed.view.approval_id,
            "reviewed_manifest_digest": proposed.view.manifest_digest,
        }
        wrong = coordinator.execute(
            call("approve", **{**target, "reviewed_manifest_digest": "0" * 64}),
            time.monotonic() + 5,
        )
        assert not wrong.ok and not docker.calls
        decision = call("approve", **target)
        approved = coordinator.execute(decision, time.monotonic() + 5)
        assert approved.ok and approved.view is not None and approved.view.state.value == "approved"
        assert coordinator.execute(decision, time.monotonic() + 5).ok
        observations: list[ExecutionAdmission] = []
        coordinator.start(
            project.root,
            proposed.view.approval_id,
            proposed.view.manifest_digest,
            request_id=uuid4(),
            admission_deadline=time.monotonic() + 5,
            on_admitted=observations.append,
        )
        assert len(observations) == 1
        show = coordinator.execute(
            call("show", approval_id=proposed.view.approval_id), time.monotonic() + 5
        )
        logs = coordinator.execute(
            call("logs", approval_id=proposed.view.approval_id), time.monotonic() + 5
        )
        assert show.ok and show.view is not None and show.view.claim_state == "finished"
        assert (
            show.completion is not None
            and show.completion.outcome.exit_code == 0
            and show.output is None
        )
        assert logs.ok and logs.output is not None and logs.output.stdout == "public output\n"
        assert logs.completion is not None and logs.completion.payload_state == "retained"
        for reply in (proposed, approved, show, logs):
            payload = reply.model_dump_json()
            assert all(
                '"' + key + '"' not in payload
                for key in (
                    "claim_id",
                    "launch_claim",
                    "owned",
                    "nonce",
                    "container_id",
                    "owner_instance_id",
                )
            )
        denied_proposal = coordinator.execute(
            call("propose", profile_name="python", argv=("/usr/local/bin/python", "-V")),
            time.monotonic() + 5,
        )
        assert denied_proposal.view is not None
        deny = call(
            "deny",
            approval_id=denied_proposal.view.approval_id,
            reviewed_manifest_digest=denied_proposal.view.manifest_digest,
        )
        assert coordinator.execute(deny, time.monotonic() + 5).ok
        assert coordinator.execute(deny, time.monotonic() + 5).ok
        altered = deny.model_copy(update={"reviewed_manifest_digest": "0" * 64})
        assert not coordinator.execute(altered, time.monotonic() + 5).ok


def test_proposal_request_replay_recovers_original_copy_before_preparing_again(
    tmp_path: Path,
) -> None:
    with composed(tmp_path) as (coordinator, store, project, docker):
        (project.root / "second.py").write_text("second public\n")
        operation_id = uuid4()
        argv = ("/usr/local/bin/python", "-V")
        record = coordinator.propose(
            project.root,
            "python",
            argv,
            ("source.py", "second.py"),
            request_id=operation_id,
            deadline=time.monotonic() + 5,
        )
        jobs = store.owner.store.layout.data_dir / "jobs"
        before = (set(jobs.iterdir()), list(docker.queries))
        (project.root / "source.py").write_text("later source\n")
        replay = coordinator.propose(
            project.root,
            "python",
            argv,
            ("second.py", "source.py"),
            request_id=operation_id,
            deadline=time.monotonic() + 5,
        )
        assert replay.approval_id == record.approval_id and replay.manifest == record.manifest
        assert (set(jobs.iterdir()), docker.queries) == before and not docker.calls
        for changed_argv, changed_paths in (
            (("/usr/local/bin/python", "-c", "print(1)"), ("source.py", "second.py")),
            (argv, ("missing.py",)),
        ):
            with pytest.raises(ReviewFault) as conflict:
                coordinator.propose(
                    project.root,
                    "python",
                    changed_argv,
                    changed_paths,
                    request_id=operation_id,
                    deadline=time.monotonic() + 5,
                )
            assert conflict.value.code == "execution_conflict"
            assert (set(jobs.iterdir()), docker.queries) == before

        class LostProposal:
            def __init__(self, base: SQLiteStorage) -> None:
                self.base = base
                self.once = True

            def authority(
                self, operation: AuthorityOperation, *, deadline: float
            ) -> AuthorityResult:
                result = self.base.authority(operation, deadline=deadline)
                if operation.operation == "propose" and self.once:
                    self.once = False
                    raise ReviewFault("storage_outcome_unconfirmed")
                return result

        coordinator.authority = LostProposal(store)
        lost_id = uuid4()
        with pytest.raises(ReviewFault):
            coordinator.propose(
                project.root,
                "python",
                argv,
                ("source.py",),
                request_id=lost_id,
                deadline=time.monotonic() + 5,
            )
        after_lost = (set(jobs.iterdir()), list(docker.queries))
        recovered = coordinator.propose(
            project.root,
            "python",
            argv,
            ("source.py",),
            request_id=lost_id,
            deadline=time.monotonic() + 5,
        )
        assert recovered.manifest.inputs.snapshot_id != record.manifest.inputs.snapshot_id
        assert (set(jobs.iterdir()), docker.queries) == after_lost and not docker.calls
        assert recovered.manifest.inputs.files[0].sha256 != record.manifest.inputs.files[-1].sha256

        # An approve operation UUID is not a proposal request, even in the same scope.
        assert store.instance_id is not None
        decision_id = uuid4()
        store.authority(
            AuthorityOperation(
                operation="approve",
                request_id=decision_id,
                project_id=project.project_id,
                canonical_root=project.root,
                owner_instance_id=store.instance_id,
                approval_id=recovered.approval_id,
                current_manifest_digest=recovered.manifest_digest,
            ),
            deadline=time.monotonic() + 5,
        )
        with pytest.raises(ReviewFault) as wrong_operation:
            coordinator.propose(
                project.root,
                "python",
                argv,
                ("source.py",),
                request_id=decision_id,
                deadline=time.monotonic() + 5,
            )
        assert wrong_operation.value.code == "execution_conflict"
        assert (set(jobs.iterdir()), docker.queries) == after_lost and not docker.calls
        scope = ProposalRequestScope(
            request_id=operation_id,
            project_id=project.project_id,
            canonical_root=project.root,
            owner_instance_id=store.instance_id,
        )
        assert store.lookup_proposal(scope, deadline=time.monotonic() + 5) == record
        for change in (
            {"project_id": uuid4()},
            {"canonical_root": project.root / "elsewhere"},
            {"owner_instance_id": uuid4()},
        ):
            with pytest.raises(ReviewFault):
                store.lookup_proposal(
                    ProposalRequestScope.model_validate({**scope.model_dump(), **change}),
                    deadline=time.monotonic() + 5,
                )
        for paths in (
            ("source.py", "SOURCE.py"),
            ("file", "file/child"),
            ("Case/a", "case/b"),
            ("é/a", "e\u0301/b"),
            (".env",),
        ):
            with pytest.raises((PreparedInputFault, DockerBackendFault)):
                coordinator.propose(
                    project.root,
                    "python",
                    argv,
                    paths,
                    request_id=operation_id,
                    deadline=time.monotonic() + 5,
                )
        assert (set(jobs.iterdir()), docker.queries) == after_lost
        legacy_id = uuid4()
        profile = store.execution_profiles[0]
        store.review(
            ReviewOperation(
                call=ExecutionCall(
                    operation="propose",
                    request_id=legacy_id,
                    directory=project.root,
                    profile_name="python",
                    argv=argv,
                ),
                project_id=project.project_id,
                canonical_root=project.root,
                owner_instance_id=store.instance_id,
                manifest=ActionManifest(profile=profile, argv=argv),
                configuration_fingerprint=profile.fingerprint,
            ),
            deadline=time.monotonic() + 5,
        )
        with pytest.raises(ReviewFault) as legacy_conflict:
            coordinator.propose(
                project.root,
                "python",
                argv,
                ("source.py",),
                request_id=legacy_id,
                deadline=time.monotonic() + 5,
            )
        assert legacy_conflict.value.code == "execution_conflict"
        assert (set(jobs.iterdir()), docker.queries) == after_lost and not docker.calls


def test_composed_fresh_flow_keeps_reviewed_copy_and_finishes_known_outcomes(
    tmp_path: Path,
) -> None:
    with composed(tmp_path) as (coordinator, store, project, docker):
        for code in (0, 7):
            before = list(docker.calls)
            proposal = coordinator.propose(
                project.root,
                "python",
                ("/usr/local/bin/python", "-V"),
                ("source.py",),
                deadline=time.monotonic() + 5,
            )
            assert docker.calls == before and proposal.state == "pending"
            approved = coordinator.decide(
                project.root,
                proposal.approval_id,
                proposal.manifest_digest,
                approve=True,
                deadline=time.monotonic() + 5,
            )
            assert docker.calls == before and approved.state == "approved"
            (project.root / "source.py").write_text("later source\n")
            docker.exit_code = code
            if code == 7:
                docker.output = b"password=SYNTHETIC_VALUE\n"
            result = coordinator.run(
                project.root,
                proposal.approval_id,
                proposal.manifest_digest,
                deadline=time.monotonic() + 5,
            )
            assert result.disposition == "completed" and result.outcome.exit_code == code
            assert result.output is not None
            if code == 0:
                assert result.output.stdout == "public output\n"
            else:
                assert (
                    result.output.guard_reason == "sensitive_channel"
                    and "SYNTHETIC_VALUE" not in result.output.stdout
                )
            assert docker.copied[-1] == (b"public original\n" if code == 0 else b"later source\n")
            assert result.owned.intent.record.claim_id is not None
            scope = DockerLaunchScope(
                project_id=project.project_id,
                canonical_root=project.root,
                claim_id=result.owned.intent.record.claim_id,
            )
            assert (
                store.launch_status(scope, deadline=time.monotonic() + 5).claim_state == "finished"
            )
        assert docker.calls == ["create", "attach", "remove"] * 2


def pending(coordinator: ExecutionCoordinator, project: Project) -> AuthorityRecord:
    return coordinator.propose(
        project.root,
        "python",
        ("/usr/local/bin/python", "-V"),
        ("source.py",),
        deadline=time.monotonic() + 5,
    )


def approved(coordinator: ExecutionCoordinator, project: Project) -> AuthorityRecord:
    record = pending(coordinator, project)
    return coordinator.decide(
        project.root,
        record.approval_id,
        record.manifest_digest,
        approve=True,
        deadline=time.monotonic() + 5,
    )


def show(store: SQLiteStorage, project: Project, approval_id: UUID) -> AuthorityRecord:
    assert store.instance_id is not None
    return store.authority(
        AuthorityOperation(
            operation="show",
            project_id=project.project_id,
            canonical_root=project.root,
            owner_instance_id=store.instance_id,
            approval_id=approval_id,
        ),
        deadline=time.monotonic() + 5,
    ).record


def test_composition_refuses_unregistered_scope_legacy_and_implicit_approval(
    tmp_path: Path,
) -> None:
    with composed(tmp_path) as (coordinator, store, project, docker):
        unregistered = tmp_path / "u"
        unregistered.mkdir(mode=0o700)
        FileProjectStore().initialize(Project(uuid4(), unregistered))
        with pytest.raises(DockerBackendFault):
            pending(coordinator, Project(uuid4(), unregistered))
        assert not docker.queries and not (store.owner.store.layout.data_dir / "jobs").exists()
        record = pending(coordinator, project)
        with pytest.raises(DockerBackendFault):
            coordinator.run(
                project.root,
                record.approval_id,
                record.manifest_digest,
                deadline=time.monotonic() + 5,
            )
        with pytest.raises(DockerBackendFault):
            coordinator.decide(
                project.root,
                record.approval_id,
                "0" * 64,
                approve=True,
                deadline=time.monotonic() + 5,
            )
        profile = store.execution_profiles[0]
        legacy = store.review(
            ReviewOperation(
                call=ExecutionCall(
                    operation="propose",
                    directory=project.root,
                    profile_name="python",
                    argv=(profile.executable, "-V"),
                ),
                project_id=project.project_id,
                canonical_root=project.root,
                owner_instance_id=coordinator.instance_id,
                manifest=ActionManifest(profile=profile, argv=(profile.executable, "-V")),
                configuration_fingerprint=profile.fingerprint,
            ),
            deadline=time.monotonic() + 5,
        ).items[0]
        with pytest.raises(ReviewFault):
            coordinator.decide(
                project.root,
                legacy.approval_id,
                legacy.manifest.fingerprint,
                approve=True,
                deadline=time.monotonic() + 5,
            )
        denied = coordinator.decide(
            project.root,
            record.approval_id,
            record.manifest_digest,
            approve=False,
            deadline=time.monotonic() + 5,
        )
        assert denied.state == "denied" and not docker.calls


def test_composition_rechecks_prepared_bytes_private_profile_and_target_before_claim(
    tmp_path: Path,
) -> None:
    for index, kind in enumerate(("snapshot", "profile", "target")):
        with composed(tmp_path / str(index)) as (coordinator, store, project, docker):
            record = approved(coordinator, project)
            if kind == "snapshot":
                copied = (
                    store.owner.store.layout.data_dir
                    / "jobs"
                    / str(record.manifest.inputs.snapshot_id)
                    / "input"
                    / "source.py"
                )
                copied.chmod(0o600)
                copied.write_text("changed prepared bytes\n")
            elif kind == "profile":
                profiles = (record.manifest.profile.model_copy(update={"output_bytes": 64}),)

                def changed_profiles(
                    current: tuple[ExecutionProfile, ...] = profiles,
                ) -> tuple[ExecutionProfile, ...]:
                    return current

                coordinator.profiles = changed_profiles
            else:
                docker.engine["ID"] = "changed-engine"
            with pytest.raises((DockerBackendFault, PreparedInputFault)):
                coordinator.run(
                    project.root,
                    record.approval_id,
                    record.manifest_digest,
                    deadline=time.monotonic() + 5,
                )
            observed = show(store, project, record.approval_id)
            assert observed.state == "approved" and observed.claim_id is None and not docker.calls


def test_composition_claim_or_create_uncertainty_never_replays_or_invents_an_intent(
    tmp_path: Path,
) -> None:
    class Claims:
        def __init__(self, base: SQLiteStorage, mode: str, docker: Docker) -> None:
            self.base, self.mode, self.docker = base, mode, docker

        def authority(self, operation: AuthorityOperation, *, deadline: float) -> AuthorityResult:
            result = self.base.authority(operation, deadline=deadline)
            if operation.operation == "claim":
                if self.mode == "lost_claim":
                    raise ReviewFault("storage_outcome_unconfirmed")
                if self.mode == "pre_intent":
                    self.docker.engine["ID"] = "changed-after-claim"
            return result

    class BeforeRun:
        def __init__(self, base: DockerExecutionController) -> None:
            self.create = base.create
            self.complete = base.complete
            self.terminate_remove_owned = base.terminate_remove_owned

        def run(
            self,
            handle: DockerOwnedContainer,
            sink: DockerOutputSink,
            *,
            deadline: float,
            cleanup_deadline: float,
        ) -> DockerJobOutcome:
            raise DockerBackendFault("docker_timeout")

    for index, kind in enumerate(("lost_claim", "pre_intent", "create", "pre_run")):
        with composed(tmp_path / str(index)) as (coordinator, store, project, docker):
            record = approved(coordinator, project)
            coordinator.authority = Claims(store, kind, docker)
            if kind == "create":
                docker.failure = "create"
            if kind == "pre_run":
                coordinator.controller = BeforeRun(coordinator.controller)
            with pytest.raises((DockerBackendFault, ReviewFault)):
                coordinator.run(
                    project.root,
                    record.approval_id,
                    record.manifest_digest,
                    deadline=time.monotonic() + 5,
                )
            with pytest.raises(DockerBackendFault):
                coordinator.run(
                    project.root,
                    record.approval_id,
                    record.manifest_digest,
                    deadline=time.monotonic() + 5,
                )
            observed = show(store, project, record.approval_id)
            assert observed.claim_state == ("unknown" if kind == "create" else "claimed")
            assert observed.claim_id is not None
            assert docker.calls == (
                ["create"]
                if kind == "create"
                else ["create", "remove"]
                if kind == "pre_run"
                else []
            )
            scope = DockerLaunchScope(
                project_id=project.project_id,
                canonical_root=project.root,
                claim_id=observed.claim_id,
            )
            if kind == "create":
                facts = store.launch_status(scope, deadline=time.monotonic() + 5)
                assert facts.phases == ("create_intent", "unknown") and facts.container_id is None
            elif kind == "pre_run":
                assert store.launch_status(scope, deadline=time.monotonic() + 5).phases == (
                    "create_intent",
                    "created",
                    "cleaned",
                )
            else:
                with pytest.raises(DockerBackendFault):
                    store.launch_status(scope, deadline=time.monotonic() + 5)


def test_composition_keeps_timeout_eof_and_completion_ack_uncertainty_distinct(
    tmp_path: Path,
) -> None:
    for index, kind in enumerate(("timeout", "after_eof", "lost_completion")):
        with composed(tmp_path / str(index)) as (coordinator, store, project, docker):
            record = approved(coordinator, project)
            if kind == "lost_completion":

                class Results:
                    def complete_execution(
                        self, completion: DockerCompletion, *, deadline: float
                    ) -> DockerExecutionResult:
                        store.complete_execution(completion, deadline=deadline)
                        raise DockerBackendFault("docker_journal_unconfirmed")

                coordinator.results = Results()
                with pytest.raises(DockerBackendFault):
                    coordinator.run(
                        project.root,
                        record.approval_id,
                        record.manifest_digest,
                        deadline=time.monotonic() + 5,
                    )
            else:
                docker.failure = kind
                result = coordinator.run(
                    project.root,
                    record.approval_id,
                    record.manifest_digest,
                    deadline=time.monotonic() + 5,
                )
                assert result.disposition == "quarantined" and result.output is not None
                assert result.output.aborted == (kind == "timeout")
            observed = show(store, project, record.approval_id)
            assert observed.claim_id is not None
            scope = DockerLaunchScope(
                project_id=project.project_id,
                canonical_root=project.root,
                claim_id=observed.claim_id,
            )
            facts = store.execution_result(scope, deadline=time.monotonic() + 5)
            assert facts.disposition == (
                "completed" if kind == "lost_completion" else "quarantined"
            )
            assert observed.claim_state == ("finished" if kind == "lost_completion" else "unknown")
            before = list(docker.calls)
            with pytest.raises(DockerBackendFault):
                coordinator.run(
                    project.root,
                    record.approval_id,
                    record.manifest_digest,
                    deadline=time.monotonic() + 5,
                )
            assert (
                docker.calls == before
                and docker.calls.count("create")
                == docker.calls.count("attach")
                == docker.calls.count("remove")
                == 1
            )


def test_bootstrap_optin_publishes_capability_without_any_startup_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from jeff.application.execution import FreshExecutionCall
    from jeff.bootstrap.runtime import _serve
    from jeff.infrastructure.runtime_ipc import ControlClient, RuntimeProjectClient

    def no_process(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("startup must not run a child")

    monkeypatch.setattr("jeff.infrastructure.docker_cli.subprocess.Popen", no_process)

    def scenario_case(scenario: str) -> None:
        with composed(tmp_path / scenario, configured=scenario != "disabled") as (
            coordinator,
            store,
            project,
            docker,
        ):
            if scenario == "mismatch":
                configuration = store.execution_backend
                assert configuration is not None
                configuration.binary.path.chmod(0o700)
                configuration.binary.path.write_bytes(b"different-cli")
                configuration.binary.path.chmod(0o500)
            if scenario == "capacity":
                namespace = store.owner.store.layout.runtime_dir / "execution-backend"
                namespace.mkdir(mode=0o700)
                for _ in range(16):
                    (namespace / str(uuid4())).mkdir(mode=0o700)
            store.close(timeout=5)  # One SQL owner at a time, including in this source fixture.
            owner = store.owner
            token = owner.store.read_credential()
            identity = RuntimeIdentityData(instance_id=uuid4(), pid=os.getpid())
            reader, writer = os.pipe()

            def observe(stop: bool = False) -> ControlObservation:
                with ControlClient(owner.store.layout, token).connect(
                    time.monotonic() + 5
                ) as connection:
                    return connection.request("stop" if stop else "status", identity.instance_id)

            async def exercise() -> None:
                task = asyncio.create_task(_serve(owner, token, identity, writer))
                try:
                    hint = await asyncio.wait_for(asyncio.to_thread(os.read, reader, 512), 5)
                    assert hint  # Scheduling hint only; authenticated status below is authority.
                    status = await asyncio.to_thread(observe)
                    assert status.application_ready
                    assert ("execution.v1" in status.available_operations) == (
                        scenario == "enabled"
                    )
                    namespace = owner.store.layout.runtime_dir / "execution-backend"
                    assert namespace.exists() == (scenario != "disabled")
                    if scenario != "enabled":
                        call = FreshExecutionCall(
                            request_id=uuid4(),
                            directory=project.root,
                            operation="show",
                            approval_id=uuid4(),
                        )
                        reply = await asyncio.to_thread(
                            RuntimeProjectClient(owner.store.layout).fresh, call
                        )
                        assert reply.error is not None
                        expected = {
                            "mismatch": "execution_backend_unavailable",
                            "capacity": "execution_backend_capacity",
                            "disabled": "execution_unavailable",
                        }
                        assert reply.error.error.code == expected[scenario]
                finally:
                    if not task.done():
                        await asyncio.to_thread(observe, True)
                    await asyncio.wait_for(task, 5)

            old = os.umask(0o177)
            try:
                asyncio.run(exercise())
            finally:
                os.umask(old)
                os.close(reader)
            owner.check()
            owner.require_no_endpoint()
            assert not docker.calls and not docker.queries

    for scenario in ("enabled", "mismatch", "capacity", "disabled"):
        scenario_case(scenario)


def test_backend_initializer_uses_existing_worker_and_gates_readiness_and_drain(
    tmp_path: Path,
) -> None:
    from jeff.application.execution import fresh_execution_failure
    from jeff.application.projects import Failure
    from jeff.application.storage import StorageFault

    def scenario_case(scenario: str) -> None:
        with composed(tmp_path / scenario) as (coordinator, store, project, docker):
            assert store.instance_id is not None
            identity = RuntimeIdentityData(instance_id=store.instance_id, pid=os.getpid())
            entered = threading.Event()
            release = threading.Event()
            names: list[str] = []

            def initialize() -> ExecutionCoordinator | Failure:
                names.append(threading.current_thread().name)
                entered.set()
                if scenario == "held":
                    assert release.wait(3)
                if scenario == "fatal":
                    raise StorageFault("storage_failed")
                return (
                    fresh_execution_failure("execution_backend_unavailable")
                    if scenario == "unavailable"
                    else coordinator
                )

            workers = RuntimeDispatcher(
                ProjectDispatcher(
                    coordinator.projects,
                    QuestionService(coordinator.projects, GitRepositoryRead()),
                    store,
                    identity,
                ),
                fresh_factory=initialize,
            )
            workers.start()
            try:
                assert entered.wait(2) and names == ["jeff-project-0"]
                if scenario == "held":
                    assert not workers.ready and not workers.actions_drained
                    assert not workers.initialization.cancel()
                    assert (
                        len(
                            [t for t in threading.enumerate() if t.name.startswith("jeff-project-")]
                        )
                        == 4
                    )
                    with pytest.raises(ControlFault):
                        workers.submit(
                            ProjectCall(directory=project.root, operation="config"),
                            time.monotonic() + 1,
                        )
                    with pytest.raises(ControlFault) as closing:
                        workers.close(timeout=0)
                    assert (
                        closing.value.code == "runtime_stop_unconfirmed"
                        and not workers.actions_drained
                    )
                    release.set()
                    workers.close(timeout=5)
                    assert workers.fresh is None
                elif scenario == "fatal":
                    with pytest.raises(StorageFault):
                        workers.initialization.result(timeout=2)
                    assert not workers.ready and workers.fresh is None
                else:
                    workers.initialization.result(timeout=2)
                    assert workers.ready
                    if scenario == "success":
                        assert workers.fresh is coordinator and workers.fresh_issue is None
                    else:
                        assert workers.fresh is None and workers.fresh_issue is not None
                    configured = workers.submit(
                        ProjectCall(directory=project.root, operation="config"),
                        time.monotonic() + 5,
                    ).result(timeout=2)
                    assert isinstance(configured, DispatchReply) and configured.result.ok
            finally:
                release.set()
                workers.close(timeout=5)
            assert (
                workers.actions_drained
                and workers.closed
                and not docker.calls
                and not docker.queries
            )

    for scenario in ("held", "success", "unavailable", "fatal"):
        scenario_case(scenario)


def test_configured_backend_activation_pins_identity_without_startup_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jeff.application.projects import Failure
    from jeff.bootstrap.runtime import create_configured_execution

    def no_process(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("startup must not run any child")

    monkeypatch.setattr("jeff.infrastructure.docker_cli.subprocess.Popen", no_process)
    with composed(tmp_path / "ok", configured=True) as (coordinator, store, project, docker):
        assert store.execution_backend is not None
        result = create_configured_execution(
            owner=store.owner,
            storage=store,
            projects=coordinator.projects,
            instance_id=coordinator.instance_id,
            deadline=time.monotonic() + 5,
        )
        assert isinstance(result, ExecutionCoordinator) and not docker.calls and not docker.queries
        root = store.owner.store.layout.runtime_dir / "execution-backend"
        assert set(p.name for p in root.iterdir()) == {str(coordinator.instance_id)}
        with pytest.raises(ControlFault):
            create_configured_execution(
                owner=store.owner,
                storage=store,
                projects=coordinator.projects,
                instance_id=uuid4(),
                deadline=time.monotonic() + 5,
            )
    with composed(tmp_path / "changed", configured=True) as (coordinator, store, project, docker):
        configuration = store.execution_backend
        assert configuration is not None
        configuration.binary.path.chmod(0o700)
        configuration.binary.path.write_bytes(b"different-cli")
        configuration.binary.path.chmod(0o500)
        refused = create_configured_execution(
            owner=store.owner,
            storage=store,
            projects=coordinator.projects,
            instance_id=coordinator.instance_id,
            deadline=time.monotonic() + 5,
        )
        assert (
            isinstance(refused, Failure) and refused.error.code == "execution_backend_unavailable"
        )
        assert store.snapshot().state == "ready" and not docker.calls and not docker.queries
    from jeff.infrastructure.docker_cli import DockerLifecycleCLI, DockerTargetInspector

    class WrongInspector(DockerTargetInspector):
        @property
        def identity(self) -> DockerCLIIdentity:
            return super().identity.model_copy(update={"sha256": "f" * 64})

    class WrongLifecycle(DockerLifecycleCLI):
        def __init__(
            self, binary: Path, config: Path, home: Path, *, deadline: float | None = None
        ) -> None:
            super().__init__(binary, config, home, deadline=deadline)
            self.identity = self.identity.model_copy(update={"size": self.identity.size + 1})

    for index, (name, replacement) in enumerate(
        (
            ("DockerTargetInspector", WrongInspector),
            ("DockerLifecycleCLI", WrongLifecycle),
        )
    ):
        with (
            composed(tmp_path / str(index), configured=True) as (
                coordinator,
                store,
                project,
                docker,
            ),
            monkeypatch.context() as patch,
        ):
            patch.setattr("jeff.bootstrap.runtime." + name, replacement)
            refused = create_configured_execution(
                owner=store.owner,
                storage=store,
                projects=coordinator.projects,
                instance_id=coordinator.instance_id,
                deadline=time.monotonic() + 5,
            )
            assert (
                isinstance(refused, Failure)
                and refused.error.code == "execution_backend_unavailable"
            )
    from jeff.application.storage import StorageFault

    with composed(tmp_path / "closing", configured=True) as (coordinator, store, project, docker):
        store.request_close()
        with pytest.raises(StorageFault):
            create_configured_execution(
                owner=store.owner,
                storage=store,
                projects=coordinator.projects,
                instance_id=coordinator.instance_id,
                deadline=time.monotonic() + 5,
            )
        assert not (store.owner.store.layout.runtime_dir / "execution-backend").exists()
    with composed(tmp_path / "expired", configured=True) as (coordinator, store, project, docker):
        refused = create_configured_execution(
            owner=store.owner,
            storage=store,
            projects=coordinator.projects,
            instance_id=coordinator.instance_id,
            deadline=time.monotonic() - 1,
        )
        assert (
            isinstance(refused, Failure)
            and not (store.owner.store.layout.runtime_dir / "execution-backend").exists()
        )
    with composed(tmp_path / "disabled") as (coordinator, store, project, docker):
        refused = create_configured_execution(
            owner=store.owner,
            storage=store,
            projects=coordinator.projects,
            instance_id=coordinator.instance_id,
            deadline=time.monotonic() + 5,
        )
        assert isinstance(refused, Failure) and refused.error.code == "execution_unavailable"
        assert not (store.owner.store.layout.runtime_dir / "execution-backend").exists()


def test_owned_execution_homes_are_fresh_private_bounded_and_never_repaired(tmp_path: Path) -> None:
    with composed(tmp_path / "valid") as (coordinator, store, project, docker):
        root = store.owner.store.layout.runtime_dir / "execution-backend"
        old = os.umask(0o177)
        try:
            with store.owner.execution_homes(
                coordinator.instance_id, deadline=time.monotonic() + 5
            ) as homes:
                assert homes == (
                    root / str(coordinator.instance_id) / "metadata",
                    root / str(coordinator.instance_id) / "lifecycle",
                )
                for path in (root, homes[0].parent, *homes):
                    assert path.stat().st_mode & 0o777 == 0o700
                assert not tuple(homes[0].iterdir()) and not tuple(homes[1].iterdir())
                store.owner.check()
                store.index(project)  # Signed SQL recovery priming still validates the namespace.
                store.owner.check()
        finally:
            os.umask(old)
        with pytest.raises(DockerBackendFault):
            with store.owner.execution_homes(
                coordinator.instance_id, deadline=time.monotonic() + 5
            ):
                pass
        # Partial generations count; there is no recursive inspection or cleanup.
        for _ in range(15):
            (root / str(uuid4())).mkdir(mode=0o700)
        before = set(root.iterdir())
        with pytest.raises(DockerBackendFault) as full:
            with store.owner.execution_homes(uuid4(), deadline=time.monotonic() + 5):
                pass
        assert full.value.code == "docker_home_capacity" and set(root.iterdir()) == before
        assert not docker.calls and not docker.queries
    with composed(tmp_path / "unsafe") as (_, store, _, _):
        root = store.owner.store.layout.runtime_dir / "execution-backend"
        root.mkdir(mode=0o700)
        root.chmod(0o600)
        try:
            with pytest.raises(DockerBackendFault):
                with store.owner.execution_homes(uuid4(), deadline=time.monotonic() + 5):
                    pass
            assert root.stat().st_mode & 0o777 == 0o600 and not tuple(root.iterdir())
        finally:
            root.chmod(0o700)
    with composed(tmp_path / "link") as (_, store, _, _):
        root = store.owner.store.layout.runtime_dir / "execution-backend"
        outside = tmp_path / "outside"
        outside.mkdir(mode=0o700)
        root.symlink_to(outside, target_is_directory=True)
        with pytest.raises(DockerBackendFault):
            with store.owner.execution_homes(uuid4(), deadline=time.monotonic() + 5):
                pass
        assert root.is_symlink() and not tuple(outside.iterdir())
    with composed(tmp_path / "foreign") as (_, store, _, _):
        root = store.owner.store.layout.runtime_dir / "execution-backend"
        root.mkdir(mode=0o700)
        (root / "unexpected").write_text("public fixture")
        with pytest.raises(DockerBackendFault):
            with store.owner.execution_homes(uuid4(), deadline=time.monotonic() + 5):
                pass
        assert {p.name for p in root.iterdir()} == {"unexpected"}


def test_internal_factory_is_cold_and_composition_rejects_busy_without_queueing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jeff.bootstrap.runtime import create_internal_execution

    with composed(tmp_path) as (coordinator, store, project, docker):
        binary = tmp_path / "docker"
        binary.write_bytes(b"synthetic-cli")
        binary.chmod(0o500)
        config = tmp_path / "cfg"
        config.mkdir(mode=0o700)
        settings = config / "config.json"
        settings.write_text("{}")
        settings.chmod(0o600)
        meta = tmp_path / "m"
        life = tmp_path / "l"
        meta.mkdir(mode=0o700)
        life.mkdir(mode=0o700)

        def no_process(*args: object, **kwargs: object) -> NoReturn:
            raise AssertionError("Cold factory must not execute any child.")

        monkeypatch.setattr("jeff.infrastructure.docker_cli.subprocess.Popen", no_process)
        built = create_internal_execution(
            owner=store.owner,
            storage=store,
            projects=coordinator.projects,
            instance_id=coordinator.instance_id,
            binary=binary,
            config=config,
            metadata_home=meta,
            lifecycle_home=life,
            declarations=coordinator.declarations,
        )
        assert isinstance(built, ExecutionCoordinator) and not docker.calls and not docker.queries
        with pytest.raises(DockerBackendFault):
            create_internal_execution(
                owner=store.owner,
                storage=store,
                projects=coordinator.projects,
                instance_id=uuid4(),
                binary=binary,
                config=config,
                metadata_home=meta,
                lifecycle_home=life,
                declarations=coordinator.declarations,
            )
        record = approved(coordinator, project)
        entered = threading.Event()
        release = threading.Event()
        docker.entered = entered
        docker.release = release
        with ThreadPoolExecutor(max_workers=1) as worker:
            future = worker.submit(
                coordinator.run,
                project.root,
                record.approval_id,
                record.manifest_digest,
                deadline=time.monotonic() + 5,
            )
            try:
                assert entered.wait(2)
                with pytest.raises(DockerBackendFault) as busy:
                    pending(coordinator, project)
                assert busy.value.code == "docker_busy"
            finally:
                release.set()
            assert future.result(timeout=5).disposition == "completed"
        assert docker.calls == ["create", "attach", "remove"]
