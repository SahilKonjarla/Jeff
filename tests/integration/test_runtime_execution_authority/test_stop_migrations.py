"""Signed DB upgrades, report persistence and original rollback/permit/DDL ordering."""

import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

from jeff.application.execution import DockerCompletion
from jeff.application.execution.pytest_report import PytestReport, PytestResult
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
                fixture.execute("DROP TABLE execution_pytest_reports")
                fixture.execute("DELETE FROM schema_migrations WHERE version=11")
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
                        assert state.schema_version == 11
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


@pytest.mark.parametrize("source", (9, 10))
@pytest.mark.parametrize("failure", ("none", "ddl", "permit"))
def test_migration_signs_exact_source_before_ddl_and_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    source: int,
) -> None:
    original_connect = sqlite3.connect
    target_table = "execution_stop_payloads" if source == 9 else "execution_pytest_reports"
    with authority_store(tmp_path) as (store, project, manifest, generation):
        intent = launch_intent(store, project, manifest, generation)
        for phase in ("create_intent", "created", "start_intent", "exited", "cleaned"):
            store.record(fact(intent, phase), deadline=time.monotonic() + 5)
        original = store.complete_execution(completion(intent), deadline=time.monotonic() + 5)
        store.close(timeout=5)
        layout = store.owner.store.layout
        with closing(original_connect(layout.database)) as sql, sql:
            sql.execute("DROP TABLE execution_pytest_reports")
            if source == 9:
                sql.execute("DROP TABLE execution_stop_payloads")
            sql.execute("DELETE FROM schema_migrations WHERE version>?", (source,))
            sql.execute(f"PRAGMA user_version={source}")
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
                if sql.startswith(f"CREATE TABLE {target_table} ("):
                    assert signed == [source]
                    ddl.append(sql)
                    if failure == "ddl":
                        raise sqlite3.OperationalError("synthetic migration DDL refusal")
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
                assert signed and signed[0] == source
                if failure != "none":
                    assert state.state == "failed"
                else:
                    assert state.schema_version == 11
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


def test_pytest_report_reopens_with_completion_and_expires_with_output(tmp_path: Path) -> None:
    now = 1_800_000_000_000
    with authority_store(tmp_path, retention_clock=lambda: now) as (
        store,
        project,
        manifest,
        generation,
    ):
        intent = launch_intent(store, project, manifest, generation)
        for phase in ("create_intent", "created", "start_intent", "exited", "cleaned"):
            event = fact(intent, phase)
            if phase == "exited":
                event = event.model_copy(update={"exit_code": 0})
            store.record(event, deadline=time.monotonic() + 5)
        report = PytestResult(
            status="available",
            report=PytestReport(
                version=1, run_token="ab" * 16, passed=2, failed=0, exit_status=0, failures=()
            ),
        )
        completed = completion(intent, exit_code=0)
        request = DockerCompletion(
            owned=completed.owned,
            outcome=completed.outcome,
            output=completed.output,
            pytest_result=report,
        )
        original = store.complete_execution(request, deadline=time.monotonic() + 5)
        assert original.pytest_result == report
        assert original.completion_digest == completed.fingerprint
        store.close()

        reopened = SQLiteStorage(store.owner, instance_id=uuid4(), retention_clock=lambda: now)
        reopened.start()
        try:
            assert reopened.wait_ready(5).schema_version == 11
            scope = launch_scope(intent)
            assert reopened.execution_result(scope, deadline=time.monotonic() + 5) == original
            now += 31 * 86400000
            expired = reopened.execution_result(scope, deadline=time.monotonic() + 5)
            assert expired.payload_state == "expired" and expired.pytest_result is None
        finally:
            reopened.close()
