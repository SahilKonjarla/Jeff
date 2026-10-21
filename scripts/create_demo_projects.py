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
from textwrap import dedent

GENERATOR = "jeff-demo-projects"
VERSION = 1
MARKER = ".jeff-demo.json"
PROJECTS = ("sandbox-project", "second-project")
DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "examples"

SERVICE = dedent('''\
    """Synthetic HTTP fixture: no static files, credentials, or external services."""

    import argparse
    import json
    import sys
    from http.server import BaseHTTPRequestHandler, HTTPServer


    def payload() -> dict[str, str]:
        return {"status": "ok", "service": "jeff-demo"}


    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            healthy = self.path == "/health"
            body = json.dumps(payload() if healthy else {"error": "not_found"}).encode("utf-8")
            self.send_response(200 if healthy else 404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            # Avoid retaining request paths or other untrusted HTTP metadata.
            pass


    def port_number(value: str) -> int:
        port = int(value)
        if not 0 <= port <= 65535:
            raise argparse.ArgumentTypeError("Port must be between 0 and 65535.")
        return port


    def main() -> int:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--host", choices=("127.0.0.1", "0.0.0.0"), default="127.0.0.1")
        parser.add_argument("--port", type=port_number, default=0,
                            help="0 lets the OS allocate a free port at bind time.")
        args = parser.parse_args()
        try:
            with HTTPServer((args.host, args.port), Handler) as server:
                actual_port = server.server_address[1]
                print(json.dumps({"host": args.host, "port": actual_port,
                                  "url": f"http://{args.host}:{actual_port}/health"}), flush=True)
                try:
                    server.serve_forever()
                except KeyboardInterrupt:
                    pass
        except OSError:
            print("Cannot bind the demo service; check the address/port and retry deliberately.",
                  file=sys.stderr)
            return 1
        return 0


    if __name__ == "__main__":
        raise SystemExit(main())
''')

TEST_SERVICE = dedent('''\
    """One passing check and one deliberately failing fixture test."""

    from service import payload


    def test_service_name() -> None:
        assert payload()["service"] == "jeff-demo"


    def test_status_deliberate_failure() -> None:
        # Intentional mismatch: the real service contract currently returns "ok".
        assert payload()["status"] == "ready"
''')

COMPOSE = dedent("""\
    services:
      api:
        image: python:3.12-slim
        command: ["python", "/app/service.py", "--host", "0.0.0.0", "--port", "8000"]
        volumes:
          - ./service.py:/app/service.py:ro
        ports:
          - "127.0.0.1::8000"
        environment:
          PYTHONDONTWRITEBYTECODE: "1"
        user: "65534:65534"
        read_only: true
        cap_drop: [ALL]
        security_opt: ["no-new-privileges:true"]
        healthcheck:
          test:
            - CMD
            - python
            - -c
            - >-
              import urllib.request;
              urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).close()
          interval: 2s
          timeout: 3s
          retries: 5
""")

IGNORE = dedent("""\
    __pycache__/
    .pytest_cache/
    .venv/
    /.jeff-demo.json
    /jeff.local.yaml
    /.jeff/
""")

COMMON_README = """
## Ownership and repeat generation

This is a generated, disposable standalone Git repository, not Jeff application source.
The baseline is a deterministic local commit on `main` with synthetic author identity.
`.jeff-demo.json` records generator/version/project and `baseline_commit`; it is ignored.
`demo-notes.txt` is intentionally untracked so `what changed?` has a visible example.

Running the generator again preserves all existing files, branches, commits, notes, and
Jeff registration/settings. It does not reset or repair modified generated repositories.
A missing/invalid ownership marker or unsafe directory is refused, not overwritten.
The marker is a recognition receipt, not an authentication or integrity guarantee.

## Inspecting or restoring the fixture

First inspect `git status` and `git diff -- test_service.py` (sandbox only). If you
explicitly choose to discard edits to a particular fixture file, copy `baseline_commit`
from `.jeff-demo.json`, then use `git restore --source=BASELINE_COMMIT -- FILE`, replacing
both uppercase placeholders with that observed hash and the exact chosen filename.
This is a deliberate destructive action for that file, not an automatic setup step.
Never include `jeff.local.yaml` or unrelated files. The generator has no reset/delete flag.

For a fresh copy without discarding anything, rerun the generator with `--output-dir`
pointing at a new location whose parent already exists. Do not use broad `git clean` or
`git reset --hard` as an initialization procedure. Incomplete creation needs manual
inspection; no automatic rollback/deletion is attempted.

## Jeff checks

With Jeff's development venv activated, from this repository:

```bash
jeff init --json
jeff config --json
jeff status --json
jeff ask "what branch am I on?"
jeff ask "what changed?" --json
```

Init is a separate deliberate step; generation does not register projects. Later Jeff
runtime/test/Docker orchestration is not implemented by these fixture files. No real
credentials or environment files are needed.
"""

SANDBOX_README = (
    """# Sandbox demo project

Synthetic M01 fixture for later M04 test and M05 process/Docker demonstrations.

## HTTP service

Using Python 3.12+ from this directory:

```bash
python service.py --port 0
```

The default host is loopback `127.0.0.1`. Port 0 binds an OS-allocated free port directly;
the first stdout line reports JSON with the actual port and `/health` URL. `/health`
returns `{"status": "ok", "service": "jeff-demo"}`; other paths return 404, never files.
Use `--port NUMBER` for a selected port, replacing NUMBER with your actual choice.
Stop your foreground service with Ctrl-C. Binding `0.0.0.0` requires explicit `--host`
and is intended for the container, not the default local demo.

## Deliberately failing pytest fixture

With the Jeff development venv activated:

```bash
python -m pytest test_service.py -q
```

Expected baseline: **1 passed, 1 failed, exit code 1**. The failing test expects `ready`
while `service.payload()` actually returns `ok`; this is intentional, not a Jeff regression.
No HTTP listener is started by the tests. The parent Jeff suite only collects `tests/`,
not these generated example tests. The package/install commands belong in Jeff's root README.

## Compose fixture

`compose.yaml` defines an `api` service using `python:3.12-slim`, a read-only mount of
**only** `service.py`, a local health check, and an OS-allocated loopback host port.
No repository-wide or credential mounts. There are no environment-file prerequisites.

Syntax validation, if Docker Compose is installed:

```bash
docker compose --env-file /dev/null -f compose.yaml config --quiet
```

The image tag is a declared fixture input, not a pulled/verified image or digest-locked
release dependency. Real pull/start/health/shutdown acceptance belongs to M05 and needs
separate approval. Generation and M01 syntax validation do not pull images, build, start
containers, or claim Docker runtime acceptance. A later approved run can discover its
actual mapped port with `docker compose port api 8000`.
"""
    + COMMON_README
)

SECOND_README = (
    """# Second demo project

Minimal synthetic M01 repository for target/identity checks. It has no service or tests.
It is separate from sandbox-project and the parent Jeff repository; no remotes exist.
"""
    + COMMON_README
)


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
