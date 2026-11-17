"""Finite cancellation admission, indexed scope and guarded atomic stop-output capture."""

import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

from jeff.application.execution import (
    DockerCompletion,
    DockerJobOutcome,
    DockerOwnedContainer,
    ReviewFault,
)
from jeff.application.storage import StorageFault
from jeff.infrastructure.runtime_output import GuardedOutputCollector

from .support_authority import authority_store, command
from .support_launch import fact, launch_intent


def test_sql_control_reservation_stays_inside_sixteen_pending_positions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    from jeff.application.execution import ExecutionCancelCall

    original = sqlite3.connect
    entered, release = threading.Event(), threading.Event()
    armed = [False]

    class Paused(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
            if armed[0] and sql.startswith("SELECT project_id,canonical_root FROM projects"):
                armed[0] = False
                entered.set()
                assert release.wait(3)
            return super().execute(sql, parameters)

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        return cast(sqlite3.Connection, original(*args, **{**kwargs, "factory": Paused}))

    monkeypatch.setattr(sqlite3, "connect", connect)
    with authority_store(tmp_path) as (store, project, manifest, generation):
        proposed = store.authority(
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
                approval_id=proposed.approval_id,
                current_manifest_digest=manifest.fingerprint,
            ),
            deadline=time.monotonic() + 5,
        )
        call = ExecutionCancelCall(
            request_id=uuid4(),
            approval_id=proposed.approval_id,
            reviewed_manifest_digest=manifest.fingerprint,
        )
        with ThreadPoolExecutor(max_workers=1) as observer:
            armed[0] = True
            lookup = observer.submit(store.lookup, project, deadline=time.monotonic() + 5)
            try:
                assert entered.wait(1)
                # The public nonblocking control port provides deterministic admission
                # while the one existing SQL actor is paused, without private queue reads.
                queued = [
                    store.submit_cancel(call, deadline=time.monotonic() + 5) for _ in range(15)
                ]
                with pytest.raises(StorageFault) as ordinary:
                    store.lookup(project, deadline=time.monotonic() + 5)
                assert ordinary.value.code == "storage_busy"
                queued.append(store.submit_cancel(call, deadline=time.monotonic() + 5))
                with pytest.raises(StorageFault) as full:
                    store.submit_cancel(call, deadline=time.monotonic() + 5)
                assert full.value.code == "storage_busy"
            finally:
                release.set()
            assert lookup.result(timeout=2) == project
            first = queued[0].result(timeout=2)
            assert all(f.result(timeout=2) == first for f in queued)
            assert first.terminal is not None and first.terminal.evidence == "not_started"


