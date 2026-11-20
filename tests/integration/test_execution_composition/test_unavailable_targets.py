"""Read-only target refusal precedes proposal creation; later uncertainty stays unknown."""

import time
from pathlib import Path
from uuid import uuid4

import pytest

from jeff.application.execution import (
    AuthorityOperation,
    AuthorityResult,
    DockerBackendFault,
    DockerTargetDeclaration,
    FreshExecutionCall,
    VerifiedDockerTarget,
)

from .support_composition import composed


def test_missing_engine_or_image_is_confirmed_before_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with composed(tmp_path, guarded=True) as (coordinator, _, project, docker):
        for fault in (
            DockerBackendFault("docker_unavailable"),
            DockerBackendFault("docker_command_failed"),
        ):

            def unavailable(
                declaration: DockerTargetDeclaration,
                *,
                deadline: float,
                fault: DockerBackendFault = fault,
            ) -> VerifiedDockerTarget:
                raise fault

            def no_preparation(*args: object, **kwargs: object) -> None:
                raise AssertionError("Unavailable target must refuse before preparing inputs")

            monkeypatch.setattr(coordinator.verifier, "verify_target", unavailable)
            monkeypatch.setattr(coordinator.preparer, "prepare", no_preparation)
            reply = coordinator.execute(
                FreshExecutionCall(
                    request_id=uuid4(),
                    directory=project.root,
                    operation="propose",
                    profile_name="python",
                    argv=("/usr/local/bin/python", "-V"),
                ),
                time.monotonic() + 5,
            )
            assert not reply.ok and reply.error is not None
            assert reply.error.error.code == "execution_unavailable"
            assert reply.view is None and reply.admission is None and not docker.calls
        page = coordinator.execute(
            FreshExecutionCall(
                request_id=uuid4(), directory=project.root, operation="approvals.list"
            ),
            time.monotonic() + 5,
        )
        assert page.page is not None and not page.page.items


def test_uncertain_metadata_helper_cleanup_is_not_confirmed_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with composed(tmp_path, guarded=True) as (coordinator, _, project, docker):

        def uncertain(
            declaration: DockerTargetDeclaration, *, deadline: float
        ) -> VerifiedDockerTarget:
            raise DockerBackendFault("docker_cleanup_unconfirmed")

        monkeypatch.setattr(coordinator.verifier, "verify_target", uncertain)
        reply = coordinator.execute(
            FreshExecutionCall(
                request_id=uuid4(),
                directory=project.root,
                operation="propose",
                profile_name="python",
                argv=("/usr/local/bin/python", "-V"),
            ),
            time.monotonic() + 5,
        )
        assert not reply.ok and reply.error is not None
        assert reply.error.error.code == "execution_unconfirmed" and not docker.calls


def test_same_availability_code_after_proposal_commit_remains_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with composed(tmp_path, guarded=True) as (coordinator, store, project, docker):
        original = store.authority

        def lost_reply(operation: AuthorityOperation, *, deadline: float) -> AuthorityResult:
            result = original(operation, deadline=deadline)
            if operation.operation == "propose":
                raise DockerBackendFault("docker_unavailable")
            return result

        monkeypatch.setattr(store, "authority", lost_reply)
        reply = coordinator.execute(
            FreshExecutionCall(
                request_id=uuid4(),
                directory=project.root,
                operation="propose",
                profile_name="python",
                argv=("/usr/local/bin/python", "-V"),
            ),
            time.monotonic() + 5,
        )
        assert not reply.ok and reply.error is not None
        assert reply.error.error.code == "execution_unconfirmed" and not docker.calls
        page = coordinator.execute(
            FreshExecutionCall(
                request_id=uuid4(), directory=project.root, operation="approvals.list"
            ),
            time.monotonic() + 5,
        )
        assert page.page is not None and len(page.page.items) == 1
        assert (
            page.page.items[0].state.value == "pending" and page.page.items[0].claim_state is None
        )
