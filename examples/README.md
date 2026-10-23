# Examples and demo repositories

**Status:** M01 generator and local fixture checks implemented; later Jeff test/process/Docker orchestration is not implemented by these examples.

## Generate from the source root

```bash
.venv/bin/python scripts/create_demo_projects.py
```

Requires Python 3.12+ and system Git. Creates these separate, parent-ignored local repositories:

| Location | Contents |
| --- | --- |
| `sandbox-project/` | README, tiny standard-library HTTP service, pytest fixture, Compose service, private ignore rules |
| `second-project/` | Minimal README/ignore baseline for project identity/targeting checks |

Each has a deterministic initial commit on `main`, synthetic author, no remotes, ignored `.jeff-demo.json` receipt with the baseline hash, and intentionally untracked `demo-notes.txt`. No Jeff registration is created. Their READMEs are generated with the source, not expected in a fresh clone until generation.

Repeats preserve all existing work and registrations. The script refuses unrelated/unsafe/incomplete targets and performs no automatic reset/delete/repair. For a fresh copy, use the [generator's `--output-dir` interface](../scripts/README.md) with a new location. Inspect diffs before deliberately restoring any single fixture file from its observed baseline; never discard ignored Jeff settings or use broad cleanup as setup.

## Use with Jeff

After the [root install](../README.md), activate Jeff's venv, then enter either generated repository:

```bash
jeff start --json  # Explicit daemon startup; no autostart for project commands.
jeff init --json
jeff config --json
jeff status --json
jeff ask "what branch am I on?"
jeff ask "what changed?" --json
```

Expected initial branch: `main` with a real commit. Expected initial visible change: untracked `demo-notes.txt`. Repeated init preserves the ID; the two repositories get distinct IDs. Public questions execute through the shared daemon. Stop with `jeff stop --json`; status remains read-only/usable while stopped, but init/config/ask require explicit startup. General multi-project job/process supervision remains future work.

## Sandbox service and tests

From `sandbox-project/` with the development venv activated:

```bash
python service.py --port 0
```

The service binds `127.0.0.1` by default and reports the actual free port and `/health` URL as its first JSON stdout line. The OS allocates/binds the port once, avoiding a free-port probe race. Health returns status `ok`; other paths return 404, not files. `--port` selects a specific port; `--host 0.0.0.0` is explicit and intended for the container, not the default demo. Stop your foreground process with Ctrl-C.

```bash
python -m pytest test_service.py -q
```

**Expected: 1 passed, 1 failed, exit 1.** The test deliberately expects status `ready` while the implementation returns `ok`. This is a reproducible fixture failure, not a failing Jeff regression. No listener is started by the fixture tests; Jeff's main suite collects only `tests/`.

## Compose scope

From the generated sandbox:

```bash
docker compose --env-file /dev/null -f compose.yaml config --quiet
```

The declared `python:3.12-slim` service mounts only `service.py` read-only, runs unprivileged, and publishes an allocated host port on loopback. No full-repository/credential mounts or environment-file prerequisites. The image tag has not been pulled, runtime-verified, or digest-locked. Real service lifecycle acceptance is M05, separately approved; syntax validation is not Docker runtime acceptance. No images/containers are created by the generator.

**Validation:** generator/refusal tests, real HTTP and expected pytest outcome, parent Git isolation, repeated preservation, and installed Jeff checks are linked from [scripts](../scripts/README.md) and [tests](../tests/README.md). Local macOS/CPython evidence does not establish Linux compatibility; Linux validation is explicitly deferred. The Compose file is a demo service fixture, not a Docker CI implementation. Linux containers on a Mac would not validate native Mac GUI/global-shortcut behavior. Do not modify generated README copies to change the templates or reset an existing demo to rerun tests.

**Ownership:** M01 establishes reproducible fixtures; M04 develops failure/retry workflows; M05 integrates processes/Docker. Generated data is synthetic. Credentials, provider endpoints, SSH hosts, and production resources remain outside these examples.
