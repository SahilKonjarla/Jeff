"""Public coordinator interface remains stable while implementation details move."""

import inspect
import json
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
from typing import Never, cast, get_type_hints
from uuid import UUID

import pytest

from jeff.application.execution import (
    AuthorityStore,
    DockerBackendFault,
    DockerCompletionStore,
    DockerExecutionController,
    DockerTargetVerifier,
    ExecutionCancelCall,
    FreshExecutionCall,
    PreparedInputPreparer,
    PreparedInputVerifier,
)
from jeff.application.execution_coordinator import ExecutionCoordinator
from jeff.application.projects import ProjectService
from jeff.application.storage import ProjectRegistry

PUBLIC_MEMBERS = (
    "__init__",
    "shutdown_confirmed",
    "cancellation_available",
    "submit_cancel",
    "begin_shutdown",
    "execution_phase",
    "execute",
    "propose",
    "decide",
    "start",
    "run",
)
# Captured before extraction; excludes definition-module metadata, not annotations/defaults.
SIGNATURE_DIGEST = "c536227f1fa2b43a758b0380445f11ce8ce947d1c3018482c6b02e8e303344ad"


def _signatures() -> dict[str, str]:
    signatures = {}
    for name in PUBLIC_MEMBERS:
        member = getattr(ExecutionCoordinator, name)
        if isinstance(member, property):
            member = member.fget
        signatures[name] = str(inspect.signature(member))
        get_type_hints(member)
    return signatures


def _coordinator() -> ExecutionCoordinator:
    def forbidden(*args: object, **kwargs: object) -> Never:
        raise AssertionError("Construction/idle control must not invoke execution collaborators")

    # These inactive placeholders deliberately cannot perform I/O. Calls tested
    # here must refuse before project, storage, verification or lifecycle access.
    return ExecutionCoordinator(
        cast(ProjectService, None),
        cast(ProjectRegistry, None),
        cast(AuthorityStore, None),
        cast(PreparedInputPreparer, None),
        cast(PreparedInputVerifier, None),
        cast(DockerTargetVerifier, None),
        cast(DockerExecutionController, None),
        cast(DockerCompletionStore, None),
        forbidden,
        profiles=forbidden,
        declarations=(),
        instance_id=UUID(int=1),
    )


def test_public_signatures_and_annotations_preserve_original_interface() -> None:
    serialized = json.dumps(_signatures(), sort_keys=True).encode()
    assert sha256(serialized).hexdigest() == SIGNATURE_DIGEST


def test_constructor_and_idle_shutdown_remain_cold() -> None:
    coordinator = _coordinator()
    assert coordinator.shutdown_confirmed
    assert not coordinator.cancellation_available
    assert coordinator.execution_phase(UUID(int=2)) is None
    coordinator.begin_shutdown(100)
    coordinator.begin_shutdown(90)
    assert coordinator.shutdown_confirmed


def test_unavailable_cancellation_refuses_before_any_collaborator_access() -> None:
    coordinator = _coordinator()
    call = ExecutionCancelCall(
        request_id=UUID(int=2), approval_id=UUID(int=3), reviewed_manifest_digest="a" * 64
    )
    with pytest.raises(DockerBackendFault) as caught:
        coordinator.submit_cancel(call, deadline=100)
    assert caught.value.code == "docker_authority_required"


def test_expired_public_request_refuses_without_touching_project_or_storage() -> None:
    coordinator = _coordinator()
    call = FreshExecutionCall(
        request_id=UUID(int=2),
        directory=Path("/synthetic/project"),
        operation="show",
        approval_id=UUID(int=3),
    )
    reply = coordinator.execute(call, deadline=0)
    assert not reply.ok and reply.error is not None
    assert reply.error.error.code == "execution_unconfirmed"
    assert reply.identity == UUID(int=1) and reply.request_id == call.request_id


def test_package_files_stay_within_size_limit(code_line_count: Callable[[str], int]) -> None:
    import jeff.application.execution_coordinator as package

    for path in Path(package.__file__).parent.glob("*.py"):
        assert code_line_count(path.read_text()) <= 400, path.name
