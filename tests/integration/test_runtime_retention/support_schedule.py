"""One bounded real-clock polling helper over cached owner retention progress."""

import time

from jeff.application.storage import RetentionProgress
from jeff.infrastructure.runtime_storage import SQLiteStorage


def _scheduled_progress(store: SQLiteStorage, attempts: int) -> RetentionProgress:
    """Observe a completed scheduled attempt without submitting diagnostic SQL."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        progress = store.retention_snapshot()
        if progress.attempts >= attempts and not progress.active:
            return progress
        time.sleep(0.01)
    raise AssertionError("Scheduled owner attempt did not complete within the fixture bound")
