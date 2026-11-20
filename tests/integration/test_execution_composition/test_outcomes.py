"""Reviewed-copy execution, preclaim drift and distinct mutation/completion uncertainty."""

import time
from pathlib import Path
from uuid import uuid4

import pytest

from jeff.application.execution import (
    ActionManifest,
    AuthorityOperation,
    AuthorityResult,
    DockerBackendFault,
    DockerCompletion,
    DockerExecutionController,
    DockerExecutionResult,
    DockerJobOutcome,
    DockerLaunchScope,
    DockerOutputSink,
    DockerOwnedContainer,
    ExecutionCall,
    ExecutionProfile,
    PreparedInputFault,
    ReviewFault,
    ReviewOperation,
)
from jeff.domain.projects import Project
from jeff.infrastructure.project_files import FileProjectStore
from jeff.infrastructure.runtime_storage import SQLiteStorage

from .support_composition import approved, composed, pending, show
from .support_docker import Docker


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
