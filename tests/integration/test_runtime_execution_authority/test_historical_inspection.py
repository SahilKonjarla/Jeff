"""Historical observations retain original bindings without transferring control.

Source tests reopen an owned synthetic SQL fixture, never a real daemon/job.
"""

import json
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from jeff.application.execution import (
    AuthorityRecord,
    ExecutionInspectionScope,
    ExecutionStopRequest,
    ReviewFault,
)
from jeff.domain.projects import Project
from jeff.infrastructure.runtime_storage import SQLiteStorage

from .support_authority import authority_store, command
from .support_launch import completion, fact, launch_intent, launch_scope, stopped_sample


@contextmanager
def restarted(
    previous: SQLiteStorage, *, retention_clock: Callable[[], int] | None = None
) -> Iterator[tuple[SQLiteStorage, UUID]]:
    previous.close(timeout=5)
    generation = uuid4()
    current = SQLiteStorage(previous.owner, instance_id=generation, retention_clock=retention_clock)
    current.start()
    try:
        assert current.wait_ready(5).state == "ready"
        yield current, generation
    finally:
        current.close(timeout=5)


def reader_scope(record: AuthorityRecord, current: UUID) -> ExecutionInspectionScope:
    return ExecutionInspectionScope(
        project_id=record.project_id,
        canonical_root=record.canonical_root,
        owner_instance_id=current,
        approval_id=record.approval_id,
    )


@pytest.mark.parametrize("unknown", (False, True), ids=("completed", "quarantined"))
def test_new_owner_reads_original_recorded_result_without_rebinding(
    tmp_path: Path, unknown: bool
) -> None:
    with authority_store(tmp_path) as (store, project, manifest, generation):
        intent = launch_intent(store, project, manifest, generation)
        for phase in ("create_intent", "created", "start_intent", "exited", "cleaned"):
            store.record(fact(intent, phase), deadline=time.monotonic() + 5)
        original = store.complete_execution(
            completion(intent, unknown=unknown), deadline=time.monotonic() + 5
        )
        with restarted(store) as (again, current):
            scope = ExecutionInspectionScope(
                project_id=project.project_id,
                canonical_root=project.root,
                owner_instance_id=current,
                approval_id=intent.record.approval_id,
            )
            observed = again.inspect_execution(scope, deadline=time.monotonic() + 5)
            assert observed.completion_state == "recorded" and observed.result == original
            assert observed.record.owner_instance_id == generation != current
            assert observed.record.claim_state == ("unknown" if unknown else "finished")
            assert observed.result.output is not None
            assert observed.result.output.stdout == "public output\n"
            assert (
                again.execution_result(launch_scope(intent), deadline=time.monotonic() + 5)
                == original
            )


@pytest.mark.parametrize("retain", (False, True), ids=("metadata-only", "retained"))
def test_historical_stop_keeps_canonical_intent_and_partial_payload(
    tmp_path: Path, retain: bool
) -> None:
    with authority_store(tmp_path) as (store, project, manifest, generation):
        original, capture = stopped_sample(store, project, manifest, generation, retain=retain)
        with restarted(store) as (again, current):
            scope = original.scope.model_copy(update={"owner_instance_id": current})
            observed = again.inspect_execution(scope, deadline=time.monotonic() + 5)
            assert observed.completion_state == "stop_recorded" and observed.result is None
            assert observed.record.owner_instance_id == generation != current
            assert observed.stop is not None
            assert observed.stop.request_id == original.request_id
            assert observed.stop.reason == original.reason
            assert observed.stop.requested_at_ms == original.requested_at_ms
            assert observed.stop.outcome == "cancelled" and observed.stop.cleanup_verified
            assert observed.stop_output == (capture.output if retain else None)
            assert observed.stop.payload_state == ("retained" if retain else "missing")