def test_stop_guarded_capture_is_atomic_and_inspectable(tmp_path: Path) -> None:
    from jeff.application.execution import (
        ExecutionInspectionScope,
        ExecutionStopObservation,
        ExecutionStopRequest,
        ExecutionStopTerminal,
    )
    from jeff.domain.execution import fingerprint

    with authority_store(tmp_path) as (store, project, manifest, generation):
        intent = launch_intent(store, project, manifest, generation)
        for phase in ("create_intent", "created", "start_intent", "exited", "cleaned"):
            store.record(fact(intent, phase), deadline=time.monotonic() + 5)
        owned = DockerOwnedContainer(
            intent=intent, container_id="e" * 64, created_at="2026-01-01T00:00:00Z"
        )
        # Use the exact birth observation from the journal helper, not an invented handle.
        owned = owned.model_copy(update={"created_at": fact(intent, "created").created_at})
        scope = ExecutionInspectionScope(
            project_id=project.project_id,
            canonical_root=project.root,
            owner_instance_id=generation,
            approval_id=intent.record.approval_id,
        )
        first = store.request_stop(
            ExecutionStopRequest(
                **scope.model_dump(), request_id=uuid4(), manifest_digest=manifest.fingerprint
            ),
            deadline=time.monotonic() + 5,
        )
        capture = GuardedOutputCollector(manifest.profile.output_bytes)
        capture.accept("stdout", b"safe prefix\n")
        snapshot = capture.seal(abort=True)
        digest_helper = DockerCompletion(
            owned=owned,
            outcome=DockerJobOutcome(
                exit_code=7, timed_out=False, outcome_unknown=False, cleanup_verified=True
            ),
            output=snapshot,
        )
        observation = ExecutionStopObservation(
            scope=scope,
            request_id=first.request_id,
            manifest_digest=manifest.fingerprint,
            owned=owned,
            terminal=ExecutionStopTerminal(
                outcome="cancelled",
                evidence="owned",
                cleanup_verified=True,
                exit_code=7,
                attempt_id=intent.attempt_id,
                ownership_digest=fingerprint(
                    {
                        "intent": intent.fingerprint,
                        "container": owned.container_id,
                        "birth": owned.created_at,
                    }
                ),
                output_facts=digest_helper.output_facts,
                output_digest=digest_helper.output_digest,
            ),
        )
        bad = snapshot.model_copy(update={"stdout": "fake prefix\n"})
        with pytest.raises((ReviewFault, ValueError)):
            store.finish_stop_with_capture(observation, bad, deadline=time.monotonic() + 5)
        assert store.inspect_stop(scope, deadline=time.monotonic() + 5) == first
        final = store.finish_stop_with_capture(observation, snapshot, deadline=time.monotonic() + 5)
        assert final.terminal == observation.terminal
        assert (
            store.finish_stop_with_capture(observation, snapshot, deadline=time.monotonic() + 5)
            == final
        )
        inspected = store.inspect_execution(scope, deadline=time.monotonic() + 5)
        assert inspected.completion_state == "stop_recorded" and inspected.result is None
        assert inspected.stop is not None and inspected.stop.outcome == "cancelled"
        assert (
            inspected.stop.payload_state == "retained" and inspected.stop.output_facts is not None
        )
        assert inspected.stop.output_facts.aborted
        assert inspected.stop_output == snapshot
        with closing(sqlite3.connect(store.owner.store.layout.database)) as sql:
            assert sql.execute("SELECT count(*) FROM execution_results").fetchone() == (0,)
            assert sql.execute("SELECT count(*) FROM execution_stop_payloads").fetchone() == (1,)


def test_cancel_submission_resolves_indexed_scope_without_cwd_and_excludes_uuid_reuse(
    tmp_path: Path,
) -> None:
    from concurrent.futures import Future

    from jeff.application.execution import ExecutionCancelCall

    with authority_store(tmp_path) as (store, project, manifest, generation):
        proposed = store.authority(
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
                approval_id=proposed.approval_id,
                current_manifest_digest=manifest.fingerprint,
            ),
            deadline=time.monotonic() + 5,
        )
        call = ExecutionCancelCall(
            request_id=uuid4(),
            approval_id=proposed.approval_id,
            reviewed_manifest_digest=manifest.fingerprint,
        )
        future = store.submit_cancel(call, deadline=time.monotonic() + 5)
        assert isinstance(future, Future)
        first = future.result(timeout=5)
        assert (
            first.scope.project_id == project.project_id
            and first.scope.canonical_root == project.root
        )
        assert first.terminal is not None and first.terminal.evidence == "not_started"
        assert store.submit_cancel(call, deadline=time.monotonic() + 5).result(timeout=5) == first
        with pytest.raises(ReviewFault):
            store.submit_cancel(
                call.model_copy(update={"reviewed_manifest_digest": "0" * 64}),
                deadline=time.monotonic() + 5,
            ).result(timeout=5)
        with pytest.raises(ReviewFault):
            store.authority(
                command(
                    project,
                    generation,
                    operation="claim",
                    request_id=call.request_id,
                    approval_id=proposed.approval_id,
                    current_manifest_digest=manifest.fingerprint,
                ),
                deadline=time.monotonic() + 5,
            )
