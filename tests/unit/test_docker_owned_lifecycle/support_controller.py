"""Fresh synthetic consumed claim, target and controller composition for each scenario."""

from pathlib import Path
from uuid import uuid4

from jeff.application.execution import (
    AuthorityRecord,
    AuthorityResult,
    DockerCLIIdentity,
    DockerTargetDeclaration,
    ExecutionManifest,
    ExecutionProfile,
    PreparedInputs,
    VerifiedDockerTarget,
)
from jeff.domain.execution import AuthorityState
from jeff.infrastructure.runtime_execution import DockerJobController

from .support_ports import Backend, Inputs, Journal, Verifier


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
