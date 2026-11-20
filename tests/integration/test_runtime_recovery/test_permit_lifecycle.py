"""Exact-journal arming/retirement, self-binding and no-mutation proof tampering refusals."""

import hashlib
import json
import os
import shutil
import sqlite3
import stat
from contextlib import closing
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

from jeff.application.runtime import ControlFault, RuntimeLayout
from jeff.domain.projects import Project
from jeff.infrastructure.runtime_state import RuntimeStateStore
from jeff.infrastructure.runtime_storage import SQLiteStorage


def test_storage_arms_exact_journal_before_business_dml_and_retires_after_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    state = RuntimeStateStore(layout)
    owner = state.acquire(create=True)
    assert owner is not None
    seen: list[bool] = []
    original = sqlite3.connect

    class Observed(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
            if sql.startswith("INSERT INTO projects"):
                assert layout.recovery_permit.is_file(), "Business DML preceded recovery permission"
                assert (layout.data_dir / "state.sqlite3-journal").is_file()
                seen.append(True)
            return super().execute(sql, parameters)

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        kwargs["factory"] = Observed
        return cast(sqlite3.Connection, original(*args, **kwargs))

    with owner:
        state.credential(owner)
        monkeypatch.setattr(sqlite3, "connect", connect)
        storage = SQLiteStorage(owner)
        storage.start()
        try:
            assert storage.wait_ready(5).state == "ready"
            project = Project(uuid4(), tmp_path / "project")
            assert storage.index(project) == (project, True)
            assert storage.lookup(project) == project
            assert seen == [True]
            assert not layout.recovery_permit.exists()
            assert not (layout.data_dir / "state.sqlite3-journal").exists()
        finally:
            storage.close()


@pytest.mark.parametrize(
    "mutation",
    [
        "mac",
        "version",
        "duplicate",
        "extra",
        "oversize",
        "copy_permit",
        "copy_journal",
        "copy_database",
        "replace_key",
        "key_contents",
        "permit_mode",
        "permit_link",
        "journal_link",
        "journal_mode",
        "database_mode",
    ],
)
def test_owned_storage_proof_refuses_changed_state_without_mutation(
    tmp_path: Path, mutation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    state = RuntimeStateStore(layout)
    owner = state.acquire(create=True)
    assert owner is not None
    with owner:
        state.credential(owner)
        storage = SQLiteStorage(owner)
        storage.start()
        try:
            assert storage.wait_ready(5).state == "ready"
        finally:
            storage.close()
        with closing(sqlite3.connect(layout.database, isolation_level=None)) as connection:
            page_size = connection.execute("PRAGMA page_size").fetchone()[0]
            pages = connection.execute("PRAGMA page_count").fetchone()[0]
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("PRAGMA user_version=4")
            journal = layout.data_dir / "state.sqlite3-journal"
            db_fd = os.open(layout.database, os.O_RDONLY | os.O_NOFOLLOW)
            journal_fd = os.open(journal, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                owner.arm_storage_recovery(
                    db_fd, journal_fd, page_size=page_size, original_pages=pages
                )
                proof = layout.recovery_permit
                body = proof.read_bytes()
                if mutation in ("mac", "version", "extra"):
                    data = json.loads(body)
                    if mutation == "mac":
                        data["mac"] = "0" * 64
                    elif mutation == "version":
                        data["version"] = 2
                    else:
                        data["unexpected"] = True
                    proof.write_text(json.dumps(data, sort_keys=True, separators=(",", ":")))
                elif mutation == "duplicate":
                    proof.write_bytes(b'{"version":1,' + body[1:])
                elif mutation == "oversize":
                    proof.write_bytes(body + b" " * 2048)
                elif mutation.startswith("copy_") or mutation == "replace_key":
                    target = {
                        "copy_permit": proof,
                        "copy_journal": journal,
                        "copy_database": layout.database,
                        "replace_key": layout.credential,
                    }[mutation]
                    saved = target.with_name("saved-" + target.name)
                    target.rename(saved)
                    shutil.copyfile(saved, target)
                    target.chmod(0o600)
                elif mutation == "key_contents":
                    layout.credential.write_bytes(b"x" * 32)
                elif mutation.endswith("_mode"):
                    {
                        "permit_mode": proof,
                        "journal_mode": journal,
                        "database_mode": layout.database,
                    }[mutation].chmod(0o644)
                else:
                    target = proof if mutation == "permit_link" else journal
                    os.link(target, layout.data_dir / "linked")

                def snapshot() -> dict[str, tuple[int, int, int, str | None]]:
                    return {
                        p.name: (
                            p.lstat().st_ino,
                            p.lstat().st_mode,
                            p.lstat().st_nlink,
                            hashlib.sha256(p.read_bytes()).hexdigest()
                            if stat.S_ISREG(p.lstat().st_mode)
                            else None,
                        )
                        for p in layout.data_dir.iterdir()
                    }

                before = snapshot()
                with pytest.raises((ControlFault, OSError)):
                    owner.verify_storage_recovery(db_fd, journal_fd)
                assert snapshot() == before
                engine_calls: list[bool] = []

                def forbidden(*args: Any, **kwargs: Any) -> sqlite3.Connection:
                    engine_calls.append(True)
                    raise AssertionError("Unproved storage reached SQLite")

                with monkeypatch.context() as patch:
                    patch.setattr(sqlite3, "connect", forbidden)
                    refused = SQLiteStorage(owner)
                    refused.start()
                    try:
                        assert refused.wait_ready(5).state == "failed"
                    finally:
                        refused.close()
                assert not engine_calls
                assert snapshot() == before
            finally:
                os.close(journal_fd)
                os.close(db_fd)
                if connection.in_transaction:
                    connection.execute("ROLLBACK")


def test_owned_journal_permit_is_self_bound_and_retires_only_after_sqlite(tmp_path: Path) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    state = RuntimeStateStore(layout)
    owner = state.acquire(create=True)
    assert owner is not None
    with owner:
        token = state.credential(owner)
        storage = SQLiteStorage(owner)
        storage.start()
        try:
            assert storage.wait_ready(5).state == "ready"
        finally:
            storage.close()
        with closing(sqlite3.connect(layout.database, isolation_level=None)) as connection:
            page_size = connection.execute("PRAGMA page_size").fetchone()[0]
            pages = connection.execute("PRAGMA page_count").fetchone()[0]
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("PRAGMA user_version=4")
            journal = layout.data_dir / "state.sqlite3-journal"
            assert journal.is_file()
            database_fd = os.open(layout.database, os.O_RDONLY | os.O_NOFOLLOW)
            journal_fd = os.open(journal, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                permit = owner.arm_storage_recovery(
                    database_fd, journal_fd, page_size=page_size, original_pages=pages
                )
                proof = layout.data_dir / "recovery.json"
                assert proof.is_file() and 0 < proof.stat().st_size <= 2048
                assert proof.stat().st_mode & 0o7777 == 0o600 and proof.stat().st_nlink == 1
                assert token.hex().encode() not in proof.read_bytes()
                assert owner.verify_storage_recovery(database_fd, journal_fd) == permit
                connection.execute("ROLLBACK")  # SQLite alone owns journal removal.
                assert not journal.exists()
                owner.retire_storage_recovery(database_fd)
                assert not proof.exists() and layout.database.exists() and layout.lock.exists()
            finally:
                os.close(journal_fd)
                os.close(database_fd)
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
