"""Disposable legacy schema-four registry with its original owner and transaction lifetimes."""

import os
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from uuid import uuid4

from jeff.application.runtime import RuntimeLayout
from jeff.domain.projects import Project
from jeff.infrastructure.runtime_state import Ownership, RuntimeStateStore
from jeff.infrastructure.runtime_storage import SQLiteStorage


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
            assert initial.wait_ready(5).schema_version == 12
            initial.index(project)
        finally:
            initial.close(timeout=5)
        with closing(sqlite3.connect(layout.database)) as fixture, fixture:
            fixture.execute("DROP TABLE execution_job_watches")
            fixture.execute("DELETE FROM schema_migrations WHERE version=12")
            fixture.execute("DROP TABLE execution_pytest_reports")
            fixture.execute("DELETE FROM schema_migrations WHERE version=11")
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
        yield owner, project
