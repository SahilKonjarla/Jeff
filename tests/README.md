# Tests

**Status:** initial M01 tests implemented; no full product or milestone acceptance is claimed.

| Directory | Scope and current implementation |
| --- | --- |
| [unit](unit/README.md) | CLI, project identity, question routing, disclosure/escaping policy, controlled ports |
| [contract](contract/README.md) | No separate suite yet; current ports exercised by unit/integration checks |
| [integration](integration/README.md) | Real storage/discovery/Git states, limits, filter safety, error/race boundaries |
| [acceptance](acceptance/README.md) | Installed console/module project/questions, actual ignores, safe errors |
| [architecture](architecture/README.md) | Static tier restrictions, relative imports, and positive/negative checker cases |

**Owning milestone:** M01 establishes project registration and this first public-interface path. Branch/change questions are implemented; demo generation is also implemented; technical probes, CI, and full platform checks remain pending.

From the repository root after installing dev dependencies:

```bash
.venv/bin/python -m pytest tests/unit tests/architecture tests/integration tests/acceptance -q
.venv/bin/python -m ruff check src tests scripts
.venv/bin/python -m ruff format --check src tests scripts
.venv/bin/python -m mypy src tests scripts
```

See [root setup](../README.md) for the pinned dependency snapshot and platform limits. Acceptance subprocesses use temporary working directories and controlled environments. `conftest.py` binds Git initialization/staging/commits/branches/configuration/conflict setup to disposable repositories only, with synthetic test identity and disabled hooks/signing. The production adapter performs narrow read-only queries. No real `.env` files, user repository mutations, providers, external network calls, or daemon startup are involved; protected-path fixtures contain only synthetic values.

Demo tests add generator refusal/preservation, deterministic independent baselines, parent Git immutability, and installed Jeff workflows against both generated roots. HTTP tests briefly bind loopback only and stop their owned processes. A nested pytest run deliberately returns 1 with 1 pass/1 failure; its parent test checks that expected result. Generated examples are outside the main `testpaths`, so that deliberate failure is not a main-suite regression. Compose structure is checked without invoking Docker inside the suite; separate local syntax validation is recorded in the tracker.

Tests use pytest only. Safe controlled fakes do not replace real integration/hardware acceptance as future milestones arrive. Destructive checks require approved disposable targets, never user repositories or production resources. See [source tiers](../src/jeff/README.md).
