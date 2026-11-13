"""Durable review-only facts through the single-owner storage port."""

import hashlib
import hmac
import json
import os
import sqlite3
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from jeff.application.execution import (
    ExecutionCall,
    ExecutionProfile,
    ReviewFault,
    ReviewOperation,
    build_manifest,
)
from jeff.application.runtime import RuntimeLayout
from jeff.application.storage import StorageFault
from jeff.domain.projects import Project
from jeff.infrastructure.runtime_state import Ownership, RuntimeStateStore
from jeff.infrastructure.runtime_storage import SQLiteStorage


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


@contextmanager
def review_store(
    root: Path, clock: dict[str, float] | None = None
) -> Iterator[tuple[SQLiteStorage, Project, ExecutionProfile, UUID]]:
    owner = RuntimeStateStore(
        RuntimeLayout(data_dir=root / "d", runtime_dir=root / "r", uid=os.geteuid())
    ).acquire(create=True)
    assert owner is not None
    generation = uuid4()
    project = Project(uuid4(), root / "project")
    profile = ExecutionProfile(
        name="default",
        project_id=project.project_id,
        canonical_root=project.root,
        engine_id="synthetic-engine",
        endpoint="unix:///private/tmp/synthetic.sock",
        image_id="sha256:" + "1" * 64,
        platform="linux/arm64",
        toolchain="Python",
        executable="/usr/local/bin/python",
    )
    with owner:
        owner.store.credential(owner)
        store = SQLiteStorage(
            owner,
            instance_id=generation,
            review_clock=(lambda: int(clock["wall"])) if clock is not None else None,
            review_monotonic=(lambda: clock["mono"]) if clock is not None else None,
        )
        store.start()
        try:
            assert store.wait_ready(5).state == "ready"
            store.index(project)
            yield store, project, profile, generation
        finally:
            store.close()


def operation(
    project: Project, generation: UUID, call: ExecutionCall, profile: ExecutionProfile | None = None
) -> ReviewOperation:
    return ReviewOperation(
        call=call,
        project_id=project.project_id,
        canonical_root=project.root,
        owner_instance_id=generation,
        manifest=build_manifest(project, profile, call.argv)
        if profile is not None and call.operation == "propose"
        else None,
        configuration_fingerprint=profile.fingerprint if profile is not None else None,
    )


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


