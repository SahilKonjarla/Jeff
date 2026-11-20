"""Signed source8/9 stop-schema upgrades and original rollback/permit/DDL ordering."""

import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

from jeff.application.storage import StorageFault
from jeff.infrastructure.runtime_state import _RecoveryPermit
from jeff.infrastructure.runtime_storage import SQLiteStorage

from .support_authority import authority_store
from .support_launch import completion, fact, launch_intent, launch_scope


def test_schema8_to9_signs_source_before_ddl_preserves_v1_and_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_connect = sqlite3.connect
    for fail_ddl in (False, True):
        with authority_store(tmp_path / str(fail_ddl)) as (store, project, manifest, generation):
            intent = launch_intent(store, project, manifest, generation)
            for phase in ("create_intent", "created", "start_intent", "exited", "cleaned"):
                store.record(fact(intent, phase), deadline=time.monotonic() + 5)
            original = store.complete_execution(completion(intent), deadline=time.monotonic() + 5)
            store.close(timeout=5)
            layout = store.owner.store.layout
            with closing(original_connect(layout.database)) as fixture, fixture:
                fixture.execute("DROP TABLE execution_stop_payloads")
                fixture.execute("DELETE FROM schema_migrations WHERE version=10")
                fixture.execute("DROP TABLE execution_lifecycles")
                fixture.execute("DELETE FROM schema_migrations WHERE version=9")
                fixture.execute("PRAGMA user_version=8")
            before = layout.database.read_bytes()
            settings = layout.settings.read_bytes()
            signed: list[int] = []
            ddl: list[str] = []
            arm = store.owner.arm_storage_recovery

            def signed_arm(
                *args: Any,
                _signed: list[int] = signed,
                _arm: Callable[..., _RecoveryPermit] = arm,
                **kwargs: Any,
            ) -> _RecoveryPermit:
                _signed.append(int(kwargs["schema_version"]))
                return _arm(*args, **kwargs)

            class Migration(sqlite3.Connection):
                def execute(
                    self,
                    sql: str,
                    parameters: Any = (),
                    /,
                    *,
                    _signed: list[int] = signed,
                    _ddl: list[str] = ddl,
                    _fail: bool = fail_ddl,
                ) -> sqlite3.Cursor:
                    if sql.startswith("CREATE TABLE execution_lifecycles"):
                        assert _signed and _signed[-1] == 8
                        _ddl.append(sql)
                        if _fail:
                            raise sqlite3.OperationalError("synthetic DB9 DDL refusal")
                    return super().execute(sql, parameters)

            def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
                return cast(
                    sqlite3.Connection, original_connect(*args, **{**kwargs, "factory": Migration})
                )

            with monkeypatch.context() as patch:
                patch.setattr(store.owner, "arm_storage_recovery", signed_arm)
                patch.setattr(sqlite3, "connect", connect)
                migrated = SQLiteStorage(store.owner, instance_id=uuid4())
                migrated.start()
                try:
                    state = migrated.wait_ready(5)
                    assert len(ddl) == 1 and signed[0] == 8
                    if fail_ddl:
                        assert state.state == "failed"
                    else:
                        assert state.schema_version == 10
                        assert (
                            migrated.execution_result(
                                launch_scope(intent), deadline=time.monotonic() + 5
                            )
                            == original
                        )
                        assert migrated.lookup(project) == project
                finally:
                    migrated.close(timeout=5)
            assert layout.settings.read_bytes() == settings
            if fail_ddl:
                assert layout.database.read_bytes() == before


@pytest.mark.parametrize("failure", ("none", "ddl", "permit"))
def test_db10_migration_signs_exact_source9_before_ddl_and_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    original_connect = sqlite3.connect
    with authority_store(tmp_path) as (store, project, manifest, generation):
        intent = launch_intent(store, project, manifest, generation)
        for phase in ("create_intent", "created", "start_intent", "exited", "cleaned"):
            store.record(fact(intent, phase), deadline=time.monotonic() + 5)
        original = store.complete_execution(completion(intent), deadline=time.monotonic() + 5)
        store.close(timeout=5)
        layout = store.owner.store.layout
        with closing(original_connect(layout.database)) as sql, sql:
            sql.execute("DROP TABLE execution_stop_payloads")
            sql.execute("DELETE FROM schema_migrations WHERE version=10")
            sql.execute("PRAGMA user_version=9")
        before = layout.database.read_bytes()
        settings = layout.settings.read_bytes()
        signed: list[int] = []
        ddl: list[str] = []
        arm = store.owner.arm_storage_recovery

        def signed_arm(*args: Any, **kwargs: Any) -> _RecoveryPermit:
            signed.append(int(kwargs["schema_version"]))
            if failure == "permit":
                raise StorageFault("storage_recovery_required")
            return arm(*args, **kwargs)

        class Migration(sqlite3.Connection):
            def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
                if sql.startswith("CREATE TABLE execution_stop_payloads"):
                    assert signed == [9]
                    ddl.append(sql)
                    if failure == "ddl":
                        raise sqlite3.OperationalError("synthetic DB10 DDL refusal")
                return super().execute(sql, parameters)

        def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            return cast(
                sqlite3.Connection, original_connect(*args, **{**kwargs, "factory": Migration})
            )

        with monkeypatch.context() as patch:
            patch.setattr(store.owner, "arm_storage_recovery", signed_arm)
            patch.setattr(sqlite3, "connect", connect)
            migrated = SQLiteStorage(store.owner, instance_id=uuid4())
            migrated.start()
            try:
                state = migrated.wait_ready(5)
                assert signed and signed[0] == 9
                if failure != "none":
                    assert state.state == "failed"
                else:
                    assert state.schema_version == 10
                    assert (
                        migrated.execution_result(
                            launch_scope(intent), deadline=time.monotonic() + 5
                        )
                        == original
                    )
                    assert migrated.lookup(project) == project
                assert len(ddl) == (0 if failure == "permit" else 1)
            finally:
                migrated.close(timeout=5)
        assert layout.settings.read_bytes() == settings
        if failure != "none":
            assert layout.database.read_bytes() == before
