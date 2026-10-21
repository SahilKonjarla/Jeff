"""Exercise the installed entry points outside the source repository."""

import os
import subprocess
import sys
import sysconfig
from importlib.metadata import distribution
from pathlib import Path

import pytest


@pytest.mark.parametrize("entry_point", ["console", "module"])
def test_installed_help(entry_point: str, tmp_path: Path) -> None:
    scripts = Path(sysconfig.get_path("scripts"))
    command = (
        [str(scripts / "jeff"), "--help"]
        if entry_point == "console"
        else [sys.executable, "-m", "jeff", "--help"]
    )

    result = subprocess.run(
        command,
        cwd=tmp_path,
        env={"PATH": os.defpath, "NO_COLOR": "1", "TERM": "dumb"},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Project registration" in result.stdout
    assert all(command in result.stdout for command in ("init", "config", "status"))
    assert not list(tmp_path.iterdir())


def test_distribution_exposes_console_entry_point() -> None:
    package = distribution("jeff")

    assert package.metadata["Requires-Python"] == ">=3.12"
    assert any(
        entry.name == "jeff"
        and entry.group == "console_scripts"
        and entry.value == "jeff.bootstrap.main:main"
        for entry in package.entry_points
    )
