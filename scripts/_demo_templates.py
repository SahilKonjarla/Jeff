"""Synthetic demo file contents only; no generation, Git, service or Docker execution."""

from textwrap import dedent

SERVICE = dedent('''\
    """Synthetic HTTP fixture: no static files, credentials, or external services."""

    import argparse
    import json
    import sys
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from socketserver import TCPServer


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


    class NumericHTTPServer(HTTPServer):
        def server_bind(self) -> None:
            # Both allowed hosts are numeric; readiness must not depend on reverse DNS.
            TCPServer.server_bind(self)
            self.server_name, self.server_port = self.server_address[:2]


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
            with NumericHTTPServer((args.host, args.port), Handler) as server:
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
