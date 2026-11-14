# Tests

**Status:** M02 accepted; subsequent approved test-only cleanup has **334 test functions/692 collected source cases** and an explicit **290-case installed selection**. The accepted M02 baseline was697 source/343 installed passes; those immutable receipts remain historical evidence. Cleanup validation ran112 affected source +9 installed cases, zero skips, plus Ruff/format/mypy—not a fresh full692/290 execution. [Cleanup scope/evidence](../.agent-work/reports/m02-test-cleanup.md). Jeff targets macOS only. Alternate-user/hosted validation remains deferred/unperformed.

| Directory | Scope and current implementation |
| --- | --- |
| [unit](unit/README.md) | CLI, project identity, question routing, disclosure/escaping policy, controlled ports |
| [contract](contract/README.md) | No separate suite yet; current ports exercised by unit/integration checks |
| [integration](integration/README.md) | Real storage/discovery/Git states, limits, filter safety, error/race boundaries |
| [acceptance](acceptance/README.md) | Installed lifecycle/project/questions/restart/status, actual ignores, native identity, safe errors and stderr-only correlation |
| [architecture](architecture/README.md) | Static tier restrictions, relative imports, and positive/negative checker cases |

## Source and installed selection

Source retains the full behavior/security/SQL/real-deadline matrix. The versioned installed selection is [test-cleanup-installed-manifest.json](../.agent-work/temporary/m02-s5a/test-cleanup-installed-manifest.json), following the existing explicit-manifest acceptance workflow; pytest does **not** automatically apply it. It retains real installed CLI/lifecycle/native-peer/helper/compatibility/crash cases, while doctor model/CLI-port unit cases and the real35-second unconfirmed-stop proof run source-only. Two standalone acceptance scenarios were merged without dropping their assertions. This is51 duplicate installed executions omitted plus2 merged cases—not53 deleted source tests.

Both delivery-hint paths, all crash/COMMIT/publication/security cases, actual backpressure triggers and one real deadline proof remain. Full guarded crash gates require new explicit fault authority; the accepted M02 envelope is exhausted. Do not rerun a full native acceptance matrix as ordinary cleanup verification or call unrun selected cases passed.

## M03 Phase2 — accepted; separate historical evidence

The initial approved source selection passed231 cases; five targeted legacy nonfault regressions subsequently passed after schema5 readiness expectations were updated. The reduced storage closeout adds exactly four source tests (no parameterized expansion): active-caller retirement failure, signed legacy format/migration ordering, DDL rollback preserving the legacy DB, and permit-publication refusal before DDL. The first reproduced the expected failure before its fix; all four green checks passed once. Do not rerun faults under historical M02 authority.

A fresh offline noneditable wheel matches35 production modules and passes dependency/console/module checks. Installed discovery accidentally expanded from two named tests to all four copied cases because the lead included a trailing `.`; all passed, including an extra client lost-reply/duplicate-UUID fault outside that slice's scope. The mistake/evidence was disclosed and work paused; Sahil subsequently approved static/documentation closeout, manual handoff, testing-clone sync and commit/push, not additional runtime tests. This is not a fresh full-suite pass or new physical-crash/hot-journal proof. A final doctor-contract compatibility correction (settings1/2, DB5, eleven wire7 capabilities) was separately approved and validated by one combined pure-model red→green case. The refreshed wheel matches35 current modules and passes dependency/help checks; the earlier native cases were not rerun on it. Separate malformed catalog/ledger/proof-mismatch coverage remains deferred. See [progress](../docs/jeff-progress.md) for exact counts, snapshots and limitations.

## M03 Phase3 Step1 — source-only internal authority foundation

The new pure inventory/manifest contract test and five real-SQL authority tests cover distinct legacy/fresh manifests, one-time consumption/non-replayed claim responses, cross-ledger UUID exclusion, concurrent claims, scope/expiry/current-binding/clock refusal, normal restart invalidation/unknown quarantine and clean schema5→6 preservation. These are synthetic internal verifier bindings, not Docker/input-copy verification or physical-crash evidence. A finite source selection passed132 cases in2.90s with no skips;95 frozen Python hashes matched before/after. It includes affected pure contract/codec/architecture checks and selected nonfault legacy SQL/review cases, **not** the full suite, installed/native CLI or old four fault cases. Wire8/DB6/settings2 fixtures retain intentional old/future protocol refusal and legacy migration semantics. No Docker calls or containers; the six-container Phase3 budget remains unused.

