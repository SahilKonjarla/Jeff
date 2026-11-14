"""Fresh internal authority through real SQLite; no Docker or input-verification claims."""

import os
import sqlite3
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from jeff.application.execution import (
    ActionManifest,
    AuthorityOperation,
    ExecutionCall,
    ExecutionManifest,
    ExecutionProfile,
    PreparedInputs,
    ReviewFault,
    ReviewOperation,
)
from jeff.application.runtime import RuntimeLayout
from jeff.domain.projects import Project
from jeff.infrastructure.runtime_state import RuntimeStateStore
from jeff.infrastructure.runtime_storage import SQLiteStorage


@contextmanager
def authority_store(
    root: Path, clock: dict[str, int] | None = None
) -> Iterator[tuple[SQLiteStorage, Project, ExecutionManifest, UUID]]:
    layout = RuntimeLayout(data_dir=root / "d", runtime_dir=root / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    project, generation = Project(uuid4(), root / "project"), uuid4()
    profile = ExecutionProfile(
        name="python",
        project_id=project.project_id,
        canonical_root=project.root,
        engine_id="synthetic-no-engine-query",
        endpoint="unix:///synthetic/nonexistent.sock",
        image_id="sha256:" + "1" * 64,
        platform="linux/arm64",
        toolchain="synthetic",
        executable="/usr/local/bin/python",
    )
    manifest = ExecutionManifest(
        profile=profile, argv=(profile.executable, "-V"), inputs=PreparedInputs()
    )
    with owner:
        owner.store.credential(owner)
        storage = SQLiteStorage(
            owner,
            instance_id=generation,
            review_clock=(lambda: clock["wall"]) if clock is not None else None,
            review_monotonic=(lambda: float(clock["mono"])) if clock is not None else None,
        )
        storage.start()
        try:
            assert storage.wait_ready(5).state == "ready"
            storage.index(project)
            yield storage, project, manifest, generation
        finally:
            storage.close(timeout=5)


def command(project: Project, generation: UUID, **values: object) -> AuthorityOperation:
    return AuthorityOperation.model_validate(
        {
            "project_id": project.project_id,
            "canonical_root": project.root,
            "owner_instance_id": generation,
            **values,
        }
    )


def test_fresh_approval_is_consumed_once_and_claim_capability_is_never_replayed(
    tmp_path: Path,
) -> None:
    with authority_store(tmp_path) as (store, project, manifest, generation):
        proposal = command(
            project,
            generation,
            operation="propose",
            manifest=manifest,
            current_manifest_digest=manifest.fingerprint,
        )
        pending = store.authority(proposal, deadline=time.monotonic() + 5)
        assert pending.record.state == "pending" and pending.launch_claim is None
        assert store.authority(proposal, deadline=time.monotonic() + 5) == pending
        decision = command(
            project,
            generation,
            operation="approve",
            approval_id=pending.record.approval_id,
            current_manifest_digest=manifest.fingerprint,
        )
        approved = store.authority(decision, deadline=time.monotonic() + 5)
        assert approved.record.state == "approved" and approved.launch_claim is None
        claim = command(
            project,
            generation,
            operation="claim",
            approval_id=pending.record.approval_id,
            current_manifest_digest=manifest.fingerprint,
        )
        first = store.authority(claim, deadline=time.monotonic() + 5)
        assert first.record.state == "consumed" and first.launch_claim == first.record.claim_id
        assert first.launch_claim is not None
        repeated = store.authority(claim, deadline=time.monotonic() + 5)
        assert repeated.record == first.record and repeated.launch_claim is None
        shown = store.authority(
            command(project, generation, operation="show", approval_id=pending.record.approval_id),
            deadline=time.monotonic() + 5,
        )
        assert shown.record == first.record and shown.launch_claim is None


def test_legacy_reviews_and_request_ids_cannot_become_authority_and_concurrent_claim_is_unique(
    tmp_path: Path,
) -> None:
    with authority_store(tmp_path) as (store, project, manifest, generation):
        legacy_manifest = ActionManifest(profile=manifest.profile, argv=manifest.argv)
        legacy_call = ExecutionCall(directory=project.root, operation="propose", argv=manifest.argv)
        legacy = ReviewOperation(
            call=legacy_call,
            project_id=project.project_id,
            canonical_root=project.root,
            owner_instance_id=generation,
            manifest=legacy_manifest,
            configuration_fingerprint=manifest.profile.fingerprint,
        )
        old = store.review(legacy, deadline=time.monotonic() + 5).items[0]
        with pytest.raises(ReviewFault) as collision:
            store.authority(
                command(
                    project,
                    generation,
                    operation="propose",
                    request_id=legacy_call.request_id,
                    manifest=manifest,
                    current_manifest_digest=manifest.fingerprint,
                ),
                deadline=time.monotonic() + 5,
            )
        assert collision.value.code == "execution_conflict"
        with pytest.raises(ReviewFault) as old_approval:
            store.authority(
                command(
                    project,
                    generation,
                    operation="approve",
                    approval_id=old.approval_id,
                    current_manifest_digest=manifest.fingerprint,
                ),
                deadline=time.monotonic() + 5,
            )
        assert old_approval.value.code == "execution_not_found"
        proposal = command(
            project,
            generation,
            operation="propose",
            manifest=manifest,
            current_manifest_digest=manifest.fingerprint,
        )
        fresh = store.authority(proposal, deadline=time.monotonic() + 5).record
        with pytest.raises(ReviewFault) as reverse:
            store.review(
                legacy.model_copy(
                    update={
                        "call": legacy_call.model_copy(update={"request_id": proposal.request_id})
                    }
                ),
                deadline=time.monotonic() + 5,
            )
        assert reverse.value.code == "execution_conflict"
        store.authority(
            command(
                project,
                generation,
                operation="approve",
                approval_id=fresh.approval_id,
                current_manifest_digest=manifest.fingerprint,
            ),
            deadline=time.monotonic() + 5,
        )
        other = Project(uuid4(), tmp_path / "other")
        store.index(other)
        with pytest.raises(ReviewFault) as scope:
            store.authority(
                command(
                    other,
                    generation,
                    operation="claim",
                    approval_id=fresh.approval_id,
                    current_manifest_digest=manifest.fingerprint,
                ),
                deadline=time.monotonic() + 5,
            )
        assert scope.value.code == "execution_not_found"

        def claim_once() -> UUID | str | None:
            try:
                return store.authority(
                    command(
                        project,
                        generation,
                        operation="claim",
                        approval_id=fresh.approval_id,
                        current_manifest_digest=manifest.fingerprint,
                    ),
                    deadline=time.monotonic() + 5,
                ).launch_claim
            except ReviewFault as error:
                return error.code

        with ThreadPoolExecutor(max_workers=8) as callers:
            outcomes = list(callers.map(lambda _: claim_once(), range(8)))
        assert sum(isinstance(item, UUID) for item in outcomes) == 1
        assert outcomes.count("execution_conflict") == 7
        old_now = store.review(
            ReviewOperation(
                call=ExecutionCall(
                    directory=project.root, operation="approval.show", object_id=old.approval_id
                ),
                project_id=project.project_id,
                canonical_root=project.root,
                owner_instance_id=generation,
            ),
            deadline=time.monotonic() + 5,
        ).items[0]
        assert old_now.launch_authority_issued is False and old_now.execution_available is False


def test_expiry_changed_binding_and_clock_rollback_never_grant_claim(tmp_path: Path) -> None:
    clock = {"wall": 1000000, "mono": 1000}
    with authority_store(tmp_path, clock) as (store, project, manifest, generation):
        first = store.authority(
            command(
                project,
                generation,
                operation="propose",
                manifest=manifest,
                current_manifest_digest=manifest.fingerprint,
            ),
            deadline=time.monotonic() + 5,
        ).record
        clock["wall"] += 600000
        clock["mono"] += 600
        with pytest.raises(ReviewFault) as expired:
            store.authority(
                command(
                    project,
                    generation,
                    operation="approve",
                    approval_id=first.approval_id,
                    current_manifest_digest=manifest.fingerprint,
                ),
                deadline=time.monotonic() + 5,
            )
        assert expired.value.code == "execution_expired"
        changed = store.authority(
            command(
                project,
                generation,
                operation="propose",
                manifest=manifest,
                current_manifest_digest=manifest.fingerprint,
            ),
            deadline=time.monotonic() + 5,
        ).record
        with pytest.raises(ReviewFault) as mismatch:
            store.authority(
                command(
                    project,
                    generation,
                    operation="approve",
                    approval_id=changed.approval_id,
                    current_manifest_digest="f" * 64,
                ),
                deadline=time.monotonic() + 5,
            )
        assert mismatch.value.code == "execution_target_mismatch"
        last = store.authority(
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
                approval_id=last.approval_id,
                current_manifest_digest=manifest.fingerprint,
            ),
            deadline=time.monotonic() + 5,
        )
        clock["wall"] -= 1
        with pytest.raises(ReviewFault) as backwards:
            store.authority(
                command(
                    project,
                    generation,
                    operation="claim",
                    approval_id=last.approval_id,
                    current_manifest_digest=manifest.fingerprint,
                ),
                deadline=time.monotonic() + 5,
            )
        assert backwards.value.code == "execution_clock"
        shown = store.authority(
            command(project, generation, operation="show", approval_id=last.approval_id),
            deadline=time.monotonic() + 5,
        )
        assert shown.record.state == "invalidated" and shown.launch_claim is None


