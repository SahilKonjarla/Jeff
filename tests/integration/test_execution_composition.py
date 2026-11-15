"""Internal composition with real SQL/files/output/controller; only discovery/Docker I/O fake."""

import copy
import json
import os
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import NoReturn
from uuid import UUID, uuid4

import pytest

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
    PreparedInputFault,
    ReviewFault,
    ReviewOperation,
)
from jeff.application.execution_coordinator import ExecutionCoordinator
from jeff.application.projects import ProjectService
from jeff.application.runtime import RuntimeLayout
from jeff.application.storage import DEFAULT_SETTINGS
from jeff.domain.projects import Project
from jeff.infrastructure.docker_cli import DockerTargetInspector
from jeff.infrastructure.prepared_inputs import FilePreparedInputStore
from jeff.infrastructure.project_files import FileProjectStore
from jeff.infrastructure.runtime_execution import DockerJobController
from jeff.infrastructure.runtime_output import GuardedOutputCollector
from jeff.infrastructure.runtime_settings import load_or_create_settings
from jeff.infrastructure.runtime_state import RuntimeStateStore
from jeff.infrastructure.runtime_storage import SQLiteStorage


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
def composed(root: Path) -> Iterator[tuple[ExecutionCoordinator, SQLiteStorage, Project, Docker]]:
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
        layout.settings.write_text(settings.model_dump_json())  # JSON is valid fixture YAML.
        generation = uuid4()
        store = SQLiteStorage(owner, instance_id=generation)
        store.start()
        try:
            assert store.wait_ready(5).state == "ready"
            store.index(project)
            docker = Docker()
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
            )
            yield coordinator, store, project, docker
        finally:
            store.close(timeout=5)


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
