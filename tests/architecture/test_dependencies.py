"""AST checks for the tier graph, including relative and from-import forms.

These checks inspect static imports, not dynamic imports or runtime behavior.
"""

import ast
import sys
from importlib.util import resolve_name
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[2] / "src" / "jeff"
ALLOWED_TIERS = {
    "domain": {"domain"},
    "application": {"application", "domain"},
    "infrastructure": {"infrastructure", "application", "domain"},
    "integrations": {"integrations", "application", "domain"},
    "interfaces": {"interfaces", "application", "domain"},
}
CLIENT_LIBRARIES = {"typer", "pydantic", "PySide6"}
TECHNICAL_LIBRARIES = {"sqlite3", "subprocess", "socket", "ctypes"}


def import_targets(source: str, module: str, *, is_package: bool = False) -> list[str]:
    """Normalize static imports without importing inspected code."""
    targets: list[str] = []
    package = module if is_package else module.rpartition(".")[0]
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            targets.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                base = resolve_name("." * node.level + base, package)
            targets.append(base)
            targets.extend(f"{base}.{alias.name}" for alias in node.names)
    return targets


def violations(source: str, module: str, *, is_package: bool = False) -> list[str]:
    """Return forbidden import targets for one production module."""
    parts = module.split(".")
    tier = parts[1] if len(parts) > 1 else ""
    forbidden: list[str] = []
    for target in import_targets(source, module, is_package=is_package):
        target_parts = target.split(".")
        root = target_parts[0]
        if root in {"scripts", "tests"}:
            forbidden.append(target)
            continue
        if root != "jeff":
            if tier == "domain" and root not in sys.stdlib_module_names:
                forbidden.append(target)
            elif tier in {"application", "interfaces"}:
                allowed_external = {"pydantic"} if tier == "application" else CLIENT_LIBRARIES
                if root in TECHNICAL_LIBRARIES or (
                    root not in sys.stdlib_module_names and root not in allowed_external
                ):
                    forbidden.append(target)
            continue

        target_tier = target_parts[1] if len(target_parts) > 1 else ""
        if target_tier == "bootstrap" and tier not in {"bootstrap", "__main__"}:
            forbidden.append(target)
        elif tier in ALLOWED_TIERS and target_tier not in ALLOWED_TIERS[tier]:
            forbidden.append(target)
        elif (
            tier == target_tier == "integrations"
            and len(parts) > 2
            and len(target_parts) > 2
            and parts[2] != target_parts[2]
        ):
            forbidden.append(target)
    return forbidden


def test_production_dependency_graph() -> None:
    failures: list[str] = []
    for path in sorted(PACKAGE.rglob("*.py")):
        parts = path.relative_to(PACKAGE.parent).with_suffix("").parts
        is_package = parts[-1] == "__init__"
        module = ".".join(parts[:-1] if is_package else parts)
        for target in violations(path.read_text(), module, is_package=is_package):
            failures.append(f"{path.relative_to(PACKAGE.parent)} imports {target}")

    assert not failures, "\n".join(failures)


@pytest.mark.parametrize(
    ("module", "source"),
    [
        ("jeff.domain.projects", "import pydantic"),
        ("jeff.domain.projects", "from jeff.application import api"),
        ("jeff.application.api", "from ..infrastructure import persistence"),
        ("jeff.infrastructure.storage", "import jeff.interfaces.cli"),
        ("jeff.infrastructure.storage", "import scripts.create_demo_projects"),
        ("jeff.interfaces.cli", "from jeff import infrastructure"),
        ("jeff.interfaces.cli", "import sqlite3"),
        ("jeff.interfaces.cli", "import subprocess"),
        ("jeff.interfaces.cli", "import openai"),
        ("jeff.application.api", "import subprocess"),
        ("jeff.application.api", "from jeff.bootstrap.main import main"),
        ("jeff.integrations.git.adapter", "from ..docker import adapter"),
    ],
)
def test_forbidden_imports_are_detected(module: str, source: str) -> None:
    assert violations(source, module)


@pytest.mark.parametrize(
    ("module", "source"),
    [
        ("jeff.domain.projects", "from dataclasses import dataclass"),
        ("jeff.application.api", "from jeff.domain import projects"),
        ("jeff.application.api", "import pydantic"),
        ("jeff.interfaces.cli", "import typer"),
        ("jeff.bootstrap.main", "from jeff.interfaces.cli import app"),
        ("jeff.__main__", "from .bootstrap.main import main"),
        ("jeff.integrations.git.adapter", "from . import parsing"),
    ],
)
def test_allowed_imports_are_accepted(module: str, source: str) -> None:
    assert violations(source, module) == []


def test_package_relative_imports_are_resolved() -> None:
    assert violations("from ..infrastructure import storage", "jeff.domain", is_package=True)
