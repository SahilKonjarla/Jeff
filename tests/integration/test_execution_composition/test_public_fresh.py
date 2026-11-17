"""Public fresh projections and scoped proposal/decision UUID replay."""

import time
from pathlib import Path
from uuid import uuid4

import pytest

from jeff.application.execution import (
    ActionManifest,
    AuthorityOperation,
    AuthorityResult,
    DockerBackendFault,
    ExecutionCall,
    PreparedInputFault,
    ProposalRequestScope,
    ReviewFault,
    ReviewOperation,
)
from jeff.infrastructure.runtime_storage import SQLiteStorage

from .support_composition import composed


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
