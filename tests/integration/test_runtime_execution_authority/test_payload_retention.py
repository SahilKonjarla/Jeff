"""Shared known/quarantined payload slots, bounded age and unavailable/tampered text."""

import sqlite3
import time
from contextlib import closing
from pathlib import Path

import pytest

from jeff.application.execution import ReviewFault
from jeff.application.storage import StorageFault
from jeff.infrastructure.runtime_storage import SQLiteStorage

from .support_authority import authority_store
from .support_launch import completion, fact, launch_intent, launch_scope, stopped_sample


def test_stop_and_legacy_payloads_share_sixteen_known_slots_without_backfilling_metadata(
    tmp_path: Path,
) -> None:
    with authority_store(tmp_path) as (store, project, manifest, generation):
        first = None
        for index in range(17):
            if index % 2:
                stopped_sample(store, project, manifest, generation)
            else:
                intent = launch_intent(store, project, manifest, generation)
                for phase in ("create_intent", "created", "start_intent", "exited", "cleaned"):
                    store.record(fact(intent, phase), deadline=time.monotonic() + 5)
                store.complete_execution(completion(intent), deadline=time.monotonic() + 5)
                if first is None:
                    first = intent
        assert first is not None
        oldest = store.execution_result(launch_scope(first), deadline=time.monotonic() + 5)
        assert oldest.payload_state == "retired" and oldest.retirement_reason == "capacity"
        with closing(sqlite3.connect(store.owner.store.layout.database)) as sql:
            assert sql.execute(
                "SELECT (SELECT count(*) FROM execution_results WHERE stdout IS NOT NULL) + "
                "(SELECT count(*) FROM execution_stop_payloads WHERE stdout IS NOT NULL)"
            ).fetchone() == (16,)
        metadata_only, sample = stopped_sample(store, project, manifest, generation, retain=False)
        inspection = store.inspect_execution(metadata_only.scope, deadline=time.monotonic() + 5)
        assert inspection.stop is not None and inspection.stop.payload_state == "missing"
        with pytest.raises(ReviewFault):
            store.finish_stop_with_capture(
                sample.observation, sample.output, deadline=time.monotonic() + 5
            )
        assert (
            store.inspect_stop(metadata_only.scope, deadline=time.monotonic() + 5) == metadata_only
        )


@pytest.mark.parametrize("kind", ("age", "clock", "tamper"))
def test_stop_archive_age_clock_and_tampering_never_expose_unavailable_text(
    tmp_path: Path, kind: str
) -> None:
    clock = {"wall": 100000000, "mono": 1000.0}
    with authority_store(
        tmp_path,
        retention_clock=lambda: int(clock["wall"]),
        retention_monotonic=lambda: clock["mono"],
    ) as (store, project, manifest, generation):
        record, sample = stopped_sample(store, project, manifest, generation)
        original = store.inspect_execution(record.scope, deadline=time.monotonic() + 5)
        assert original.stop_output == sample.output and original.stop is not None
        if kind == "age":
            clock["wall"] += 31 * 86400000
            clock["mono"] += 31 * 86400
        elif kind == "clock":
            clock["wall"] -= 1
        else:
            store.close(timeout=5)
            with closing(sqlite3.connect(store.owner.store.layout.database)) as sql, sql:
                sql.execute("UPDATE execution_stop_payloads SET stdout='evil prefix' || char(10)")
            reopened = SQLiteStorage(store.owner, instance_id=generation)
            reopened.start()
            try:
                assert reopened.wait_ready(5).state == "ready"
                with pytest.raises(ReviewFault) as altered:
                    reopened.inspect_execution(record.scope, deadline=time.monotonic() + 5)
                assert altered.value.code == "storage_schema_unsupported"
            finally:
                reopened.close(timeout=5)
            return
        inspected = store.inspect_execution(record.scope, deadline=time.monotonic() + 5)
        assert inspected.stop is not None
        assert inspected.stop.payload_state == ("expired" if kind == "age" else "clock_unconfirmed")
        assert (
            inspected.stop_output is None
            and inspected.stop.output_facts == original.stop.output_facts
        )
        if kind == "age":
            store.retire(deadline=time.monotonic() + 5)
            retired = store.inspect_execution(record.scope, deadline=time.monotonic() + 5)
            assert retired.stop is not None and retired.stop.payload_state == "retired"
            assert retired.stop.retirement_reason == "age" and retired.stop_output is None


def test_output_retention_caps_text_then_expires_quarantine_without_releasing_it(
    tmp_path: Path,
) -> None:
    clock = [10000000000]
    with authority_store(tmp_path, retention_clock=lambda: clock[0]) as (
        store,
        project,
        manifest,
        generation,
    ):
        intents = []
        for index in range(18):
            intent = launch_intent(store, project, manifest, generation)
            intents.append(intent)
            for phase in ("create_intent", "created", "start_intent", "exited", "cleaned"):
                store.record(fact(intent, phase), deadline=time.monotonic() + 5)
            text = b"safe\n" * 10000 if index == 1 else b"public\n"
            result = store.complete_execution(
                completion(intent, text, unknown=index == 17), deadline=time.monotonic() + 5
            )
            assert result.disposition == ("quarantined" if index == 17 else "completed")
        first = store.execution_result(launch_scope(intents[0]), deadline=time.monotonic() + 5)
        assert (
            first.payload_state == "retired"
            and first.retirement_reason == "capacity"
            and first.output is None
        )
        retained = store.execution_result(launch_scope(intents[1]), deadline=time.monotonic() + 5)
        assert retained.output is not None and retained.output.retained_bytes == 50000
        clock[0] -= 1
        assert (
            store.execution_result(
                launch_scope(intents[1]), deadline=time.monotonic() + 5
            ).payload_state
            == "clock_unconfirmed"
        )
        with pytest.raises(StorageFault):
            store.retire(deadline=time.monotonic() + 5)
        clock[0] += 1 + 31 * 86400000
        expired = store.execution_result(launch_scope(intents[-1]), deadline=time.monotonic() + 5)
        assert expired.payload_state == "expired" and expired.output is None
        batch = store.retire(deadline=time.monotonic() + 5)
        assert batch.has_more
        states = [
            store.execution_result(launch_scope(item), deadline=time.monotonic() + 5).payload_state
            for item in intents[1:]
        ]
        assert states.count("retired") == 4 and states.count("expired") == 13
        for _ in range(4):
            store.retire(deadline=time.monotonic() + 5)
        for intent in intents[1:]:
            final = store.execution_result(launch_scope(intent), deadline=time.monotonic() + 5)
            assert (
                final.payload_state == "retired"
                and final.retirement_reason == "age"
                and final.output is None
            )
            assert final.output_facts.retained_bytes > 0 and final.output_digest
        assert (
            store.launch_status(
                launch_scope(intents[-1]), deadline=time.monotonic() + 5
            ).claim_state
            == "unknown"
        )
        with pytest.raises(ReviewFault):
            launch_intent(store, project, manifest, generation)
