"""Fresh single-use authority, legacy/scope exclusion and expiry/clock safety."""

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from jeff.application.execution import ActionManifest, ExecutionCall, ReviewFault, ReviewOperation
from jeff.domain.projects import Project

from .support_authority import authority_store, command


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
