"""Build source-pinned historical compatibility fixtures on hosted macOS ARM64 only.

These are new rebuilds, not recovered original wheels or historical acceptance evidence.
No daemon, Git workload, migration, or application job is started during provisioning.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

REPOSITORY = "https://github.com/SahilKonjarla/Jeff.git"


@dataclass(frozen=True)
class Fixture:
    variable: str
    commit: str
    wire: int
    database: int


FIXTURES = (
    Fixture("JEFF_TEST_V3_PYTHON", "8f1129023631aa8a0ac4041c70bfba2d33ef0297", 3, 2),
    Fixture("JEFF_TEST_B_PYTHON", "930d8a1419a502fc87841ff3f1bb33292628c229", 5, 3),
    Fixture("JEFF_TEST_C_PYTHON", "fd3a53aa985845d806653ed3599e98212c35df90", 5, 3),
)
# Existing M02 source-rebuild tool versions/bytes; not a claim of identical rebuilt wheels.
BUILD_REQUIREMENTS = (
    "setuptools==82.0.1 --hash=sha256:"
    "a59e362652f08dcd477c78bb6e7bd9d80a7995bc73ce773050228a348ce2e5bb\n"
    "wheel==0.48.0 --hash=sha256:"
    "3217dcc807155e45db462d7ef2431f5ddda0d7273b700d05a67b271ceb1287ab\n"
    "packaging==26.3 --hash=sha256:"
    "d7193f7c8e4e93f444fde0262bf90af30e16fa0ad0ad44cb553c87339b23cd1c\n"
)
PROBE = """
import hashlib,json,sys
from pathlib import Path
import jeff
from jeff.application.runtime import PROTOCOL_REVISION
from jeff.application.storage import DATABASE_SCHEMA
root=Path(jeff.__file__).resolve().parent
assert root.is_relative_to(Path(sys.prefix).resolve()), 'Historical import escaped its venv'
print(json.dumps({'wire':PROTOCOL_REVISION,'database':DATABASE_SCHEMA,
 'modules':{'jeff/'+p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*.py')}}))
"""


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(command: list[str], cwd: Path, env: dict[str, str], timeout: int = 90) -> str:
    result = subprocess.run(
        command, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, check=False
    )
    if result.returncode:
        # Commands use only fixed public sources and owned paths; never print install/env dumps.
        raise RuntimeError(f"Compatibility setup command failed ({result.returncode}): {command}")
    return result.stdout.strip()


def constraints(checkout: Path) -> str:
    pins: dict[str, str] = {}
    for name in ("requirements.txt", "requirements-dev.lock"):
        for raw in (checkout / name).read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or line == "-r requirements.txt":
                continue
            match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^\s;]+)", line)
            if match is None:
                raise ValueError("Compatibility dependencies require exact version constraints")
            package, version = match.groups()
            package = re.sub(r"[-_.]+", "-", package).lower()
            if package in pins and pins[package] != version:
                raise ValueError("Conflicting compatibility dependency constraints")
            pins[package] = version
    if not pins:
        raise ValueError("Compatibility dependency constraints are empty")
    return "".join(f"{name}=={version}\n" for name, version in sorted(pins.items()))


def wheel_modules(source: Path, wheel: Path) -> dict[str, str]:
    expected = {
        p.relative_to(source / "src").as_posix(): digest(p)
        for p in (source / "src" / "jeff").rglob("*.py")
    }
    if not expected:
        raise ValueError("Historical source has no Python package")
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("Duplicate wheel members")
        actual = {
            name: hashlib.sha256(archive.read(name)).hexdigest()
            for name in names
            if name.startswith("jeff/") and name.endswith(".py")
        }
    if actual != expected:
        raise ValueError("Historical wheel modules differ from pinned source")
    return expected


def verify_identity(raw: str, fixture: Fixture, modules: dict[str, str]) -> None:
    if json.loads(raw) != {
        "wire": fixture.wire,
        "database": fixture.database,
        "modules": modules,
    }:
        raise ValueError("Historical installed identity differs from pinned source")


def rebuild(
    fixture: Fixture,
    root: Path,
    env: dict[str, str],
    dependency_constraints: Path,
    build_requirements: Path,
) -> dict[str, object]:
    directory = root / fixture.variable
    directory.mkdir(mode=0o700)
    source, app, dist = (directory / name for name in ("source", "app", "dist"))
    source.mkdir(mode=0o700)
    dist.mkdir(mode=0o700)
    git = ["/usr/bin/git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false"]
    run([*git, "init", "--quiet"], source, env)
    run(
        [
            *git,
            "fetch",
            "--quiet",
            "--no-tags",
            "--no-recurse-submodules",
            "--depth=1",
            REPOSITORY,
            fixture.commit,
        ],
        source,
        env,
    )
    run([*git, "checkout", "--quiet", "--detach", "FETCH_HEAD"], source, env)
    if run([*git, "rev-parse", "HEAD"], source, env) != fixture.commit:
        raise ValueError("Historical checkout is not the approved commit")
    run([sys.executable, "-I", "-m", "venv", str(app)], directory, env, timeout=30)
    python = str(app / "bin" / "python")
    pip = [python, "-I", "-m", "pip", "--isolated", "--disable-pip-version-check"]
    run(
        [*pip, "install", "--only-binary=:all:", "--require-hashes", "-r", str(build_requirements)],
        directory,
        env,
    )
    run(
        [*pip, "wheel", "--no-deps", "--no-build-isolation", "--wheel-dir", str(dist), str(source)],
        directory,
        env,
    )
    wheels = list(dist.glob("*.whl"))
    if len(wheels) != 1:
        raise ValueError("Expected exactly one historical wheel")
    wheel = wheels[0]
    modules = wheel_modules(source, wheel)
    run([*git, "diff", "--exit-code", "HEAD", "--", "src/jeff"], source, env)
    wheel_hash = digest(wheel)
    run(
        [
            *pip,
            "install",
            "--only-binary=:all:",
            "--constraint",
            str(dependency_constraints),
            str(wheel),
        ],
        directory,
        env,
    )
    verify_identity(run([python, "-I", "-B", "-c", PROBE], directory, env, 15), fixture, modules)
    run([*pip, "check"], directory, env, 15)
    if digest(wheel) != wheel_hash:
        raise ValueError("Historical wheel changed during installation")
    return {
        "variable": fixture.variable,
        "source_commit": fixture.commit,
        "python": python,
        "wire": fixture.wire,
        "database": fixture.database,
        "wheel": str(wheel),
        "wheel_sha256": wheel_hash,
        "modules": modules,
    }


def provision(root: Path) -> None:
    if any(
        os.environ.get(key) != value
        for key, value in (
            ("GITHUB_ACTIONS", "true"),
            ("RUNNER_OS", "macOS"),
            ("RUNNER_ARCH", "ARM64"),
        )
    ):
        raise ValueError("Compatibility provisioning is hosted macOS ARM64 only")
    temporary = Path(os.environ["RUNNER_TEMP"]).resolve(strict=True)
    if root.is_symlink():
        raise ValueError("Compatibility setup cannot adopt a symlink")
    root = root.resolve()
    if root.parent != temporary or root.exists():
        raise ValueError("Compatibility setup requires a fresh direct child of runner temp")
    root.mkdir(mode=0o700)
    home = root / "home"
    home.mkdir(mode=0o700)
    env = {
        "PATH": os.defpath,
        "HOME": str(home),
        "TMPDIR": str(temporary),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
        "LC_ALL": "C",
    }
    dependency_constraints = root / "constraints.txt"
    dependency_constraints.write_text(constraints(Path(__file__).resolve().parents[1]))
    build_requirements = root / "build-requirements.txt"
    build_requirements.write_text(BUILD_REQUIREMENTS)
    fixtures = [
        rebuild(fixture, root, env, dependency_constraints, build_requirements)
        for fixture in FIXTURES
    ]
    manifest = {
        "schema": 1,
        "kind": "source_pinned_rebuilds_not_original_wheels",
        "constraints_sha256": digest(dependency_constraints),
        "build_requirements_sha256": digest(build_requirements),
        "fixtures": fixtures,
    }
    with (root / "manifest.json").open("x", encoding="utf-8") as output:
        json.dump(manifest, output, indent=2)
    # Publish only after every source/wheel/installed identity has passed; no partial fallback.
    with Path(os.environ["GITHUB_ENV"]).open("a", encoding="utf-8") as output:
        for fixture in fixtures:
            output.write(f"{fixture['variable']}={fixture['python']}\n")
    print("Verified three source-pinned compatibility rebuilds (not original wheels)")


if __name__ == "__main__":
    provision(Path(os.environ["RUNNER_TEMP"]) / "jeff-compatibility")