def test_restart_invalidates_unused_authority_and_quarantines_unknown_claim(tmp_path: Path) -> None:
    with authority_store(tmp_path) as (store, project, manifest, generation):
        first = store.authority(
            command(
                project,
                generation,
                operation="propose",
                manifest=manifest,
                current_manifest_digest=manifest.fingerprint,
            ),
            deadline=time.monotonic() + 5,
        ).record
        second = store.authority(
            command(
                project,
                generation,
                operation="propose",
                manifest=manifest,
                current_manifest_digest=manifest.fingerprint,
            ),
            deadline=time.monotonic() + 5,
        ).record
        for record in (first, second):
            store.authority(
                command(
                    project,
                    generation,
                    operation="approve",
                    approval_id=record.approval_id,
                    current_manifest_digest=manifest.fingerprint,
                ),
                deadline=time.monotonic() + 5,
            )
        store.authority(
            command(
                project,
                generation,
                operation="claim",
                approval_id=first.approval_id,
                current_manifest_digest=manifest.fingerprint,
            ),
            deadline=time.monotonic() + 5,
        )
        with pytest.raises(ReviewFault) as occupied:
            store.authority(
                command(
                    project,
                    generation,
                    operation="claim",
                    approval_id=second.approval_id,
                    current_manifest_digest=manifest.fingerprint,
                ),
                deadline=time.monotonic() + 5,
            )
        assert occupied.value.code == "execution_capacity"
        store.close(timeout=5)
        new_generation = uuid4()
        again = SQLiteStorage(store.owner, instance_id=new_generation)
        again.start()
        try:
            assert again.wait_ready(5).state == "ready"
            old = again.authority(
                command(project, new_generation, operation="show", approval_id=first.approval_id),
                deadline=time.monotonic() + 5,
            )
            assert old.record.state == "consumed" and old.record.claim_state == "unknown"
            assert old.launch_claim is None
            unused = again.authority(
                command(project, new_generation, operation="show", approval_id=second.approval_id),
                deadline=time.monotonic() + 5,
            )
            assert unused.record.state == "invalidated" and unused.launch_claim is None
            fresh = again.authority(
                command(
                    project,
                    new_generation,
                    operation="propose",
                    manifest=manifest,
                    current_manifest_digest=manifest.fingerprint,
                ),
                deadline=time.monotonic() + 5,
            ).record
            again.authority(
                command(
                    project,
                    new_generation,
                    operation="approve",
                    approval_id=fresh.approval_id,
                    current_manifest_digest=manifest.fingerprint,
                ),
                deadline=time.monotonic() + 5,
            )
            with pytest.raises(ReviewFault) as quarantined:
                again.authority(
                    command(
                        project,
                        new_generation,
                        operation="claim",
                        approval_id=fresh.approval_id,
                        current_manifest_digest=manifest.fingerprint,
                    ),
                    deadline=time.monotonic() + 5,
                )
            assert quarantined.value.code == "execution_outcome_unknown"
        finally:
            again.close(timeout=5)


