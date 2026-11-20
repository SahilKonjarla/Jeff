"""Exact journal identity, one competing attempt and facts-only uncertain cleanup/replies."""

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from jeff.application.execution import DockerBackendFault, DockerLaunchEvent, ReviewFault

from .support_authority import authority_store
from .support_launch import fact, launch_intent, launch_scope


def test_journal_preserves_normal_facts_without_premature_admission_release(tmp_path: Path) -> None:
    with authority_store(tmp_path) as (store, project, manifest, generation):
        intent = launch_intent(store, project, manifest, generation)
        for phase in ("create_intent", "created", "start_intent", "exited", "cleaned"):
            store.record(fact(intent, phase), deadline=time.monotonic() + 5)
        observed = store.launch_status(launch_scope(intent), deadline=time.monotonic() + 5)
        assert observed.phases == ("create_intent", "created", "start_intent", "exited", "cleaned")
        assert observed.container_id == "e" * 64 and observed.exit_code == 7
        assert not observed.uncertain and observed.claim_state == "claimed"
        # These are persisted observations, not a live handle or a completion/output receipt.
        assert not hasattr(observed, "launch_claim")


def test_journal_competing_attempts_and_changed_bindings_cannot_reserve_twice(
    tmp_path: Path,
) -> None:
    with authority_store(tmp_path) as (store, project, manifest, generation):
        intent = launch_intent(store, project, manifest, generation)

        def reserve(index: int) -> UUID | str:
            candidate = intent.model_copy(update={"attempt_id": uuid4(), "nonce": f"{index:032x}"})
            try:
                store.record(fact(candidate, "create_intent"), deadline=time.monotonic() + 5)
                return candidate.attempt_id
            except DockerBackendFault as error:
                return error.code

        with ThreadPoolExecutor(max_workers=8) as callers:
            outcomes = list(callers.map(reserve, range(8)))
        winners = [value for value in outcomes if isinstance(value, UUID)]
        assert len(winners) == 1
        snapshot = store.launch_status(launch_scope(intent), deadline=time.monotonic() + 5)
        assert snapshot.intent.attempt_id == winners[0] and snapshot.phases == ("create_intent",)
        for changed in (
            snapshot.intent.model_copy(update={"nonce": "f" * 32}),
            snapshot.intent.model_copy(
                update={
                    "inputs": snapshot.intent.inputs.model_copy(
                        update={"path": tmp_path / "other-input"}
                    )
                }
            ),
        ):
            with pytest.raises(DockerBackendFault):
                store.record(fact(changed, "created"), deadline=time.monotonic() + 5)
        with pytest.raises(DockerBackendFault):
            store.launch_status(
                launch_scope(intent).model_copy(update={"project_id": uuid4()}),
                deadline=time.monotonic() + 5,
            )


def test_journal_original_expiry_and_clock_health_gate_start_but_not_cleanup(
    tmp_path: Path,
) -> None:
    for index, mode in enumerate(("wall", "mono", "rollback")):
        clock = {"wall": 1000000, "mono": 1000}
        with authority_store(tmp_path / str(index), clock) as (
            store,
            project,
            manifest,
            generation,
        ):
            intent = launch_intent(store, project, manifest, generation)
            for phase in ("create_intent", "created"):
                store.record(fact(intent, phase), deadline=time.monotonic() + 5)
            if mode == "wall":
                clock["wall"] += 600001
            elif mode == "mono":
                clock["mono"] += 601
            else:
                clock["wall"] -= 1
            with pytest.raises(DockerBackendFault):
                store.record(fact(intent, "start_intent"), deadline=time.monotonic() + 5)
            store.record(fact(intent, "unknown"), deadline=time.monotonic() + 5)
            store.record(fact(intent, "cleaned"), deadline=time.monotonic() + 5)
            snapshot = store.launch_status(launch_scope(intent), deadline=time.monotonic() + 5)
            assert snapshot.uncertain and snapshot.claim_state == "unknown"
            assert "start_intent" not in snapshot.phases and "cleaned" in snapshot.phases


def test_journal_partial_identity_and_owned_unknown_cleanup_remain_distinct(tmp_path: Path) -> None:
    with authority_store(tmp_path / "p") as (store, project, manifest, generation):
        intent = launch_intent(store, project, manifest, generation)
        partial = fact(intent, "unknown").model_copy(update={"observed_container_id": "e" * 64})
        store.record(partial, deadline=time.monotonic() + 5)
        snapshot = store.launch_status(launch_scope(intent), deadline=time.monotonic() + 5)
        assert snapshot.observed_container_id == "e" * 64 and snapshot.container_id is None
        with pytest.raises(DockerBackendFault):
            store.record(fact(intent, "cleaned"), deadline=time.monotonic() + 5)
        with pytest.raises(DockerBackendFault):
            store.record(
                partial.model_copy(update={"observed_container_id": "f" * 64}),
                deadline=time.monotonic() + 5,
            )
        assert store.launch_status(launch_scope(intent), deadline=time.monotonic() + 5) == snapshot
    with authority_store(tmp_path / "o") as (store, project, manifest, generation):
        intent = launch_intent(store, project, manifest, generation)
        for phase in ("create_intent", "created", "start_intent", "exited", "unknown", "cleaned"):
            store.record(fact(intent, phase), deadline=time.monotonic() + 5)
        snapshot = store.launch_status(launch_scope(intent), deadline=time.monotonic() + 5)
        assert snapshot.container_id == "e" * 64 and snapshot.exit_code == 7 and snapshot.uncertain
        for changes in (
            {"container_id": "f" * 64},
            {"created_at": "2026-02-01T00:00:00.000000000Z"},
        ):
            with pytest.raises(DockerBackendFault):
                store.record(
                    fact(intent, "cleaned").model_copy(update=changes),
                    deadline=time.monotonic() + 5,
                )
        assert store.launch_status(launch_scope(intent), deadline=time.monotonic() + 5) == snapshot


def test_journal_postcommit_reply_loss_never_replays_mutation_or_releases_admission(
    tmp_path: Path,
) -> None:
    for index, lost in enumerate(("create_intent", "start_intent", "cleaned")):
        with authority_store(tmp_path / str(index)) as (store, project, manifest, generation):
            intent = launch_intent(store, project, manifest, generation)
            phases = ("create_intent", "created", "start_intent", "exited", "cleaned")
            for phase in phases[: phases.index(lost)]:
                store.record(fact(intent, phase), deadline=time.monotonic() + 5)

            class LostReply:
                def record(self, event: DockerLaunchEvent, *, deadline: float) -> None:
                    store.record(
                        event, deadline=deadline
                    )  # Real commit, then synthetic transport loss.
                    raise DockerBackendFault("docker_journal_unconfirmed")

            with pytest.raises(DockerBackendFault):
                LostReply().record(fact(intent, lost), deadline=time.monotonic() + 5)
            if lost in {"create_intent", "start_intent"}:
                with pytest.raises(DockerBackendFault):
                    store.record(fact(intent, lost), deadline=time.monotonic() + 5)
            snapshot = store.launch_status(launch_scope(intent), deadline=time.monotonic() + 5)
            assert lost in snapshot.phases and snapshot.claim_state == "claimed"
            with pytest.raises(ReviewFault) as occupied:
                launch_intent(store, project, manifest, generation)
            assert occupied.value.code == "execution_capacity"
            store.record(fact(intent, "unknown"), deadline=time.monotonic() + 5)
            assert store.launch_status(
                launch_scope(intent), deadline=time.monotonic() + 5
            ).uncertain
