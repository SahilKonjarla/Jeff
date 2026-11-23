"""Stopped installation/catalog summaries and existing synthetic tool/host refusals."""

import os
import sqlite3
from contextlib import closing

import pytest

from jeff.application.runtime import RuntimeLayout
from jeff.infrastructure.runtime_diagnostics import RuntimeDiagnosticsAdapter

from .support_snapshot import snapshot


@pytest.mark.parametrize("role", ["lock", "credential", "settings", "database"])
def test_incomplete_installation_is_not_created_or_called_stopped(
    stopped: RuntimeLayout,
    role: str,
) -> None:
    getattr(stopped, role).unlink()
    before = snapshot(stopped)
    result = RuntimeDiagnosticsAdapter(lambda: stopped).inspect()
    assert not result.ok and result.data.summary == "unhealthy"
    assert result.data.checks[3].code == "installation_incomplete"
    assert result.data.runtime is None and result.data.database_schema_version is None
    assert snapshot(stopped) == before


@pytest.mark.parametrize("kind", ["mode", "symlink", "hardlink", "fifo", "ancestor"])
def test_unsafe_installation_is_rejected_before_content_reads(
    stopped: RuntimeLayout,
    kind: str,
) -> None:
    if kind == "mode":
        stopped.settings.chmod(0o644)
    elif kind == "symlink":
        stopped.settings.unlink()
        stopped.settings.symlink_to(stopped.data_dir.parent / "absent-target")
    elif kind == "hardlink":
        os.link(stopped.settings, stopped.data_dir / "extra-link")
    elif kind == "fifo":
        stopped.settings.unlink()
        os.mkfifo(stopped.settings, 0o600)
    else:
        stopped.data_dir.parent.chmod(0o777)
    try:
        before = snapshot(stopped)
        result = RuntimeDiagnosticsAdapter(lambda: stopped).inspect()
        assert not result.ok and result.data.summary == "unhealthy"
        assert result.data.checks[3].status == "unsafe"
        assert result.data.checks[3].code == "unsafe_runtime_path"
        assert result.data.configured_retention_days is None
        assert result.data.database_schema_version is None
        assert snapshot(stopped) == before
    finally:
        if kind == "ancestor":
            stopped.data_dir.parent.chmod(0o700)


@pytest.mark.parametrize(
    "kind,reason",
    [
        ("settings", "settings_invalid"),
        ("credential_size", "runtime_credentials_invalid"),
        ("foreign", "storage_unrecognized"),
        ("header", "storage_unrecognized"),
        ("future", "storage_schema_unsupported"),
        ("catalog", "storage_schema_unsupported"),
        ("journal", "storage_recovery_required"),
        ("wal", "storage_recovery_required"),
        ("shm", "storage_recovery_required"),
    ],
)
def test_fixed_refusals_preserve_existing_bytes_and_sidecars(
    stopped: RuntimeLayout,
    kind: str,
    reason: str,
) -> None:
    if kind == "settings":
        stopped.settings.write_text("schema_version: 1\nschema_version: 1\n")
    elif kind == "credential_size":
        stopped.credential.write_bytes(b"")
    elif kind in ("foreign", "future", "catalog"):
        # Deliberate catalog/marker mutation for refusal setup, never the assertion seam.
        with closing(sqlite3.connect(stopped.database)) as connection, connection:
            connection.execute(
                {
                    "foreign": "PRAGMA application_id=0",
                    "future": "PRAGMA user_version=5",
                    "catalog": "CREATE TABLE foreign_table(value TEXT)",
                }[kind]
            )
    elif kind == "header":
        stopped.database.write_bytes(b"synthetic non-database fixture")
    else:
        sidecar = stopped.data_dir / ("state.sqlite3-" + kind)
        sidecar.write_bytes(b"synthetic sidecar: not a hot-journal recovery claim")
        sidecar.chmod(0o600)
    before = snapshot(stopped)
    result = RuntimeDiagnosticsAdapter(lambda: stopped).inspect()
    assert not result.ok and result.data.summary == "unhealthy"
    assert any(check.code == reason and check.required for check in result.data.checks)
    assert result.data.runtime is None
    assert snapshot(stopped) == before