@pytest.mark.parametrize("boundary", (0, 1, 3), ids=("claim", "create", "start"))
@pytest.mark.parametrize("stop_intent", (False, True), ids=("no-stop", "pending-stop"))
def test_historical_unknown_never_invents_completion(
    tmp_path: Path, boundary: int, stop_intent: bool
) -> None:
    with authority_store(tmp_path) as (store, project, manifest, generation):
        intent = launch_intent(store, project, manifest, generation)
        for phase in ("create_intent", "created", "start_intent")[:boundary]:
            store.record(fact(intent, phase), deadline=time.monotonic() + 5)
        original = None
        if stop_intent:
            original = store.request_stop(
                ExecutionStopRequest(
                    **reader_scope(intent.record, generation).model_dump(),
                    request_id=uuid4(),
                    manifest_digest=manifest.fingerprint,
                ),
                deadline=time.monotonic() + 5,
            )
        with restarted(store) as (again, current):
            observed = again.inspect_execution(
                reader_scope(intent.record, current), deadline=time.monotonic() + 5
            )
            assert observed.record.claim_state == "unknown"
            assert observed.completion_state == (
                "launch_without_result" if boundary else "claim_only"
            )
            assert observed.result is None and observed.stop_output is None
            if original is None:
                assert observed.stop is None
            else:
                assert observed.stop is not None and observed.stop.outcome is None
                assert observed.stop.request_id == original.request_id
                assert observed.stop.requested_at_ms == original.requested_at_ms
                assert observed.stop.cleanup_verified is None


def test_historical_prestart_revocation_remains_positive_no_launch_evidence(tmp_path: Path) -> None:
    with authority_store(tmp_path) as (store, project, manifest, generation):
        pending = store.authority(
            command(
                project,
                generation,
                operation="propose",
                manifest=manifest,
                current_manifest_digest=manifest.fingerprint,
            ),
            deadline=time.monotonic() + 5,
        ).record
        store.authority(
            command(
                project,
                generation,
                operation="approve",
                approval_id=pending.approval_id,
                current_manifest_digest=manifest.fingerprint,
            ),
            deadline=time.monotonic() + 5,
        )
        original = store.request_stop(
            ExecutionStopRequest(
                **reader_scope(pending, generation).model_dump(),
                request_id=uuid4(),
                manifest_digest=manifest.fingerprint,
            ),
            deadline=time.monotonic() + 5,
        )
        with restarted(store) as (again, current):
            observed = again.inspect_execution(
                reader_scope(pending, current), deadline=time.monotonic() + 5
            )
            assert observed.record.state == "invalidated" and observed.record.claim_id is None
            assert observed.completion_state == "stop_recorded" and observed.result is None
            assert observed.stop is not None and observed.stop.request_id == original.request_id
            assert observed.stop.evidence == "not_started" and observed.stop.cleanup_verified
            assert observed.stop.payload_state == "missing" and observed.stop_output is None


@pytest.mark.parametrize("stopped", (False, True), ids=("ordinary", "stopped"))
@pytest.mark.parametrize("retired", (False, True), ids=("expired", "retired"))
def test_historical_unavailable_text_preserves_terminal_facts(
    tmp_path: Path, stopped: bool, retired: bool
) -> None:
    wall = [100_000_000]

    def clock() -> int:
        return wall[0]

    with authority_store(tmp_path, retention_clock=clock) as (store, project, manifest, generation):
        if stopped:
            original, _ = stopped_sample(store, project, manifest, generation)
            scope = original.scope
        else:
            intent = launch_intent(store, project, manifest, generation)
            for phase in ("create_intent", "created", "start_intent", "exited", "cleaned"):
                store.record(fact(intent, phase), deadline=time.monotonic() + 5)
            store.complete_execution(completion(intent), deadline=time.monotonic() + 5)
            scope = reader_scope(intent.record, generation)
        before = store.inspect_execution(scope, deadline=time.monotonic() + 5)
        wall[0] += 31 * 86_400_000
        with restarted(store, retention_clock=clock) as (again, current):
            if retired:
                again.retire(deadline=time.monotonic() + 5)
            after = again.inspect_execution(
                scope.model_copy(update={"owner_instance_id": current}),
                deadline=time.monotonic() + 5,
            )
            assert after.record == before.record
            if stopped:
                assert after.stop is not None and before.stop is not None
                assert after.stop_output is None and after.stop.outcome == before.stop.outcome
                assert after.stop.output_facts == before.stop.output_facts
            else:
                assert after.result is not None and before.result is not None
                assert after.result.output is None
                assert after.result.completion_digest == before.result.completion_digest
                assert after.result.outcome == before.result.outcome
            payload = after.stop if stopped else after.result
            assert payload is not None
            assert payload.payload_state == ("retired" if retired else "expired")
            assert payload.retirement_reason == ("age" if retired else None)


