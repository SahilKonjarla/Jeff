"""Actual fresh-interpreter import proof; requires separately approved child execution."""

import subprocess
import sys


def test_fresh_imports_do_not_resolve_defaults_generate_entropy_or_create_state() -> None:
    code = """
import os, tempfile
from pathlib import Path
import jeff.application.projects

def forbidden(*args, **kwargs):
    raise AssertionError("Import attempted installation I/O/default selection")

Path.home = forbidden
tempfile.gettempdir = forbidden
os.getuid = os.geteuid = os.mkdir = os.open = os.urandom = forbidden
import jeff.domain.runtime
import jeff.application.runtime
import jeff.infrastructure.runtime_paths
print("import-safe")
"""
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert result.stdout.strip() == "import-safe"
