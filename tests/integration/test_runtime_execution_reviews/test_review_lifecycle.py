"""Durable review-only approval, restart invalidation, bounded clocks and concurrent scope."""

import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest

from jeff.application.execution import (
    ExecutionCall,
    ExecutionProfile,
    ReviewFault,
    ReviewOperation,
    build_manifest,
)
from jeff.application.runtime import RuntimeLayout
from jeff.domain.projects import Project
from jeff.infrastructure.runtime_state import RuntimeStateStore
from jeff.infrastructure.runtime_storage import SQLiteStorage

from .support_review import operation, review_store


def test_review_approval_is_durable_deduplicated_and_invalidated_on_restart(tmp_path: Path) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    project = Project(uuid4(), tmp_path / "project")
    generation = uuid4()
    profile = ExecutionProfile(
        name="default",
        project_id=project.project_id,
        canonical_root=project.root,
        engine_id="synthetic-engine",
        endpoint="unix:///private/tmp/synthetic.sock",
        image_id="sha256:" + "1" * 64,
        platform="linux/arm64",
        toolchain="Python 3.12",
        executable="/usr/local/bin/python",
    )
    manifest = build_manifest(project, profile, ("python", "-c", "print(42)"))
    with owner:
        owner.store.credential(owner)
        storage = SQLiteStorage(owner, instance_id=generation)
        storage.start()
        try:
            assert storage.wait_ready(5).state == "ready"
            storage.index(project)
            call = ExecutionCall(directory=project.root, operation="propose", argv=manifest.argv)
            operation = ReviewOperation(
                call=call,
                project_id=project.project_id,
                canonical_root=project.root,
                owner_instance_id=generation,
                manifest=manifest,
                configuration_fingerprint=profile.fingerprint,
            )
            proposed = storage.review(operation, deadline=time.monotonic() + 5)
            assert proposed == storage.review(operation, deadline=time.monotonic() + 5)
            record = proposed.items[0]
            assert record.state == "pending" and record.job_state == "waiting_for_approval"
            decision = ReviewOperation(
                call=ExecutionCall(
                    directory=project.root, operation="approve", object_id=record.approval_id
                ),
                project_id=project.project_id,
                canonical_root=project.root,
                owner_instance_id=generation,
                configuration_fingerprint=profile.fingerprint,
            )
            approved = storage.review(decision, deadline=time.monotonic() + 5)
            assert approved.items[0].state == "approved"
            assert approved.items[0].job_state == "blocked"
            assert approved.items[0].launch_authority_issued is False
            assert approved == storage.review(decision, deadline=time.monotonic() + 5)
        finally:
            storage.close()
        new_generation = uuid4()
        restarted = SQLiteStorage(owner, instance_id=new_generation)
        restarted.start()
        try:
            assert restarted.wait_ready(5).state == "ready"
            query = ReviewOperation(
                call=ExecutionCall(
                    directory=project.root, operation="job.show", object_id=record.job_id
                ),
                project_id=project.project_id,
                canonical_root=project.root,
                owner_instance_id=new_generation,
            )
            retained = restarted.review(query, deadline=time.monotonic() + 5)
            assert retained.items[0].state == "invalidated"
            assert retained.items[0].execution_available is False
        finally:
            restarted.close()


def test_expiry_and_clock_rollback_never_extend_review(tmp_path: Path) -> None:
    clock = {"wall": 1000000.0, "mono": 100.0}
    with review_store(tmp_path, clock) as (store, project, profile, generation):
        proposed = store.review(
            operation(
                project,
                generation,
                ExecutionCall(directory=project.root, operation="propose", argv=("python", "-V")),
                profile,
            ),
            deadline=time.monotonic() + 5,
        ).items[0]
        clock.update(wall=1600001.0, mono=700.001)
        expired = store.review(
            operation(
                project,
                generation,
                ExecutionCall(
                    directory=project.root,
                    operation="approval.show",
                    object_id=proposed.approval_id,
                ),
            ),
            deadline=time.monotonic() + 5,
        ).items[0]
        assert expired.state == "expired" and expired.expires_at_ms == proposed.expires_at_ms
        next_review = store.review(
            operation(
                project,
                generation,
                ExecutionCall(directory=project.root, operation="propose", argv=("python", "-V")),
                profile,
            ),
            deadline=time.monotonic() + 5,
        ).items[0]
        clock["wall"] -= 1
        with pytest.raises(ReviewFault) as caught:
            store.review(
                operation(
                    project,
                    generation,
                    ExecutionCall(
                        directory=project.root,
                        operation="approve",
                        object_id=next_review.approval_id,
                    ),
                    profile,
                ),
                deadline=time.monotonic() + 5,
            )
        assert caught.value.code == "execution_clock"
        invalid = store.review(
            operation(
                project,
                generation,
                ExecutionCall(
                    directory=project.root,
                    operation="approval.show",
                    object_id=next_review.approval_id,
                ),
            ),
            deadline=time.monotonic() + 5,
        ).items[0]
        assert invalid.state == "invalidated" and invalid.launch_authority_issued is False


def test_concurrent_decisions_have_one_winner_and_scope_is_not_transferable(tmp_path: Path) -> None:
    with review_store(tmp_path) as (store, project, profile, generation):
        record = store.review(
            operation(
                project,
                generation,
                ExecutionCall(directory=project.root, operation="propose", argv=("python", "-V")),
                profile,
            ),
            deadline=time.monotonic() + 5,
        ).items[0]

        def decide(choice: str) -> str:
            from typing import cast

            from jeff.application.execution import ExecutionOperation

            try:
                return (
                    store.review(
                        operation(
                            project,
                            generation,
                            ExecutionCall(
                                directory=project.root,
                                operation=cast(ExecutionOperation, choice),
                                object_id=record.approval_id,
                            ),
                            profile,
                        ),
                        deadline=time.monotonic() + 5,
                    )
                    .items[0]
                    .state.value
                )
            except ReviewFault as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(decide, ("approve", "deny")))
        assert results.count("execution_conflict") == 1
        assert sum(value in ("approved", "denied") for value in results) == 1
        second = Project(uuid4(), tmp_path / "other")
        store.index(second)
        with pytest.raises(ReviewFault) as caught:
            store.review(
                operation(
                    second,
                    generation,
                    ExecutionCall(
                        directory=second.root, operation="job.show", object_id=record.job_id
                    ),
                ),
                deadline=time.monotonic() + 5,
            )
        assert caught.value.code == "execution_not_found"


def test_changed_configuration_invalidates_without_granting(tmp_path: Path) -> None:
    with review_store(tmp_path) as (store, project, profile, generation):
        record = store.review(
            operation(
                project,
                generation,
                ExecutionCall(directory=project.root, operation="propose", argv=("python", "-V")),
                profile,
            ),
            deadline=time.monotonic() + 5,
        ).items[0]
        changed = profile.model_copy(update={"toolchain": "Changed declared toolchain"})
        with pytest.raises(ReviewFault) as caught:
            store.review(
                operation(
                    project,
                    generation,
                    ExecutionCall(
                        directory=project.root, operation="approve", object_id=record.approval_id
                    ),
                    changed,
                ),
                deadline=time.monotonic() + 5,
            )
        assert caught.value.code == "execution_target_mismatch"
        current = store.review(
            operation(
                project,
                generation,
                ExecutionCall(
                    directory=project.root, operation="job.show", object_id=record.job_id
                ),
            ),
            deadline=time.monotonic() + 5,
        ).items[0]
        assert current.state == "invalidated"
