"""Normal restarts and signed schema5/6/7 migrations preserve facts, never permission."""

import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from uuid import uuid4

import pytest

from jeff.application.execution import (
    ActionManifest,
    DockerBackendFault,
    ExecutionCall,
    ReviewFault,
    ReviewOperation,
)
from jeff.infrastructure.runtime_state import _RecoveryPermit
from jeff.infrastructure.runtime_storage import SQLiteStorage

from .support_authority import authority_store, command
from .support_launch import completion, fact, launch_intent, launch_scope


def test_results_restart_and_signed_schema7_migration_preserve_exclusions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for migrate in (False, True):
        with authority_store(tmp_path / str(migrate)) as (store, project, manifest, generation):
            intent = launch_intent(store, project, manifest, generation)
            for phase in ("create_intent", "created", "start_intent", "exited", "cleaned"):
                store.record(fact(intent, phase), deadline=time.monotonic() + 5)
            unknown = None
            if not migrate:
                store.complete_execution(completion(intent), deadline=time.monotonic() + 5)
                unknown = launch_intent(store, project, manifest, generation)
                for phase in ("create_intent", "created", "start_intent", "exited", "cleaned"):
                    store.record(fact(unknown, phase), deadline=time.monotonic() + 5)
                store.complete_execution(
                    completion(unknown, unknown=True), deadline=time.monotonic() + 5
                )
            store.close(timeout=5)
            settings = store.owner.store.layout.settings.read_bytes()
            if migrate:
                with (
                    closing(sqlite3.connect(store.owner.store.layout.database)) as fixture,
                    fixture,
                ):
                    fixture.execute("DROP TABLE execution_stop_payloads")
                    fixture.execute("DELETE FROM schema_migrations WHERE version=10")
                    fixture.execute("DROP TABLE execution_lifecycles")
                    fixture.execute("DELETE FROM schema_migrations WHERE version=9")
                    fixture.execute("DROP TABLE execution_results")
                    fixture.execute("DELETE FROM schema_migrations WHERE version=8")
                    fixture.execute("DROP TABLE execution_pytest_reports")
                    fixture.execute("DELETE FROM schema_migrations WHERE version=11")
                    fixture.execute("PRAGMA user_version=7")
            signed: list[int] = []
            original_arm = store.owner.arm_storage_recovery

            def record_arm(
                database_fd: int,
                journal_fd: int,
                *,
                page_size: int,
                original_pages: int,
                schema_version: int = 4,
                _arm: Callable[..., _RecoveryPermit] = original_arm,
                _signed: list[int] = signed,
            ) -> object:
                permit = _arm(
                    database_fd,
                    journal_fd,
                    page_size=page_size,
                    original_pages=original_pages,
                    schema_version=schema_version,
                )
                actual = permit.schema_version
                assert actual == schema_version
                _signed.append(actual)
                return permit

            with monkeypatch.context() as patch:
                patch.setattr(store.owner, "arm_storage_recovery", record_arm)
                again = SQLiteStorage(store.owner, instance_id=uuid4())
                again.start()
                try:
                    assert again.wait_ready(5).schema_version == 11
                    assert (
                        again.lookup(project) == project
                        and store.owner.store.layout.settings.read_bytes() == settings
                    )
                    observed = again.launch_status(
                        launch_scope(intent), deadline=time.monotonic() + 5
                    )
                    assert observed.claim_state == ("unknown" if migrate else "finished")
                    if migrate:
                        assert signed[0] == 7
                        with pytest.raises(DockerBackendFault):
                            again.execution_result(
                                launch_scope(intent), deadline=time.monotonic() + 5
                            )
                    else:
                        assert (
                            again.execution_result(
                                launch_scope(intent), deadline=time.monotonic() + 5
                            ).output
                            is not None
                        )
                        assert unknown is not None
                        assert (
                            again.execution_result(
                                launch_scope(unknown), deadline=time.monotonic() + 5
                            ).disposition
                            == "quarantined"
                        )
                    with pytest.raises(DockerBackendFault):
                        again.complete_execution(completion(intent), deadline=time.monotonic() + 5)
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
            fixture.execute("DROP TABLE execution_stop_payloads")
            fixture.execute("DELETE FROM schema_migrations WHERE version=10")
            fixture.execute("DROP TABLE execution_lifecycles")
            fixture.execute("DELETE FROM schema_migrations WHERE version=9")
            fixture.execute("DROP TABLE execution_results")
            fixture.execute("DELETE FROM schema_migrations WHERE version=8")
            fixture.execute("DROP TABLE execution_launch_events")
            fixture.execute("DROP TABLE execution_launches")
            fixture.execute("DELETE FROM schema_migrations WHERE version=7")
            fixture.execute("DROP TABLE execution_authority_operations")
            fixture.execute("DROP TABLE execution_authorities")
            fixture.execute("DELETE FROM schema_migrations WHERE version=6")
            fixture.execute("DROP TABLE execution_pytest_reports")
            fixture.execute("DELETE FROM schema_migrations WHERE version=11")
            fixture.execute("PRAGMA user_version=5")
        settings_before = layout.settings.read_bytes()
        new_generation = uuid4()
        migrated = SQLiteStorage(store.owner, instance_id=new_generation)
        migrated.start()
        try:
            assert migrated.wait_ready(5).schema_version == 11
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