## Phase3 Step2 metadata slice — not container execution

`test_docker_target_verification.py` has six synthetic test functions, most recently6passed in0.20s. The combined real-pipe/local-UDS/fake-child regression covers bounded sanitized failure facts, stderr-on-success refusal, raw Config schema compatibility, active-extra refusal, cache creation under a distinct HOME, config immutability and home reuse/overlap refusal. No native child or container is created by that synthetic test. A separate explicitly approved five-query real Docker metadata preflight passed1.9537s;97 source hashes remained unchanged. Earlier ancestry/template/cache refusals and diagnostic evidence remain preserved. No effective isolation, input copying, owned lifecycle, installed job or crash/recovery proof is implied; container budget0/6. See current progress for exact receipts and scopes.

**Owning milestone:** M01 establishes registration, the two Git questions and independent demo fixtures. Separate Mac speech/model/hotkey feasibility probes passed, but are not this pytest suite or shipped integrations. Hosted CI is not active; Docker/self-hosted CI alternatives are not configured. Docker is now selected for future application jobs, not for automatically moving this native macOS validation suite into containers. Separately approved disposable M03 Docker probes have run; those finite source-level receipts are not this pytest suite, installed Jeff acceptance or production jobs. Linux client/runtime verification is outside product scope.

From the repository root after installing dev dependencies:

```bash
.venv/bin/python -m pytest tests/unit tests/architecture tests/integration tests/acceptance -q
.venv/bin/python -m ruff check src tests scripts
.venv/bin/python -m ruff format --check src tests scripts
.venv/bin/python -m mypy src tests scripts
```

See [root setup](../README.md) for the pinned dependency snapshot and platform limits. Run pytest with the interpreter where the intended checkout is installed; do not mix two clones' venvs or count the intentionally empty contract directory as test evidence.

**Critical temp boundary:** `tmp_path`/custom `TMPDIR`/`--basetemp` must be genuinely outside every Git checkout. Ignoring `.agent-work/` does not stop Git discovery at that directory. An earlier nested-temp harness wrongly registered its parent root; guarded external-temp reruns fixed the harness, not the application. Verify the external arena with actual system Git before any init. Pytest may clear an existing `--basetemp`, so use only a fresh, owned disposable directory, never an arbitrary user path. No automatic repository/config cleanup is part of running tests.

Acceptance subprocesses use those temporary working directories and controlled environments. `conftest.py` binds Git initialization/staging/commits/branches/configuration/conflict setup to disposable repositories only, with synthetic test identity and disabled hooks/signing. The production adapter performs narrow read-only queries. With the required outside-Git fixture boundary, tests target owned disposable data, not user repositories or real `.env` contents. No provider calls; real daemon startup/shutdown uses explicitly owned private external installations. CLI action/demo fixtures explicitly start/stop them; help/stopped behavior has separate no-state/no-fallback proofs; protected-path fixtures contain only synthetic values. The HTTP checks use ephemeral loopback, not external network services.

Demo tests add generator refusal/preservation, deterministic independent baselines, parent Git immutability, and installed Jeff workflows against both generated roots. HTTP tests briefly bind loopback only and stop their owned processes. A nested pytest run deliberately returns 1 with 1 pass/1 failure; its parent test checks that expected result. Generated examples are outside the main `testpaths`, so that deliberate failure is not a main-suite regression. Compose structure is checked without invoking Docker inside the suite; separate local syntax validation is recorded in the tracker.

Tests use pytest only. Safe controlled fakes do not replace real integration/hardware acceptance as future milestones arrive. Destructive checks require approved disposable targets, never user repositories or production resources. See [source tiers](../src/jeff/README.md).
