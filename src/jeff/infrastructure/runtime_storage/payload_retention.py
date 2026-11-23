"""Bounded shared known/quarantined output slots and physical payload retirement."""

import sqlite3
from typing import cast

from jeff.application.storage import StorageFault

from .. import runtime_storage as _state


def _retire_execution_payloads(
    connection: sqlite3.Connection, cutoff: int, *, age_limit: int = 4
) -> int:
    # Bounded page churn under the existing16MiB recovery-journal ceiling.
    # No claim of physical-crash proof follows from this logical byte budget.
    rows = _state.SQLiteStorage._retained_payloads(connection)
    retired = 0
    expired = sorted(
        (row for row in rows if row[4] and row[3] < cutoff),
        key=lambda row: (row[2] != "quarantined", row[3], row[0], row[1]),
    )[:age_limit]
    removed = {(row[0], row[1]) for row in expired}
    known = sorted(
        (r for r in rows if r[2] == "known" and (r[0], r[1]) not in removed),
        key=lambda r: (r[3], r[0], r[1]),
    )
    for row, reason in [(r, "age") for r in expired] + [(r, "capacity") for r in known[:-16]]:
        table, key = (
            ("execution_results", "sequence")
            if row[0] == "legacy"
            else ("execution_stop_payloads", "proposal_id")
        )
        if table == "execution_results":
            # Report excerpts share the archive’s retention, not the terminal facts’ lifetime.
            connection.execute(
                "DELETE FROM execution_pytest_reports WHERE attempt_id="
                "(SELECT attempt_id FROM execution_results WHERE sequence=?)",
                (row[1],),
            )
        connection.execute(
            f"UPDATE {table} SET stdout=NULL,stderr=NULL,retirement_reason=? WHERE {key}=?",
            (reason, row[1]),
        )
        retired += 1
    return retired


def _retained_payloads(connection: sqlite3.Connection) -> list[tuple[str, str, str, int, int]]:
    rows = connection.execute(
        "SELECT 'legacy',CAST(sequence AS TEXT),CASE disposition WHEN 'completed' "
        "THEN 'known' ELSE 'quarantined' END,recorded_at_ms,1 FROM execution_results "
        "WHERE stdout IS NOT NULL UNION ALL SELECT 'stop',proposal_id,disposition,"
        "recorded_at_ms,clock_confirmed FROM execution_stop_payloads "
        "WHERE stdout IS NOT NULL LIMIT 19"
    ).fetchall()
    if len(rows) > 18 or sum(r[2] == "quarantined" for r in rows) > 1:
        raise StorageFault("storage_schema_unsupported")
    return cast(list[tuple[str, str, str, int, int]], rows)
