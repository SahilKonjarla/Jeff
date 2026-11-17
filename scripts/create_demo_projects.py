"""Generate owned, standalone demo repositories without overwriting existing work.

This development helper intentionally does not import Jeff runtime adapters.
"""

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from textwrap import dedent as dedent

GENERATOR = "jeff-demo-projects"
VERSION = 1
MARKER = ".jeff-demo.json"
PROJECTS = ("sandbox-project", "second-project")
DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "examples"

if __package__:
    from . import _demo_templates as _templates
else:
    import importlib.util

    _template_spec = importlib.util.spec_from_file_location(
        "_jeff_demo_templates", Path(__file__).resolve().with_name("_demo_templates.py")
    )
    if _template_spec is None or _template_spec.loader is None:
        raise ImportError("Cannot load demo templates.")
    _templates = importlib.util.module_from_spec(_template_spec)
    _template_spec.loader.exec_module(_templates)

SERVICE = _templates.SERVICE
TEST_SERVICE = _templates.TEST_SERVICE
COMPOSE = _templates.COMPOSE
IGNORE = _templates.IGNORE
COMMON_README = _templates.COMMON_README
SANDBOX_README = _templates.SANDBOX_README
SECOND_README = _templates.SECOND_README


def templates(project: str) -> dict[str, str]:
    """The entire baseline file set; no source paths or secrets are copied."""
    files = {".gitignore": IGNORE}
    if project == "sandbox-project":
        files.update(
            {
                "README.md": SANDBOX_README,
                "service.py": SERVICE,
                "test_service.py": TEST_SERVICE,
                "pytest.ini": "[pytest]\ntestpaths = test_service.py\n",
                "compose.yaml": COMPOSE,
            }
        )
    elif project == "second-project":
        files["README.md"] = SECOND_README
    else:
        raise DemoError("Unknown demo project kind.")
    return files


class DemoError(Exception):
    """Public-safe setup error; never display file contents or raw Git stderr."""


@dataclass(frozen=True)
class DemoResult:
    project: str
    path: Path
    created: bool


def _safe_directory_path(path: Path, *, may_be_missing: bool) -> None:
    """Refuse links/special ancestors; only the final output directory may be absent."""
    for candidate in (path, *path.parents):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            if candidate == path and may_be_missing:
                continue
            raise DemoError("Output directory parent must already exist.") from None
        if not stat.S_ISDIR(info.st_mode):
            raise DemoError("Demo paths must be real directories, not symlinks or special files.")


