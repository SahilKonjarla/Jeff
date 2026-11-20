"""Characterize controller interfaces, session defaults and fixed container policy argv."""

import inspect
import json
from collections.abc import Callable
from dataclasses import fields
from hashlib import sha256
from pathlib import Path
from typing import get_type_hints
from uuid import UUID

from jeff.application.execution import (
    AuthorityRecord,
    DockerCLIIdentity,
    DockerLaunchIntent,
    DockerOwnedContainer,
    DockerTargetDeclaration,
    ExecutionManifest,
    ExecutionProfile,
    PreparedInputBinding,
    PreparedInputs,
    VerifiedDockerTarget,
)
from jeff.domain.execution import AuthorityState
from jeff.infrastructure import runtime_execution as native

ARGUMENT_DIGEST = "2ed26476550eec81b4b0937da8e9c75c2b25d2fb672e70ff33e6bab94f8d230d"
SESSION_DIGEST = "770e77a6fa52249da0ded6593ce0321bac5749347bdea41facfebdf135e4db5b"
SIGNATURE_DIGEST = "73a9993a5f47d8cf8e93135a478a313fd250a6b2cbe652deebc0a2ef2d319df9"


def _digest(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _handle() -> DockerOwnedContainer:
    profile = ExecutionProfile(
        name="python",
        project_id=UUID(int=1),
        canonical_root=Path("/synthetic/project"),
        engine_id="synthetic-engine",
        endpoint="unix:///synthetic/docker.sock",
        image_id="sha256:" + "1" * 64,
        platform="linux/arm64",
        toolchain="Python 3.12",
        executable="/usr/local/bin/python",
    )
    inputs = PreparedInputs(snapshot_id=UUID(int=2))
    manifest = ExecutionManifest(profile=profile, argv=(profile.executable, "-V"), inputs=inputs)
    record = AuthorityRecord(
        proposal_id=UUID(int=3),
        approval_id=UUID(int=4),
        job_id=UUID(int=5),
        project_id=profile.project_id,
        canonical_root=profile.canonical_root,
        owner_instance_id=UUID(int=6),
        manifest=manifest,
        manifest_digest=manifest.fingerprint,
        state=AuthorityState.CONSUMED,
        created_at_ms=100,
        expires_at_ms=1000,
        claim_id=UUID(int=7),
        claim_state="claimed",
        claimed_at_ms=200,
    )
    declaration = DockerTargetDeclaration(profile=profile, image_environment=(("LANG", "C.UTF-8"),))
    target = VerifiedDockerTarget(
        declaration_digest=declaration.fingerprint,
        profile_digest=profile.fingerprint,
        binary=DockerCLIIdentity(path=Path("/synthetic/bin/docker"), sha256="2" * 64, size=10),
        endpoint=profile.endpoint,
        engine_id=profile.engine_id,
        image_id=profile.image_id,
        platform=profile.platform,
        client_version="29.0.0",
        server_version="29.0.0",
        image_configuration_digest="3" * 64,
        effective_environment=declaration.image_environment,
    )
    intent = DockerLaunchIntent(
        attempt_id=UUID(int=8),
        record=record,
        target=target,
        inputs=PreparedInputBinding(
            snapshot_id=inputs.snapshot_id,
            inventory_digest=inputs.digest,
            path=Path("/synthetic/prepared/input"),
        ),
        nonce="4" * 32,
    )
    return DockerOwnedContainer(
        intent=intent, container_id="5" * 64, created_at="2026-01-01T00:00:00Z"
    )


def _arguments() -> dict[str, object]:
    intent = _handle().intent
    return {
        "argv": list(native.create_arguments(intent)),
        "labels": native.ownership_labels(intent),
        "scratch": native.scratch_options(intent),
    }


def _session() -> dict[str, object]:
    session = native._Session(_handle())
    return {
        field.name: getattr(session, field.name)
        for field in fields(session)
        if field.name != "handle"
    }


def _signatures() -> dict[str, str]:
    result = {}
    for name, value in vars(native.DockerJobController).items():
        if isinstance(value, (staticmethod, classmethod)):
            value = value.__func__
        if inspect.isfunction(value):
            signature = str(inspect.signature(value)).replace(
                f"{native._Session.__module__}._Session",
                "jeff.infrastructure.runtime_execution._Session",
            )
            result[name] = signature
            get_type_hints(value)
    return result


def test_fixed_create_arguments_and_ownership_metadata_remain_identical() -> None:
    assert _digest(_arguments()) == ARGUMENT_DIGEST


def test_session_flags_and_finalization_defaults_remain_identical() -> None:
    assert _digest(_session()) == SESSION_DIGEST


def test_controller_signatures_and_resolvable_annotations_remain_identical() -> None:
    assert _digest(_signatures()) == SIGNATURE_DIGEST


def test_budget_classmethod_retains_dynamic_deadline_dispatch() -> None:
    seen: list[float] = []

    class Controller(native.DockerJobController):
        @staticmethod
        def _deadline(deadline: float) -> None:
            seen.append(deadline)

    Controller._budgets(100, 105)
    assert seen == [100]


def test_package_files_stay_within_size_limit(code_line_count: Callable[[str], int]) -> None:
    for path in Path(native.__file__).parent.glob("*.py"):
        assert code_line_count(path.read_text()) <= 400, path.name
