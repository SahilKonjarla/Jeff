"""Characterize execution contracts while their implementations move into a package."""

import importlib
import inspect
import json
import pickle
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
from typing import get_type_hints
from uuid import UUID

from pydantic import BaseModel

from jeff.application import execution
from jeff.domain.execution import AuthorityState, fingerprint

# Wire14 intentionally admits distinct guarded authority/projections and settings4 policy.
# Wire13 schema pin (historical): 77950bae8bb87158bd506e945c24d4a79f661cd4a442cb1c0c0f9d854f5342b9.
# M04 adds report, log-tail, watch, and job lookup fields; guard schemas stay pinned.
SCHEMA_DIGEST = "5928574a1ca5726dbca044f54202d24e3c52abc2fa5c18b846edb411fc3f076b"
M03_FACTS_DIGEST = "420777cfd00a528e39fc76893005ea6780d76c507513ca8c5c9e6cd7a783dbfa"
FACTS_DIGEST = "82f9ecb7271d4c15a0efe9ae6dc84a61686ea6f830c16d439247e0e90c1d032f"
# Non-executable guard drafts are separate; preserve the entire old schema/fact snapshots.
GUARD_SCHEMAS = {
    "GuardArtifact",
    "GuardedExecutionDraft",
    "GuardChallenge",
    "GuardAuthorize",
    "GuardAbort",
}
GUARD_SCHEMA_DIGEST = "53337a1b4a4a6a6f182b4a3e36be96cf733debb87f169aa20a1dcf7378f6d107"
# JFG2 formats stay isolated from both permanently frozen older schema sets.
GUARD_V2_SCHEMAS = {
    "GuardedExecutionManifest",
    "GuardExecutionArtifact",
    "GuardV2Challenge",
    "GuardV2Authorize",
    "GuardV2Abort",
    "GuardV2Dispatch",
    "GuardV2Stderr",
    "GuardV2Terminal",
}
# Admission docstrings now describe the implemented verifier rather than rejecting all authority.
# Contracts-only historical pin: 71595d3441b96a4eb0051ee57051ecb848f0810c07404d5cee93645702ebe81b.
GUARD_V2_SCHEMA_DIGEST = "ea21c20b737f818d61477697dd18edf670a4afad5d266c928e5029716a7939b5"


def _schemas() -> dict[str, object]:
    return {
        name: value.model_json_schema()
        for name, value in vars(execution).items()
        if isinstance(value, type) and issubclass(value, execution._Model)
    }


