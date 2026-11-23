"""Safe disposable repositories; no mutations to the Jeff repository."""

import ast
import io
import os
import shutil
import subprocess
import tokenize
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest


def _count_code_lines(source: str) -> int:
    """Count code-bearing rows, excluding docstrings and multiline literal spans."""
    nodes = list(ast.walk(ast.parse(source)))
    docstrings: set[ast.AST] = set()
    for node in nodes:
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                docstrings.add(first)

    lines = source.split("\n")
    masked = [list(line) for line in lines]
    for node in nodes:
        if not isinstance(node, (ast.Constant, ast.JoinedStr, ast.Expr)):
            continue
        if isinstance(node, ast.Expr) and node not in docstrings:
            continue
        if isinstance(node, ast.Constant) and not isinstance(node.value, (str, bytes)):
            continue
        end, end_column = node.end_lineno, node.end_col_offset
        assert end is not None and end_column is not None
        if node not in docstrings and node.lineno == end:
            continue
        for row in range(node.lineno, end + 1):
            line = lines[row - 1]
            # AST columns are UTF-8 byte offsets; tokenize columns are characters.
            start = len(line.encode()[: node.col_offset].decode()) if row == node.lineno else 0
            stop = len(line.encode()[:end_column].decode()) if row == end else len(line)
            masked[row - 1][start:stop] = [" "] * (stop - start)

    ignored = {
        tokenize.COMMENT,
        tokenize.NL,
        tokenize.NEWLINE,
        tokenize.INDENT,
        tokenize.DEDENT,
        tokenize.ENDMARKER,
        tokenize.ENCODING,
    }
    code_rows: set[int] = set()
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type in ignored:
            continue
        for row in range(token.start[0], token.end[0] + 1):
            start = token.start[1] if row == token.start[0] else 0
            stop = token.end[1] if row == token.end[0] else len(masked[row - 1])
            if "".join(masked[row - 1][start:stop]).strip():
                code_rows.add(row)
    return len(code_rows)


@pytest.fixture
def code_line_count() -> Callable[[str], int]:
    """Shared size policy; source inspection only, never import or execute the file."""
    return _count_code_lines


@pytest.fixture
def git_helper_script() -> Callable[[str, str], str]:
    """Apply a native fault/observer at Git's owning process, retaining all inherited FDs."""

    def compose(helper_hook: str, daemon_hook: str) -> str:
        worker = (
            helper_hook + "\nimport jeff.integrations.git.worker as _worker\n"
            "raise SystemExit(_worker.main())\n"
        )
        return f"""
import subprocess
_spawn_helper=subprocess.Popen
_helper_code={worker!r}
def _launch_helper(command,*args,**kwargs):
    if command[3:5]==['-m','jeff.integrations.git.worker']:
        command=[*command[:3],'-c',_helper_code,*command[5:]]
    return _spawn_helper(command,*args,**kwargs)
subprocess.Popen=_launch_helper
{daemon_hook}
"""

    return compose


@pytest.fixture
def owned_runtime_launches(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Capture genuine handles; emergency cleanup is evidence of a failing fixture."""
    original = subprocess.Popen
    children: list[subprocess.Popen[bytes]] = []

    def tracked(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        child = original(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", tracked)
    yield
    forced = False
    for child in children:
        if child.poll() is None:
            forced = True
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
    assert not forced, "Owned runtime fixture required forced child cleanup"


@pytest.fixture(autouse=True)
def legacy_sqlite_fixture_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Existing native compatibility fixtures deliberately exercise legacy SQLite."""
    monkeypatch.setenv("JEFF_TEST_SQLITE_FIXTURE", "1")


@pytest.fixture
def cli_environment(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> dict[str, str]:
    root = tmp_path_factory.mktemp("cli").resolve()
    for name in ("home", "tmp", "config", "cache", "data"):
        (root / name).mkdir(mode=0o700)
    env = {
        "HOME": str(root / "home"),
        "TMPDIR": str(root / "tmp"),
        "XDG_CONFIG_HOME": str(root / "config"),
        "XDG_CACHE_HOME": str(root / "cache"),
        "XDG_DATA_HOME": str(root / "data"),
        "JEFF_DATA_DIR": str(root / "d"),
        "JEFF_RUNTIME_DIR": str(root / "r"),
        "JEFF_TEST_SQLITE_FIXTURE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return env


@pytest.fixture
def running_cli(
    cli_environment: dict[str, str], owned_runtime_launches: None
) -> Iterator[dict[str, str]]:
    from jeff.application.runtime import ControlSuccess
    from jeff.bootstrap.runtime import create_runtime_controller

    control = create_runtime_controller()
    try:
        started = control.start()
        assert isinstance(started, ControlSuccess) and started.data.application_ready, started
        yield cli_environment
    finally:
        assert isinstance(control.stop(), ControlSuccess)
        (Path(cli_environment["JEFF_DATA_DIR"]) / "ipc.key").unlink(missing_ok=True)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Create a disposable child repository, not the harness's Git-negative boundary.

    The runner must first verify that the temporary arena has no Git ancestors.
    Merely placing an ignored scratch directory inside Jeff is not sufficient.
    """
    executable = shutil.which("git", path=os.defpath)
    assert executable is not None, "These checks require an installed system Git"
    root = tmp_path / "sample project"
    root.mkdir()
    subprocess.run(
        [executable, "-c", "init.templateDir=", "init", "--initial-branch=main", str(root)],
        cwd=tmp_path,
        env={
            "PATH": os.defpath,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        },
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    return root.resolve()


@pytest.fixture
def git_run(git_repo: Path) -> Callable[..., subprocess.CompletedProcess[bytes]]:
    """Every mutation is bound to this fixture's disposable repository."""
    executable = shutil.which("git", path=os.defpath)
    assert executable is not None

    def run(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [
                executable,
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "user.name=Jeff Tests",
                "-c",
                "user.email=tests@example.invalid",
                "-C",
                str(git_repo),
                *arguments,
            ],
            env={"PATH": os.defpath, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull},
            capture_output=True,
            check=check,
            timeout=10,
        )

    return run
