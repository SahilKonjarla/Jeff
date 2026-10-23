# Acceptance tests

**Status:** installed M01 help, project-command and branch/change-question checks passed on Mac, including the supplied user manual transcript. M02 internal control acceptance also has current feature-wheel evidence; full milestone acceptance/public CLI lifecycle and CI success are not implied.

- [test_demo_workflow.py](test_demo_workflow.py) invokes the real generator CLI from a foreign working directory, then installed Jeff initialization/config/status and both questions for each generated repository. It checks distinct/stable IDs, initial `main`/baseline/note metadata, preserved registration on rerun, help without writes, and refusal without echoing private fixture data.
- [test_cli_install.py](test_cli_install.py) checks installed console/module help and package metadata from temporary directories, with no project I/O.
- [test_project_commands.py](test_project_commands.py) runs real installed module commands against disposable Git repositories: init/config/status/repeat, stable identity from subdirectories, actual `git check-ignore`, errors outside/unregistered repositories, and safe malformed-config messages. Inherited Git targeting/config injection is deliberately supplied and must not redirect the adapter.

- [test_installed_questions.py](test_installed_questions.py) runs both installed console and module `ask` entry points from fixture subdirectories. It checks identity/time, unborn branch, scoped changes, escaped filenames in JSON/text, unsupported/unregistered errors, and unchanged config/HEAD with no index creation.

- [test_installed_runtime_control.py](test_installed_runtime_control.py): runs the installed interpreter outside source/Git with both real directory overrides, verifies parent/daemon identity, distinguishes control from application readiness, confirms survival after launcher exit and shutdown by another caller, rejects invalid inherited descriptors without state creation, and proves public `jeff start` uses that same installed implementation and full readiness. This test works in an editable installation and is also explicitly rerun in a fresh verified noneditable feature-wheel venv; an editable-only run is not wheel proof. Test keys are private/ephemeral and never printed.

- [test_installed_runtime_storage.py](test_installed_runtime_storage.py): storage/schema readiness and durable validated registry across actual restarts, plus owned legacy-v1/current hard refusal without replacement/rekey/downgrade. The compatibility case requires an explicitly verified `JEFF_TEST_V1_PYTHON`; unconfigured ordinary developer runs skip that one optional-environment case. The guarded acceptance runners require/re-hash retained inputs and execute it without skips, including a newly installed v1 environment beside the current noneditable wheel. No default installations are fixtures.

- [test_installed_project_rpc.py](test_installed_project_rpc.py): actual v3 child/native identity and init/config/both Git answers, read-only config, repeated/restarted/concurrent init identity preservation, metadata receipt/duplicate/target refusal, actual disconnect before claim/after publication, malformed/moved/linked YAML refusal, protected/renamed/escaped metadata and filter refusal, no question/config/content archive, and retained-v2 hard refusal/migration without replacement. `JEFF_TEST_V2_PYTHON` is explicitly verified/required in guarded acceptance; an ordinary developer run without that optional owned environment skips only the compatibility case. Public CLI now routes to these operations and full readiness is asserted only for actual live complete dispatch. Control-only fixtures remain not application-ready.

- [test_installed_activity.py](test_installed_activity.py): actual native/authenticated v4 daemon, real Git observations/scoped trace/replay/restart/no replay; actual retained-v3 hard refusal, matching-old stop and preserved legacy metadata. No public history/debug or live subscriptions in A. Guarded runners verify `JEFF_TEST_V3_PYTHON`; unconfigured ordinary runs may skip compatibility, guarded evidence does not.

- [test_daemon_cli_workflow.py](test_daemon_cli_workflow.py): public start/repeat/stop outside Git, daemon-dependent project/restart flow, stopped no-autostart/no-write behavior, read-only registered stopped status, lazy help/invalid overrides, actual lost response and duplicate CLI stderr-only correlation. Actual socket/random boundaries are controlled in caller subprocesses, never mocked final success; server uses real Git. Existing project/question/demo fixtures now explicitly start/stop owned installations and preserve prior safety assertions.

Project-command successes exit 0; recognized failures exit 1 with typed JSON or stderr text. Unsupported CLI syntax/commands exit 2. Config inspects identity only; status preserves identity and adds explicit runtime observations. Neither dumps arbitrary settings or implicitly indexes/writes activity.

**Dependencies:** pytest and Jeff installed in the same test Python environment (editable checkout or built wheel), plus system Git. A clean non-editable wheel/app venv was verified on Mac; an editable-only claim is stale. With multiple clones, verify the interpreter imports the intended installed package rather than another checkout. [Fixture initialization](../conftest.py) is confined to temporary repositories; no user repository index/history is changed. Test output uses synthetic private values, never real credentials or user `.env` files. Integration protected-path fixtures use synthetic data only.

Fixture temp roots must be genuinely outside all Git ancestors; see the [test-map safety note](../README.md). Do not set `--basetemp` to ignored scratch inside Jeff, and do not delete user registration just to simulate an unregistered fixture.

From the repository root after [installation](../../README.md):

```bash
.venv/bin/python -m pytest tests/acceptance -q
```

**Owning milestones:** M01 public CLI and M02 internal control lifecycle. Linux is deferred; other Python versions and a fresh OS remain unverified. Clean local wheel/venv testing is not a published release or hosted-run claim. Branch/change questions have scoped local automated/manual evidence, not full product acceptance. Internal storage readiness is covered, but no full application daemon/public lifecycle, desktop/audio/providers or remote integration acceptance is claimed. Missing hardware/credentials do not reduce required scope. See [test map](../README.md).