def _digest(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _facts() -> dict[str, object]:
    """Fixed synthetic facts cover old reviews, fresh authority, output and stop digests."""
    project_id, owner_id, snapshot_id, proposal_id, approval_id, job_id, claim_id, attempt_id = (
        UUID(int=i) for i in range(1, 9)
    )
    profile = execution.ExecutionProfile(
        name="python",
        project_id=project_id,
        canonical_root=Path("/synthetic/project"),
        engine_id="synthetic-engine",
        endpoint="unix:///synthetic/docker.sock",
        image_id="sha256:" + "1" * 64,
        platform="linux/arm64",
        toolchain="Python 3.12",
        executable="/usr/local/bin/python",
    )
    inputs = execution.PreparedInputs(
        snapshot_id=snapshot_id,
        files=(execution.PreparedFile(path="hello.py", size=7, sha256="2" * 64),),
    )
    legacy = execution.ActionManifest(profile=profile, argv=(profile.executable, "hello.py"))
    manifest = execution.ExecutionManifest(profile=profile, argv=legacy.argv, inputs=inputs)
    authority = execution.AuthorityRecord(
        proposal_id=proposal_id,
        approval_id=approval_id,
        job_id=job_id,
        project_id=project_id,
        canonical_root=profile.canonical_root,
        owner_instance_id=owner_id,
        manifest=manifest,
        manifest_digest=manifest.fingerprint,
        state=AuthorityState.CONSUMED,
        created_at_ms=100,
        expires_at_ms=1000,
        claim_id=claim_id,
        claim_state="claimed",
        claimed_at_ms=200,
    )
    target = execution.VerifiedDockerTarget(
        declaration_digest="3" * 64,
        profile_digest=profile.fingerprint,
        binary=execution.DockerCLIIdentity(
            path=Path("/synthetic/bin/docker"), sha256="4" * 64, size=10
        ),
        endpoint=profile.endpoint,
        engine_id=profile.engine_id,
        image_id=profile.image_id,
        platform=profile.platform,
        client_version="29.0.0",
        server_version="29.0.0",
        image_configuration_digest="5" * 64,
        effective_environment=(),
    )
    intent = execution.DockerLaunchIntent(
        attempt_id=attempt_id,
        record=authority,
        target=target,
        inputs=execution.PreparedInputBinding(
            snapshot_id=snapshot_id,
            inventory_digest=inputs.digest,
            path=Path("/synthetic/prepared/input"),
        ),
        nonce="6" * 32,
    )
    owned = execution.DockerOwnedContainer(
        intent=intent, container_id="7" * 64, created_at="2026-01-01T00:00:00Z"
    )
    output = execution.DockerOutputSnapshot(
        retention_limit=1024,
        stdout="hello\n",
        stderr="",
        retained_bytes=6,
        stdout_source_bytes=6,
        stderr_source_bytes=0,
        stdout_eof=True,
        stderr_eof=True,
        aborted=False,
        truncated=False,
        guard_reason="none",
    )
    completion = execution.DockerCompletion(
        owned=owned,
        outcome=execution.DockerJobOutcome(exit_code=0, cleanup_verified=True),
        output=output,
    )
    scope = execution.ExecutionInspectionScope(
        project_id=project_id,
        canonical_root=profile.canonical_root,
        owner_instance_id=owner_id,
        approval_id=approval_id,
    )
    stop = execution.ExecutionStopRecord(
        scope=scope,
        request_id=UUID(int=9),
        manifest_digest=manifest.fingerprint,
        reason="cancel",
        requested_at_ms=300,
        clock_confirmed=True,
        terminal=execution.ExecutionStopTerminal(
            outcome="cancelled",
            evidence="owned",
            cleanup_verified=True,
            exit_code=0,
            attempt_id=attempt_id,
            ownership_digest=fingerprint(
                {
                    "intent": intent.fingerprint,
                    "container": owned.container_id,
                    "birth": owned.created_at,
                }
            ),
            output_facts=completion.output_facts,
            output_digest=completion.output_digest,
        ),
    )
    return {
        "profile": profile.model_dump_json(),
        "inputs": inputs.model_dump_json(),
        "inputs_digest": inputs.digest,
        "legacy": legacy.model_dump_json(),
        "legacy_digest": legacy.fingerprint,
        "manifest": manifest.model_dump_json(),
        "manifest_digest": manifest.fingerprint,
        "intent": intent.model_dump_json(),
        "intent_digest": intent.fingerprint,
        "completion": completion.model_dump_json(),
        "completion_digest": completion.fingerprint,
        "output_digest": completion.output_digest,
        "stop": stop.model_dump_json(),
        "stop_digest": stop.fingerprint,
        "stop_view": execution.ExecutionStopView.from_record(stop).model_dump_json(),
    }


def test_model_schemas_preserve_original_contracts() -> None:
    schemas = _schemas()
    assert GUARD_SCHEMAS <= schemas.keys()
    assert GUARD_V2_SCHEMAS <= schemas.keys()
    assert GUARD_SCHEMAS.isdisjoint(GUARD_V2_SCHEMAS)
    assert (
        _digest(
            {
                key: value
                for key, value in schemas.items()
                if key not in GUARD_SCHEMAS | GUARD_V2_SCHEMAS
            }
        )
        == SCHEMA_DIGEST
    )
    assert _digest({key: schemas[key] for key in sorted(GUARD_SCHEMAS)}) == GUARD_SCHEMA_DIGEST
    assert (
        _digest({key: schemas[key] for key in sorted(GUARD_V2_SCHEMAS)}) == GUARD_V2_SCHEMA_DIGEST
    )


def test_execution_facts_match_m04_and_project_back_to_m03() -> None:
    facts = _facts()
    assert _digest(facts) == FACTS_DIGEST
    completion_json, stop_json = facts["completion"], facts["stop"]
    assert isinstance(completion_json, str) and isinstance(stop_json, str)
    completion, stop = json.loads(completion_json), json.loads(stop_json)
    assert completion.pop("pytest_result") is None
    assert stop["scope"].pop("job_id") is None
    assert stop["scope"].pop("latest_pytest") is False
    # Compare the legacy projection, not a claim that the SQLite reader still
    # accepts old stop digests after the model has filled in the new defaults.
    facts["completion"] = json.dumps(completion, separators=(",", ":"))
    facts["stop"] = json.dumps(stop, separators=(",", ":"))
    facts["stop_digest"] = fingerprint(stop)
    assert _digest(facts) == M03_FACTS_DIGEST


def test_facade_exports_alias_definitions_and_resolves_method_annotations() -> None:
    for name in execution.__all__:
        value = getattr(execution, name)
        if inspect.isclass(value) or inspect.isfunction(value):
            assert getattr(importlib.import_module(value.__module__), name) is value
            if inspect.isfunction(value):
                get_type_hints(value)
            else:
                for method in vars(value).values():
                    if isinstance(method, (classmethod, staticmethod)):
                        method = method.__func__
                    if inspect.isfunction(method):
                        get_type_hints(method)


def test_package_files_stay_within_size_limit(code_line_count: Callable[[str], int]) -> None:
    package = Path(execution.__file__).parent
    for path in package.glob("*.py"):
        assert code_line_count(path.read_text()) <= 400, path.name


def test_contract_models_remain_pickleable_through_public_imports() -> None:
    value = execution.ExecutionCancelCall(
        request_id=UUID(int=1), approval_id=UUID(int=2), reviewed_manifest_digest="a" * 64
    )
    assert pickle.loads(pickle.dumps(value)) == value
    assert isinstance(value, BaseModel)
