"""Signed historical migration ordering and clean legacy settings/project preservation."""

import hashlib
import hmac
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from jeff.application.runtime import RuntimeLayout
from jeff.domain.projects import Project
from jeff.infrastructure.runtime_state import RuntimeStateStore
from jeff.infrastructure.runtime_storage import SQLiteStorage

from .support_schema4 import schema4_registry


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
                if (
                    sql == "COMMIT"
                    and migrating
                    and self is migrating[0]
                    and events == ["legacy-permit-before-ddl"]
                ):
                    assert not self.in_transaction
                    events.append("commit")
                return result

        def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            return original_connect(*args, **kwargs, factory=ObservedMigration)

        def unlink(path: Any, *args: Any, **kwargs: Any) -> None:
            if (
                path == "recovery.json"
                and migrating
                and events == ["legacy-permit-before-ddl", "commit"]
            ):
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
                assert storage.wait_ready(5).schema_version == 10
                assert storage.lookup(project) == project
                assert events == ["legacy-permit-before-ddl", "commit", "retire"]
                assert not (layout.data_dir / "recovery.json").exists()
                assert not Path(str(layout.database) + "-journal").exists()
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
            assert initial.wait_ready(5).schema_version == 10
            initial.index(project)
        finally:
            initial.close()
        # Construct only this disposable legacy fixture, not a real installation.
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
            fixture.execute("DROP TABLE execution_reviews")
            fixture.execute("DROP TABLE execution_operations")
            fixture.execute("DELETE FROM schema_migrations WHERE version=5")
            fixture.execute("PRAGMA user_version=4")
        old_settings = b"schema_version: 1\nhistory:\n  retention_days: 30\n"
        layout.settings.write_bytes(old_settings)
        migrated = SQLiteStorage(owner)
        migrated.start()
        try:
            assert migrated.wait_ready(5).schema_version == 10
            assert migrated.lookup(project) == project
            assert layout.settings.read_bytes() == old_settings
        finally:
            migrated.close()
