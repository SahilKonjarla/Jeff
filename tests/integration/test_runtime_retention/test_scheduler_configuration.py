"""Huge/restart-only retention settings and readiness-relative opt-in scheduling defaults."""

import os
import sqlite3
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from jeff.application.runtime import RuntimeLayout
from jeff.application.storage import ReceiptClaim, ReceiptTransition
from jeff.infrastructure.runtime_state import RuntimeStateStore
from jeff.infrastructure.runtime_storage import SQLiteStorage

from .support_schedule import _scheduled_progress


def test_huge_retention_days_are_safe_and_settings_reload_only_on_restart(tmp_path: Path) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    instance, project = uuid4(), uuid4()
    mono, wall = [100.0], [0]
    huge = 10**100
    with owner:
        # Create ordinary private defaults, then change only this owned fixture's policy.
        owner.store.credential(owner)
        initial = SQLiteStorage(owner)
        initial.start()
        try:
            assert initial.wait_ready(5).retention_days == 30
        finally:
            initial.close()
        layout.settings.write_text(f"schema_version: 1\nhistory:\n  retention_days: {huge}\n")
        store = SQLiteStorage(
            owner,
            instance_id=instance,
            scheduled_retention=True,
            retention_clock=lambda: wall[0],
            retention_monotonic=lambda: mono[0],
        )
        store.start()
        try:
            assert store.wait_ready(5).retention_days == huge
            claim = ReceiptClaim(
                request_id=uuid4(),
                owner_instance_id=instance,
                canonical_root=tmp_path / "p",
                project_id=project,
                operation="ask.branch",
            )
            store.claim(claim, deadline=time.monotonic() + 5)
            done = store.finish(
                ReceiptTransition(
                    request_id=claim.request_id,
                    owner_instance_id=instance,
                    state="succeeded",
                    outcome_code="ok",
                ),
                deadline=time.monotonic() + 5,
            )
            assert done.finished_at_ms is not None
            wall[0] = done.finished_at_ms + 86400000 + 1
            mono[0] = 160
            before = _scheduled_progress(store, 1)
            assert before.last_batch is not None and before.last_batch.retired_count == 0
            layout.settings.write_text(
                "# restart only\nschema_version: 1\nhistory:\n  retention_days: 1\n"
            )
            mono[0] = 220
            live = _scheduled_progress(store, 2)
            assert live.last_batch is not None and live.last_batch.retired_count == 0
            assert store.snapshot().retention_days == huge
            assert live.last_batch.retention_generation == before.last_batch.retention_generation
        finally:
            store.close()
        restarted = SQLiteStorage(
            owner,
            instance_id=instance,
            scheduled_retention=True,
            retention_clock=lambda: wall[0],
            retention_monotonic=lambda: mono[0],
        )
        restarted.start()
        try:
            assert restarted.wait_ready(5).retention_days == 1
            assert restarted.retention_snapshot().attempts == 0
            assert restarted.retention_snapshot().next_attempt_at == 280
            mono[0] = 280
            expired = _scheduled_progress(restarted, 1)
            assert expired.last_batch is not None and expired.last_batch.retired_count == 1
            assert layout.settings.read_text().startswith("# restart only\n")
        finally:
            restarted.close()


def test_scheduler_is_disabled_by_default_even_when_both_clocks_advance(tmp_path: Path) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    mono, wall = [100.0], [0]
    with owner:
        owner.store.credential(owner)
        store = SQLiteStorage(
            owner, retention_monotonic=lambda: mono[0], retention_clock=lambda: wall[0]
        )
        store.start()
        try:
            assert store.wait_ready(5).state == "ready"
            mono[0], wall[0] = 1000000, 9223372036854775807
            time.sleep(0.12)
            progress = store.retention_snapshot()
            assert not progress.enabled and progress.attempts == 0
            assert progress.next_attempt_at is None and progress.last_batch is None
        finally:
            store.close()


def test_sixty_second_delay_begins_after_sql_initialization_not_at_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = RuntimeLayout(data_dir=tmp_path / "d", runtime_dir=tmp_path / "r", uid=os.geteuid())
    owner = RuntimeStateStore(layout).acquire(create=True)
    assert owner is not None
    mono = [100.0]
    original = sqlite3.connect

    class StartupClockAdvance(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = ()) -> sqlite3.Cursor:
            result = super().execute(sql, parameters)
            if sql == "PRAGMA user_version=4":
                mono[0] = 500  # controlled time passes during actual schema initialization
            return result

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        return original(*args, **kwargs, factory=StartupClockAdvance)

    with owner, monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", connect)
        owner.store.credential(owner)
        store = SQLiteStorage(owner, scheduled_retention=True, retention_monotonic=lambda: mono[0])
        store.start()
        try:
            assert store.wait_ready(5).state == "ready"
            assert store.retention_snapshot().next_attempt_at == 560
            assert store.retention_snapshot().attempts == 0
            mono[0] = 559.999
            time.sleep(0.12)
            assert store.retention_snapshot().attempts == 0
            mono[0] = 560
            assert _scheduled_progress(store, 1).last_outcome == "committed"
        finally:
            store.close()