@contextmanager
def schema4_registry(root: Path) -> Iterator[tuple[Ownership, Project]]:
    """Manufacture only disposable legacy state, without crashes or recovery residue."""
    layout = RuntimeLayout(data_dir=root / "d", runtime_dir=root / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    project = Project(uuid4(), root / "project")
    with owner:
        owner.store.credential(owner)
        initial = SQLiteStorage(owner)
        initial.start()
        try:
            assert initial.wait_ready(5).schema_version == 5
            initial.index(project)
        finally:
            initial.close(timeout=5)
        with closing(sqlite3.connect(layout.database)) as fixture, fixture:
            fixture.execute("DROP TABLE execution_reviews")
            fixture.execute("DROP TABLE execution_operations")
            fixture.execute("DELETE FROM schema_migrations WHERE version=5")
            fixture.execute("PRAGMA user_version=4")
        yield owner, project


def test_schema4_signed_format_and_migration_commit_retirement_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_connect, original_unlink = sqlite3.connect, os.unlink
    events: list[str] = []
    migrating: list[sqlite3.Connection] = []
    with schema4_registry(tmp_path) as (owner, project):
        layout = owner.store.layout

        class ObservedMigration(sqlite3.Connection):
            def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
                if sql.startswith("CREATE TABLE execution_reviews"):
                    raw = (layout.data_dir / "recovery.json").read_bytes()
                    published = json.loads(raw)
                    # Historical permit1/schema4 encoding, independent of current models.
                    legacy = {
                        "version": 1,
                        "schema_version": 4,
                        "nonce": published["nonce"],
                        "page_size": published["page_size"],
                        "original_pages": published["original_pages"],
                        "bindings": published["bindings"],
                    }
                    assert set(legacy["bindings"]) == {
                        "runtime",
                        "data",
                        "lock",
                        "key",
                        "database",
                        "journal",
                        "permit",
                    }
                    canonical = json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode()
                    tag = hmac.new(
                        owner.store.read_credential(),
                        b"jeff.storage.recovery.v1\0" + canonical,
                        hashlib.sha256,
                    ).hexdigest()
                    expected = json.dumps(
                        {**legacy, "mac": tag}, sort_keys=True, separators=(",", ":")
                    ).encode()
                    assert raw == expected
                    db = os.open(layout.database, os.O_RDONLY)
                    journal = os.open(str(layout.database) + "-journal", os.O_RDONLY)
                    try:
                        assert owner.verify_storage_recovery(db, journal).schema_version == 4
                    finally:
                        os.close(journal)
                        os.close(db)
                    events.append("legacy-permit-before-ddl")
                    migrating.append(self)
                result = super().execute(sql, parameters)
                if sql == "COMMIT" and migrating and self is migrating[0]:
                    assert not self.in_transaction
                    events.append("commit")
                return result

        def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            return original_connect(*args, **kwargs, factory=ObservedMigration)

        def unlink(path: Any, *args: Any, **kwargs: Any) -> None:
            if path == "recovery.json" and migrating:
                assert events == ["legacy-permit-before-ddl", "commit"]
                assert not migrating[0].in_transaction
                events.append("retire")
            original_unlink(path, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(sqlite3, "connect", connect)
            patch.setattr(os, "unlink", unlink)
            storage = SQLiteStorage(owner)
            storage.start()
            try:
                assert storage.wait_ready(5).schema_version == 5
                assert storage.lookup(project) == project
                assert events == ["legacy-permit-before-ddl", "commit", "retire"]
                assert not (layout.data_dir / "recovery.json").exists()
                assert not Path(str(layout.database) + "-journal").exists()
            finally:
                storage.close(timeout=5)


def test_failed_migration_ddl_rolls_back_schema4_and_registered_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_connect = sqlite3.connect
    failures = 0
    created_review_table = False
    with schema4_registry(tmp_path) as (owner, _project):
        layout = owner.store.layout
        before = layout.database.read_bytes()
        assert int.from_bytes(before[60:64], "big") == 4

        class FailingMigration(sqlite3.Connection):
            def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
                nonlocal failures, created_review_table
                if sql.startswith("CREATE TABLE execution_operations"):
                    assert created_review_table
                    failures += 1
                    raise sqlite3.OperationalError("SYNTHETIC_MIGRATION_DDL_FAILURE")
                result = super().execute(sql, parameters)
                if sql.startswith("CREATE TABLE execution_reviews"):
                    created_review_table = True
                return result

        def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            return original_connect(*args, **kwargs, factory=FailingMigration)

        with monkeypatch.context() as patch:
            patch.setattr(sqlite3, "connect", connect)
            storage = SQLiteStorage(owner)
            storage.start()
            try:
                ready = storage.wait_ready(5)
                assert ready.state == "failed"
                assert ready.error == "storage_failed"
                assert failures == 1 and created_review_table
            finally:
                storage.close(timeout=5)
        # Exact rollback preserves the whole legacy DB, including its registration.
        assert layout.database.read_bytes() == before
        assert not (layout.data_dir / "recovery.json").exists()
        assert not Path(str(layout.database) + "-journal").exists()


def test_failed_permit_publication_prevents_migration_ddl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_connect, original_link = sqlite3.connect, os.link
    failures = 0
    migration_ddl: list[str] = []
    with schema4_registry(tmp_path) as (owner, _project):
        layout = owner.store.layout
        before = layout.database.read_bytes()

        class ObservedMigration(sqlite3.Connection):
            def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
                if sql.startswith("CREATE TABLE execution_"):
                    migration_ddl.append(sql)
                return super().execute(sql, parameters)

        def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            return original_connect(*args, **kwargs, factory=ObservedMigration)

        def link(source: Any, target: Any, *args: Any, **kwargs: Any) -> None:
            nonlocal failures
            if target == "recovery.json":
                failures += 1
                raise OSError("SYNTHETIC_PERMIT_PUBLICATION_FAILURE")
            original_link(source, target, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(sqlite3, "connect", connect)
            patch.setattr(os, "link", link)
            storage = SQLiteStorage(owner)
            storage.start()
            try:
                ready = storage.wait_ready(5)
                assert ready.state == "failed"
                assert ready.error == "storage_recovery_required"
                assert failures == 1 and migration_ddl == []
            finally:
                storage.close(timeout=5)
        assert layout.database.read_bytes() == before
        assert not (layout.data_dir / "recovery.json").exists()
        assert not Path(str(layout.database) + "-journal").exists()
        assert not list(layout.data_dir.glob(".recovery-*"))


def test_rollback_retirement_failure_resolves_active_caller_and_seals_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    project = Project(uuid4(), tmp_path / "registered")
    original_unlink = os.unlink
    failures = 0

    def unlink(path: Any, *args: Any, **kwargs: Any) -> None:
        nonlocal failures
        if path == "recovery.json":
            failures += 1
            raise OSError("SYNTHETIC_RETIREMENT_FAILURE")
        original_unlink(path, *args, **kwargs)

    with owner:
        owner.store.credential(owner)
        storage = SQLiteStorage(owner)
        storage.start()
        try:
            assert storage.wait_ready(5).state == "ready"
            storage.index(project)
            with monkeypatch.context() as patch, ThreadPoolExecutor(max_workers=1) as callers:
                patch.setattr(os, "unlink", unlink)
                # A real registry conflict rolls back a real, armed transaction.
                pending = callers.submit(
                    storage.index,
                    Project(uuid4(), project.root),
                    deadline=time.monotonic() + 6,
                )
                with pytest.raises(StorageFault) as caught:
                    pending.result(timeout=2)
                assert caught.value.code == "storage_outcome_unconfirmed"
            assert failures == 1
            assert storage.snapshot().state == "failed"
            assert storage.snapshot().error == "storage_outcome_unconfirmed"
            with pytest.raises(StorageFault) as closed:
                storage.lookup(project)
            assert closed.value.code == "storage_closing"
        finally:
            storage.close(timeout=5)


def test_clean_schema4_migrates_without_losing_project_or_rewriting_v1_settings(
    tmp_path: Path,
) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    project = Project(uuid4(), tmp_path / "project")
    with owner:
        owner.store.credential(owner)
        initial = SQLiteStorage(owner)
        initial.start()
        try:
            assert initial.wait_ready(5).schema_version == 5
            initial.index(project)
        finally:
            initial.close()
        # Construct only this disposable legacy fixture, not a real installation.
        with sqlite3.connect(layout.database) as fixture:
            fixture.execute("DROP TABLE execution_reviews")
            fixture.execute("DROP TABLE execution_operations")
            fixture.execute("DELETE FROM schema_migrations WHERE version=5")
            fixture.execute("PRAGMA user_version=4")
        old_settings = b"schema_version: 1\nhistory:\n  retention_days: 30\n"
        layout.settings.write_bytes(old_settings)
        migrated = SQLiteStorage(owner)
        migrated.start()
        try:
            assert migrated.wait_ready(5).schema_version == 5
            assert migrated.lookup(project) == project
            assert layout.settings.read_bytes() == old_settings
        finally:
            migrated.close()
