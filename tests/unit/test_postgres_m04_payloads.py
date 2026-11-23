"""Pure contracts for the unwired PostgreSQL M04 proof; no service required."""

from pathlib import Path
from uuid import uuid4

import pytest

from jeff.application.execution import (
    AuthorityRecord,
    DockerBackendFault,
    DockerCLIIdentity,
    DockerCompletion,
    DockerExecutionResult,
    DockerJobOutcome,
    DockerLaunchIntent,
    DockerOwnedContainer,
    ExecutionManifest,
    ExecutionProfile,
    PreparedFile,
    PreparedInputBinding,
    PreparedInputs,
    VerifiedDockerTarget,
)
from jeff.application.execution.pytest_report import (
    PYTEST_RUNNER_ARGV,
    PytestFailure,
    PytestReport,
    PytestResult,
)
from jeff.application.execution.watch import notification_id_for
from jeff.application.storage import StorageFault
from jeff.domain.execution import AuthorityState
from jeff.infrastructure.postgres_payloads import decode_report, encode_report
from jeff.infrastructure.postgres_watches import _notice
from jeff.infrastructure.runtime_output import GuardedOutputCollector

_TOKEN = "a" * 32


def _completion(root: Path, *, token: str = _TOKEN, path: str = "test_case.py") -> DockerCompletion:
    project = uuid4()
    profile = ExecutionProfile(
        name="pytest",
        project_id=project,
        canonical_root=root,
        engine_id="synthetic",
        endpoint="unix:///synthetic/docker.sock",
        executable=PYTEST_RUNNER_ARGV[0],
        toolchain="synthetic",
        platform="linux/arm64",
        image_id="sha256:" + "1" * 64,
    )
    inputs = PreparedInputs(files=(PreparedFile(path="test_case.py", size=10, sha256="a" * 64),))
    manifest = ExecutionManifest(profile=profile, argv=(*PYTEST_RUNNER_ARGV, _TOKEN), inputs=inputs)
    record = AuthorityRecord(
        proposal_id=uuid4(),
        approval_id=uuid4(),
        job_id=uuid4(),
        project_id=project,
        canonical_root=root,
        owner_instance_id=uuid4(),
        manifest=manifest,
        manifest_digest=manifest.fingerprint,
        state=AuthorityState.CONSUMED,
        created_at_ms=1,
        expires_at_ms=10,
        claim_state="claimed",
        claim_id=uuid4(),
        claimed_at_ms=2,
    )
    intent = DockerLaunchIntent(
        attempt_id=uuid4(),
        record=record,
        nonce="b" * 32,
        target=VerifiedDockerTarget(
            declaration_digest="c" * 64,
            profile_digest=profile.fingerprint,
            binary=DockerCLIIdentity(path=Path("/synthetic/docker"), sha256="d" * 64, size=1),
            endpoint=profile.endpoint,
            engine_id=profile.engine_id,
            image_id=profile.image_id,
            platform=profile.platform,
            client_version="synthetic",
            server_version="synthetic",
            image_configuration_digest="e" * 64,
            effective_environment=(),
        ),
        inputs=PreparedInputBinding(
            snapshot_id=inputs.snapshot_id, inventory_digest=inputs.digest, path=root / "prepared"
        ),
    )
    capture = GuardedOutputCollector(profile.output_bytes)
    capture.finish("stdout")
    capture.finish("stderr")
    return DockerCompletion(
        owned=DockerOwnedContainer(
            intent=intent, container_id="f" * 64, created_at="2026-01-01T00:00:00Z"
        ),
        outcome=DockerJobOutcome(exit_code=1, cleanup_verified=True),
        output=capture.seal(),
        pytest_result=PytestResult(
            status="available",
            report=PytestReport(
                version=2,
                run_token=token,
                passed=1,
                failed=1,
                exit_status=1,
                failures=(
                    PytestFailure(node_id=f"{path}::test_fails", path=path, line=1, excerpt="oops"),
                ),
            ),
        ),
    )


def test_pytest_report_round_trip_is_bound_to_completion(tmp_path: Path) -> None:
    completion = _completion(tmp_path)
    result = DockerExecutionResult(
        owned=completion.owned,
        outcome=completion.outcome,
        output_facts=completion.output_facts,
        output_digest=completion.output_digest,
        completion_digest=completion.fingerprint,
        disposition="completed",
        sequence=1,
        recorded_at_ms=2,
        payload_state="retained",
        output=completion.output,
    )
    text, digest = encode_report(completion)
    assert decode_report(result, text, digest) == completion.pytest_result
    with pytest.raises(StorageFault):
        decode_report(result, text, "0" * 64)
    with pytest.raises(StorageFault):
        decode_report(result.model_copy(update={"payload_state": "retired"}), text, digest)


@pytest.mark.parametrize(("token", "path"), [("c" * 32, "test_case.py"), (_TOKEN, "other.py")])
def test_unbound_pytest_report_is_rejected(tmp_path: Path, token: str, path: str) -> None:
    with pytest.raises(DockerBackendFault) as error:
        encode_report(_completion(tmp_path, token=token, path=path))
    assert error.value.code == "docker_output_failed"


def test_notice_uses_terminal_facts_not_the_report(tmp_path: Path) -> None:
    record = _completion(tmp_path).owned.intent.record
    assert _notice(record, None, None) is None
    unknown = _notice(record.model_copy(update={"claim_state": "unknown"}), None, None)
    failure = _notice(
        record.model_copy(update={"claim_state": "finished"}),
        None,
        {"disposition": "completed", "outcome": {"exit_code": 1}},
    )
    assert unknown is not None and unknown.outcome == "interrupted" and unknown.exit_code is None
    assert failure is not None and failure.outcome == "failed" and failure.exit_code == 1
    assert (
        unknown.notification_id
        == failure.notification_id
        == notification_id_for(record.project_id, record.job_id)
    )