@pytest.mark.parametrize(
    "mismatch", ("reader", "project", "root", "approval", "registered-project")
)
def test_historical_reads_still_require_current_reader_and_exact_registered_scope(
    tmp_path: Path, mismatch: str
) -> None:
    with authority_store(tmp_path) as (store, project, manifest, generation):
        intent = launch_intent(store, project, manifest, generation)
        with restarted(store) as (again, current):
            scope = reader_scope(intent.record, current)
            if mismatch == "reader":
                scope = scope.model_copy(update={"owner_instance_id": generation})
            elif mismatch == "project":
                scope = scope.model_copy(update={"project_id": uuid4()})
            elif mismatch == "root":
                scope = scope.model_copy(update={"canonical_root": project.root / "wrong"})
            elif mismatch == "approval":
                scope = scope.model_copy(update={"approval_id": uuid4()})
            else:
                other = Project(uuid4(), tmp_path / "other")
                again.index(other)
                scope = scope.model_copy(
                    update={"project_id": other.project_id, "canonical_root": other.root}
                )
            with pytest.raises(ReviewFault):
                again.inspect_execution(scope, deadline=time.monotonic() + 5)


def test_historical_read_requires_an_identified_current_storage_instance(tmp_path: Path) -> None:
    with authority_store(tmp_path) as (store, project, manifest, generation):
        intent = launch_intent(store, project, manifest, generation)
        store.close(timeout=5)
        again = SQLiteStorage(store.owner)
        again.start()
        try:
            assert again.wait_ready(5).state == "ready"
            with pytest.raises(ReviewFault, match="execution_target_mismatch"):
                again.inspect_execution(
                    reader_scope(intent.record, uuid4()), deadline=time.monotonic() + 5
                )
        finally:
            again.close(timeout=5)


@pytest.mark.parametrize("kind", ("scope", "manifest", "digest", "payload"))
def test_historical_stop_rejects_corrupt_original_bindings(tmp_path: Path, kind: str) -> None:
    with authority_store(tmp_path) as (store, project, manifest, generation):
        original, _ = stopped_sample(store, project, manifest, generation)
        store.close(timeout=5)
        # Corrupt only this closed synthetic fixture; assertions use the inspection port.
        with closing(sqlite3.connect(store.owner.store.layout.database)) as sql, sql:
            if kind == "payload":
                sql.execute("UPDATE execution_stop_payloads SET stdout='altered public text'")
            elif kind == "digest":
                sql.execute("UPDATE execution_lifecycles SET record_digest=?", ("0" * 64,))
            else:
                value = original.model_dump(mode="json")
                if kind == "scope":
                    value["scope"]["owner_instance_id"] = str(uuid4())
                else:
                    value["manifest_digest"] = "0" * 64
                sql.execute("UPDATE execution_lifecycles SET record_json=?", (json.dumps(value),))
        with restarted(store) as (again, current):
            scope = original.scope.model_copy(update={"owner_instance_id": current})
            with pytest.raises(ReviewFault, match="storage_schema_unsupported"):
                again.inspect_execution(scope, deadline=time.monotonic() + 5)


def test_historical_completion_rejects_changed_original_owner_binding(tmp_path: Path) -> None:
    with authority_store(tmp_path) as (store, project, manifest, generation):
        intent = launch_intent(store, project, manifest, generation)
        for phase in ("create_intent", "created", "start_intent", "exited", "cleaned"):
            store.record(fact(intent, phase), deadline=time.monotonic() + 5)
        store.complete_execution(completion(intent), deadline=time.monotonic() + 5)
        store.close(timeout=5)
        # Controlled corruption of this synthetic fixture only, not an application mutation path.
        with closing(sqlite3.connect(store.owner.store.layout.database)) as sql, sql:
            sql.execute("UPDATE execution_authorities SET owner_instance_id=?", (str(uuid4()),))
        with restarted(store) as (again, current):
            with pytest.raises(ReviewFault, match="storage_schema_unsupported"):
                again.inspect_execution(
                    reader_scope(intent.record, current), deadline=time.monotonic() + 5
                )