def _owned(path: Path, project: str) -> None:
    """Recognize the generator marker without requiring an unchanged working tree."""
    _safe_directory_path(path, may_be_missing=False)
    _safe_directory_path(path / ".git", may_be_missing=False)
    try:
        descriptor = os.open(path / MARKER, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 4096:
                raise DemoError("Existing demo ownership marker is unsafe or too large.")
            content = stream.read(4097)
            if len(content) > 4096:
                raise DemoError("Existing demo ownership marker is too large.")
            data: object = json.loads(content)
        if not isinstance(data, dict) or (
            data.get("generator") != GENERATOR
            or type(data.get("version")) is not int
            or data.get("version") != VERSION
            or data.get("project") != project
            or not isinstance(data.get("baseline_commit"), str)
            or re.fullmatch(r"[0-9a-f]{40}", data["baseline_commit"]) is None
        ):
            raise DemoError("Existing directory is not a recognized demo of this version.")
    except (OSError, ValueError, RecursionError) as error:
        raise DemoError(
            "Existing directory has no valid safe demo marker; nothing was replaced."
        ) from error


def _git(executable: str, root: Path, *arguments: str) -> bytes:
    """Bind fixture mutations to their own .git, never an inherited parent repository."""
    _safe_directory_path(root, may_be_missing=False)
    if arguments[0] == "init":
        if (root / ".git").exists() or (root / ".git").is_symlink():
            raise DemoError(
                "Unexpected Git metadata appeared; no existing repository was initialized."
            )
    else:
        # Never let add/commit fall back to a parent repository if init was interrupted.
        _safe_directory_path(root / ".git", may_be_missing=False)
    try:
        result = subprocess.run(
            [
                executable,
                "-c",
                "init.templateDir=",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.attributesFile=/dev/null",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "user.name=Jeff Demo",
                "-c",
                "user.email=demo@example.invalid",
                "-C",
                str(root),
                *arguments,
            ],
            env={
                "PATH": os.defpath,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_ATTR_NOSYSTEM": "1",
                "GIT_ALLOW_PROTOCOL": "",
                "GIT_NO_LAZY_FETCH": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00",
                "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00",
                "LC_ALL": "C",
            },
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DemoError(
            "Fixture Git setup failed or timed out; inspect partial directories manually."
        ) from error
    if result.returncode != 0 or len(result.stdout) + len(result.stderr) > 65536:
        raise DemoError("Fixture Git setup failed; no user files were reset or cleaned.")
    return result.stdout


def _new_file(path: Path, content: str, *, private: bool = False) -> None:
    """Create exclusively under a directory descriptor; preserve any existing entry."""
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        descriptor = os.open(
            path.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600 if private else 0o644,
            dir_fd=directory,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
    finally:
        os.close(directory)


def generate(output_dir: Path = DEFAULT_OUTPUT) -> tuple[DemoResult, ...]:
    """Preflight both targets, then create only absent ones; never repair/reset existing ones."""
    if ".." in output_dir.parts:
        raise DemoError(
            "Parent traversal in the output path is not supported; use an explicit path."
        )
    output_dir = Path(os.path.abspath(output_dir))
    try:
        _safe_directory_path(output_dir, may_be_missing=True)
        pending: list[tuple[str, Path, bool]] = []
        for project in PROJECTS:
            target = output_dir / project
            exists = target.exists() or target.is_symlink()
            if exists:
                _owned(target, project)
            pending.append((project, target, exists))
        executable = shutil.which("git", path=os.defpath)
        if any(not exists for _, _, exists in pending) and executable is None:
            raise DemoError("Install system Git before creating demo repositories.")
        results: list[DemoResult] = []
        for project, target, exists in pending:
            if exists:
                results.append(DemoResult(project, target, False))
                continue
            assert executable is not None
            output_dir.mkdir(exist_ok=True)
            target.mkdir()  # Exclusive: a concurrent directory is never adopted/overwritten.
            files = templates(project)
            for name, content in files.items():
                _new_file(target / name, content)
            _git(executable, target, "init", "--initial-branch=main", "--object-format=sha1")
            _git(executable, target, "add", "--", *sorted(files))
            _git(
                executable, target, "commit", "--no-gpg-sign", "-m", "Initialize Jeff demo fixture"
            )
            baseline = (
                _git(executable, target, "rev-parse", "--verify", "HEAD").decode("ascii").strip()
            )
            if re.fullmatch(r"[0-9a-f]{40}", baseline) is None:
                raise DemoError("Fixture baseline could not be verified.")
            _new_file(
                target / "demo-notes.txt",
                f"Intentionally untracked notes for the {project} Git-question demo.\n",
            )
            _new_file(
                target / MARKER,
                json.dumps(
                    {
                        "generator": GENERATOR,
                        "version": VERSION,
                        "project": project,
                        "baseline_commit": baseline,
                    },
                    indent=2,
                )
                + "\n",
                private=True,
            )
            results.append(DemoResult(project, target, True))
        return tuple(results)
    except OSError as error:
        raise DemoError(
            "Filesystem setup failed; inspect any partial demo directories. "
            "No automatic cleanup or overwrite was attempted."
        ) from error


def main() -> int:
    """Create or preserve declared fixtures; failures never trigger automatic cleanup."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Destination for the two demos; parent must exist (default: examples/).",
    )
    args = parser.parse_args()
    try:
        results = generate(args.output_dir)
    except DemoError as error:
        print(f"Demo generation refused: {error}", file=sys.stderr)
        return 1
    for result in results:
        print(f"{'Created' if result.created else 'Preserved'}: {json.dumps(str(result.path))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