def test_clean_schema5_migration_preserves_registration_settings_and_legacy_review(
    tmp_path: Path,
) -> None:
    with authority_store(tmp_path) as (store, project, manifest, generation):
        layout = store.owner.store.layout
        legacy_manifest = ActionManifest(profile=manifest.profile, argv=manifest.argv)
        legacy = store.review(
            ReviewOperation(
                call=ExecutionCall(directory=project.root, operation="propose", argv=manifest.argv),
                project_id=project.project_id,
                canonical_root=project.root,
                owner_instance_id=generation,
                manifest=legacy_manifest,
                configuration_fingerprint=manifest.profile.fingerprint,
            ),
            deadline=time.monotonic() + 5,
        ).items[0]
        store.close(timeout=5)
        with closing(sqlite3.connect(layout.database)) as fixture, fixture:
            fixture.execute("DROP TABLE execution_authority_operations")
            fixture.execute("DROP TABLE execution_authorities")
            fixture.execute("DELETE FROM schema_migrations WHERE version=6")
            fixture.execute("PRAGMA user_version=5")
        settings_before = layout.settings.read_bytes()
        new_generation = uuid4()
        migrated = SQLiteStorage(store.owner, instance_id=new_generation)
        migrated.start()
        try:
            assert migrated.wait_ready(5).schema_version == 6
            assert migrated.lookup(project) == project
            assert layout.settings.read_bytes() == settings_before
            observed = migrated.review(
                ReviewOperation(
                    call=ExecutionCall(
                        directory=project.root,
                        operation="approval.show",
                        object_id=legacy.approval_id,
                    ),
                    project_id=project.project_id,
                    canonical_root=project.root,
                    owner_instance_id=new_generation,
                ),
                deadline=time.monotonic() + 5,
            ).items[0]
            assert observed.state == "invalidated" and observed.launch_authority_issued is False
            with pytest.raises(ReviewFault) as still_not_authority:
                migrated.authority(
                    command(
                        project,
                        new_generation,
                        operation="approve",
                        approval_id=legacy.approval_id,
                        current_manifest_digest=manifest.fingerprint,
                    ),
                    deadline=time.monotonic() + 5,
                )
            assert still_not_authority.value.code == "execution_not_found"
        finally:
            migrated.close(timeout=5)
