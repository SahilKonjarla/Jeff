# Tests

**Status:** M01 suites implemented. Last agent-recorded guarded macOS run: **126 passed**, plus Ruff/format/mypy/pip checks. The supplied manual CLI transcript passed; the user also reports a repeat local test run, without an additional log/count supplied. Do not invent new counts or treat this as hosted CI, full product coverage or automatic milestone acceptance.

| Directory | Scope and current implementation |
| --- | --- |
| [unit](unit/README.md) | CLI, project identity, question routing, disclosure/escaping policy, controlled ports |
| [contract](contract/README.md) | No separate suite yet; current ports exercised by unit/integration checks |
| [integration](integration/README.md) | Real storage/discovery/Git states, limits, filter safety, error/race boundaries |
| [acceptance](acceptance/README.md) | Installed console/module project/questions, actual ignores, safe errors |
| [architecture](architecture/README.md) | Static tier restrictions, relative imports, and positive/negative checker cases |

**Owning milestone:** M01 establishes registration, the two Git questions and independent demo fixtures. Separate Mac speech/model/hotkey feasibility probes passed, but are not this pytest suite or shipped integrations. Hosted CI is not active; Docker/self-hosted alternatives are not configured. Linux verification is explicitly deferred.

From the repository root after installing dev dependencies:

```bash
.venv/bin/python -m pytest tests/unit tests/architecture tests/integration tests/acceptance -q
.venv/bin/python -m ruff check src tests scripts
.venv/bin/python -m ruff format --check src tests scripts
.venv/bin/python -m mypy src tests scripts
```

See [root setup](../README.md) for the pinned dependency snapshot and platform limits. Run pytest with the interpreter where the intended checkout is installed; do not mix two clones' venvs or count the intentionally empty contract directory as test evidence.

**Critical temp boundary:** `tmp_path`/custom `TMPDIR`/`--basetemp` must be genuinely outside every Git checkout. Ignoring `.agent-work/` does not stop Git discovery at that directory. An earlier nested-temp harness wrongly registered its parent root; guarded external-temp reruns fixed the harness, not the application. Verify the external arena with actual system Git before any init. Pytest may clear an existing `--basetemp`, so use only a fresh, owned disposable directory, never an arbitrary user path. No automatic repository/config cleanup is part of running tests.

Acceptance subprocesses use those temporary working directories and controlled environments. `conftest.py` binds Git initialization/staging/commits/branches/configuration/conflict setup to disposable repositories only, with synthetic test identity and disabled hooks/signing. The production adapter performs narrow read-only queries. With the required outside-Git fixture boundary, tests target owned disposable data, not user repositories or real `.env` contents. No provider calls or daemon startup are part of this suite; protected-path fixtures contain only synthetic values. The HTTP checks use ephemeral loopback, not external network services.

Demo tests add generator refusal/preservation, deterministic independent baselines, parent Git immutability, and installed Jeff workflows against both generated roots. HTTP tests briefly bind loopback only and stop their owned processes. A nested pytest run deliberately returns 1 with 1 pass/1 failure; its parent test checks that expected result. Generated examples are outside the main `testpaths`, so that deliberate failure is not a main-suite regression. Compose structure is checked without invoking Docker inside the suite; separate local syntax validation is recorded in the tracker.

Tests use pytest only. Safe controlled fakes do not replace real integration/hardware acceptance as future milestones arrive. Destructive checks require approved disposable targets, never user repositories or production resources. See [source tiers](../src/jeff/README.md).