def test_legacy_catalog_reports_upgrade_without_migrating(stopped: RuntimeLayout) -> None:
    # Synthetic schema3 catalog, NOT a retained-installed-version compatibility claim.
    with closing(sqlite3.connect(stopped.database)) as connection, connection:
        connection.executescript(
            "DROP TABLE execution_job_watches; DROP TABLE execution_pytest_reports;"
            "DROP TABLE execution_stop_payloads; DROP TABLE execution_lifecycles;"
            "DROP TABLE execution_results;"
            "DROP TABLE execution_launch_events; DROP TABLE execution_launches;"
            "DROP TABLE execution_authority_operations; DROP TABLE execution_authorities;"
            "DROP TABLE execution_operations; DROP TABLE execution_reviews;"
            "DROP INDEX receipts_expirable; DROP TABLE request_tombstones;"
            "DROP TABLE activity_meta;"
            "CREATE TABLE activity_meta (singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
            "epoch TEXT NOT NULL, "
            "coverage_started_at_ms INTEGER NOT NULL CHECK(coverage_started_at_ms>=0), "
            "high_water INTEGER NOT NULL CHECK(high_water>=0), "
            "generation INTEGER NOT NULL CHECK(generation>=0));"
            "DELETE FROM schema_migrations WHERE version>=4; PRAGMA user_version=3;"
        )
    before = snapshot(stopped)
    result = RuntimeDiagnosticsAdapter(lambda: stopped).inspect()
    assert not result.ok and result.data.summary == "unsupported"
    assert result.data.database_schema_version == 3
    assert result.data.checks[6].code == "storage_upgrade_required"
    assert snapshot(stopped) == before


def test_large_valid_retention_has_bounded_inactive_summary(stopped: RuntimeLayout) -> None:
    stopped.settings.write_text(f"schema_version: 1\nhistory:\n  retention_days: {10**60}\n")
    before = snapshot(stopped)
    result = RuntimeDiagnosticsAdapter(lambda: stopped).inspect()
    assert result.ok and result.data.summary == "stopped"
    assert result.data.configured_retention_days == "above_report_limit"
    assert result.data.runtime is None
    assert snapshot(stopped) == before


@pytest.mark.parametrize("fault", ["missing", "invalid", "timeout"])
def test_git_process_boundary_failures_are_bounded_and_private(
    stopped: RuntimeLayout,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    import subprocess

    def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if fault == "missing":
            raise FileNotFoundError("SYNTHETIC_PRIVATE_TOOL")
        if fault == "timeout":
            raise subprocess.TimeoutExpired("SYNTHETIC_PRIVATE_TOOL", 5)
        return subprocess.CompletedProcess(["git"], 0, b"SYNTHETIC_PRIVATE_TOOL", b"PRIVATE_STDERR")

    before = snapshot(stopped)
    with monkeypatch.context() as seam:
        seam.setattr(subprocess, "run", run)
        result = RuntimeDiagnosticsAdapter(lambda: stopped).inspect()
    assert not result.ok
    assert result.data.summary == ("unavailable" if fault == "timeout" else "unhealthy")
    assert (
        result.data.checks[2].code
        == {
            "missing": "git_unavailable",
            "invalid": "git_probe_failed",
            "timeout": "diagnostic_deadline_exceeded",
        }[fault]
    )
    assert "PRIVATE" not in result.model_dump_json()
    assert snapshot(stopped) == before


@pytest.mark.parametrize("fault", ["platform", "python"])
def test_unsupported_host_inputs_are_not_platform_validation(
    stopped: RuntimeLayout,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    import sys

    with monkeypatch.context() as seam:
        if fault == "platform":
            seam.setattr(sys, "platform", "linux")
        else:
            seam.setattr(sys, "version_info", (3, 11, 0))
        result = RuntimeDiagnosticsAdapter(lambda: stopped).inspect()
    assert not result.ok and result.data.summary == "unsupported"
    assert result.data.checks[0 if fault == "platform" else 1].code == (
        "unsupported_runtime_platform" if fault == "platform" else "python_unsupported"
    )