def test_journal_restart_and_clean_schema6_migration_never_restore_permission(
    tmp_path: Path,
) -> None:
    for downgrade in (False, True):
        with authority_store(tmp_path / str(downgrade)) as (store, project, manifest, generation):
            intent = launch_intent(store, project, manifest, generation)
            if not downgrade:
                for phase in ("create_intent", "created", "start_intent"):
                    store.record(fact(intent, phase), deadline=time.monotonic() + 5)
            store.close(timeout=5)
            settings = store.owner.store.layout.settings.read_bytes()
            if downgrade:
                with (
                    closing(sqlite3.connect(store.owner.store.layout.database)) as fixture,
                    fixture,
                ):
                    fixture.execute("DROP TABLE execution_stop_payloads")
                    fixture.execute("DELETE FROM schema_migrations WHERE version=10")
                    fixture.execute("DROP TABLE execution_lifecycles")
                    fixture.execute("DELETE FROM schema_migrations WHERE version=9")
                    fixture.execute("DROP TABLE execution_results")
                    fixture.execute("DELETE FROM schema_migrations WHERE version=8")
                    fixture.execute("DROP TABLE execution_launch_events")
                    fixture.execute("DROP TABLE execution_launches")
                    fixture.execute("DELETE FROM schema_migrations WHERE version=7")
                    fixture.execute("DROP TABLE execution_pytest_reports")
                    fixture.execute("DELETE FROM schema_migrations WHERE version=11")
                    fixture.execute("PRAGMA user_version=6")
            new_generation = uuid4()
            again = SQLiteStorage(store.owner, instance_id=new_generation)
            again.start()
            try:
                assert again.wait_ready(5).schema_version == 11
                assert (
                    again.lookup(project) == project
                    and store.owner.store.layout.settings.read_bytes() == settings
                )
                old = again.authority(
                    command(
                        project,
                        new_generation,
                        operation="show",
                        approval_id=intent.record.approval_id,
                    ),
                    deadline=time.monotonic() + 5,
                )
                assert old.record.claim_state == "unknown" and old.launch_claim is None
                with pytest.raises(DockerBackendFault):
                    again.record(fact(intent, "start_intent"), deadline=time.monotonic() + 5)
                if not downgrade:
                    snapshot = again.launch_status(
                        launch_scope(intent), deadline=time.monotonic() + 5
                    )
                    assert snapshot.phases == ("create_intent", "created", "start_intent")
                    assert snapshot.uncertain and snapshot.container_id == "e" * 64
            finally:
                again.close(timeout=5)
