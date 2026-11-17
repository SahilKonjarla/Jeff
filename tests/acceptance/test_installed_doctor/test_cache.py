"""Authenticated cache-only facts, bounded retention, non-ready caches and history coexistence."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

import pytest

from jeff.application.runtime import ControlSuccess, RuntimeLayout
from jeff.infrastructure.runtime_process import RuntimeController

from .support_readonly import readonly_cli, snapshot


def test_live_doctor_uses_authenticated_cache_without_local_sql_or_settings_read(
    doctor_installation: tuple[RuntimeLayout, dict[str, str], Path],
) -> None:
    layout, env, cwd = doctor_installation
    # Deliberate inactive fixture edit; active startup-loaded retention is still30.
    layout.settings.write_text("schema_version: 1\nhistory:\n  retention_days: 17\n")
    prefix = readonly_cli(layout, env, cwd)
    before = snapshot(layout)
    result = subprocess.run(
        [*prefix, "-m", "jeff", "doctor", "--json"],
        cwd=cwd,
        env=env,
        capture_output=True,
        timeout=10,
    )
    assert snapshot(layout) == before
    report = json.loads(result.stdout)["data"]
    assert report["summary"] == "healthy"
    assert result.returncode == 0 and len(result.stdout) <= 8192
    assert report["runtime"]["application_ready"] is True
    assert report["runtime"]["identity"]["pid"] > 0
    assert report["runtime"]["active_retention_days"] == 30
    assert report["runtime"]["settings_source"] == "default_file"
    assert report["configured_retention_days"] is None
    assert report["settings_schema_version"] is None
    assert report["database_schema_version"] == 4
    assert all(check["status"] == "ok" for check in report["checks"])
    assert str(layout.data_dir) not in result.stdout.decode()


def test_live_doctor_text_labels_active_not_configured_retention(
    doctor_installation: tuple[RuntimeLayout, dict[str, str], Path],
) -> None:
    layout, env, cwd = doctor_installation
    layout.settings.write_text("synthetic invalid inactive YAML: [\n")
    prefix = readonly_cli(layout, env, cwd)
    before = snapshot(layout)
    result = subprocess.run(
        [*prefix, "-m", "jeff", "doctor"], cwd=cwd, env=env, capture_output=True, timeout=10
    )
    assert result.returncode == 0 and b"Doctor: healthy" in result.stdout
    assert b"Active retention days: 30" in result.stdout
    assert b"Configured retention" not in result.stdout
    assert snapshot(layout) == before


def test_live_doctor_bounds_large_active_retention_after_normal_restart(
    doctor_installation: tuple[RuntimeLayout, dict[str, str], Path],
) -> None:
    layout, env, cwd = doctor_installation
    controller = RuntimeController(layout)
    assert isinstance(controller.stop(), ControlSuccess)
    layout.settings.write_text(f"schema_version: 1\nhistory:\n  retention_days: {10**60}\n")
    assert isinstance(controller.start(), ControlSuccess)
    prefix = readonly_cli(layout, env, cwd)
    before = snapshot(layout)
    result = subprocess.run(
        [*prefix, "-m", "jeff", "doctor", "--json"],
        cwd=cwd,
        env=env,
        capture_output=True,
        timeout=10,
    )
    payload = json.loads(result.stdout)
    assert result.returncode == 0 and len(result.stdout) <= 8192
    assert payload["data"]["runtime"]["active_retention_days"] == "above_report_limit"
    assert payload["data"]["runtime"]["settings_source"] == "existing_file"
    assert payload["data"]["configured_retention_days"] is None
    assert str(10**60) not in result.stdout.decode()
    assert snapshot(layout) == before


@pytest.mark.parametrize(
    "cached_control", ["ready", "initializing", "failed", "closing"], indirect=True
)
def test_doctor_observes_real_nonready_control_and_storage_cache(
    cached_control: tuple[RuntimeLayout, dict[str, str], Path, str],
) -> None:
    layout, env, cwd, mode = cached_control
    prefix = readonly_cli(layout, env, cwd)
    # Initializing actor is paused; failed/closing/ready actors are quiescent.
    before = snapshot(layout)
    result = subprocess.run(
        [*prefix, "-m", "jeff", "doctor", "--json"],
        cwd=cwd,
        env=env,
        capture_output=True,
        timeout=10,
    )
    payload = json.loads(result.stdout)
    report = payload["data"]
    assert result.returncode == 1
    assert report["summary"] == ("unhealthy" if mode == "failed" else "unavailable")
    assert report["runtime"]["application_ready"] is False
    assert report["runtime"]["storage_state"] == mode
    assert report["runtime"]["identity"]["pid"] > 0
    assert report["configured_retention_days"] is None
    assert snapshot(layout) == before
    assert b"SYNTHETIC_PRIVATE_FAILURE" not in result.stdout + result.stderr


def test_doctor_does_not_displace_project_history_or_debug(
    doctor_installation: tuple[RuntimeLayout, dict[str, str], Path],
    git_repo: Path,
) -> None:
    layout, env, cwd = doctor_installation

    def invoke(*args: str) -> subprocess.CompletedProcess[bytes]:
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-m", "jeff", *args],
            cwd=git_repo,
            env=env,
            capture_output=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr.decode(errors="replace")
        return result

    invoke("init", "--json")
    invoke("ask", "what branch am I on?", "--json")
    before_debug = invoke("debug", "last", "--json").stdout
    before_history = invoke("history", "--json").stdout
    prefix = readonly_cli(layout, env, cwd)
    before = snapshot(layout)
    result = subprocess.run(
        [*prefix, "-m", "jeff", "doctor", "--json"],
        cwd=cwd,
        env=env,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0 and json.loads(result.stdout)["data"]["summary"] == "healthy"
    assert snapshot(layout) == before
    assert invoke("debug", "last", "--json").stdout == before_debug
    assert invoke("history", "--json").stdout == before_history


def test_live_doctor_uses_cache_during_actual_sqlite_write_lock(
    doctor_installation: tuple[RuntimeLayout, dict[str, str], Path],
) -> None:
    layout, env, cwd = doctor_installation
    old_mask = os.umask(0o177)
    try:
        with closing(sqlite3.connect(layout.database, timeout=0, isolation_level=None)) as writer:
            writer.execute("BEGIN EXCLUSIVE")
            try:
                writer.execute("UPDATE schema_migrations SET name=name WHERE version=4")
                assert (layout.data_dir / "state.sqlite3-journal").exists()
                prefix = readonly_cli(layout, env, cwd)
                before = snapshot(layout)
                began = time.monotonic()
                result = subprocess.run(
                    [*prefix, "-m", "jeff", "doctor", "--json"],
                    cwd=cwd,
                    env=env,
                    capture_output=True,
                    timeout=10,
                )
                assert (
                    result.returncode == 0
                    and json.loads(result.stdout)["data"]["summary"] == "healthy"
                )
                assert time.monotonic() - began < 3
                assert snapshot(layout) == before
            finally:
                writer.execute("ROLLBACK")
    finally:
        os.umask(old_mask)
