# Jeff

Local, human-supervised developer assistant for **macOS**. M02 and M03 are accepted within their recorded evidence qualifications: explicit authenticated daemon lifecycle, project/Git questions, durable history, read-only diagnostics, and opt-in approved Docker jobs. Desktop, voice, reasoning providers, Pi/MCP/SSH integration, test watchers and service orchestration remain later milestones. M04 is in progress; the source-level pytest proposal and result flow has not yet been verified in Docker.

Docker is the backend for explicitly declared application jobs, not a replacement for the native Jeff daemon or permission to containerize development checks. Linux Jeff client/runtime compatibility is out of scope. [Source map](src/README.md), [CLI contract and uncertainty guidance](src/jeff/interfaces/README.md), [test map](tests/README.md).

## Install

From this source checkout, use Python3.12+ and system Git. Recorded runtime evidence is macOS/CPython3.12 on Apple ARM64. Installation is an explicit operator action, not permission for an agent to upgrade normal state:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

For development, replace the final command with:

```bash
python -m pip install -r requirements-dev.lock -e '.[dev]'
```

Dependencies and tools are declared in `pyproject.toml`. The development lock is a tested Mac/Python snapshot, not a cross-platform hash lock. `requirements.txt` alone does not install the CLI. Reuse the existing environment when dependencies have not changed; deactivation does not stop Jeff.

M03 was accepted at **wire14 / DB10 / guarded opt-in settings4 / default settings2**. In-progress M04 source uses **DB11** for pytest result storage; the application and SQLite still run natively on macOS. Legacy settings1/2/3 remain parseable; settings3 alone cannot enable new configured guarded launches. Match client and daemon installations. Older binaries do not support DB11 or downgrade. Verification uses isolated fixtures, not a normal-installation migration or fresh-machine distribution test.

## Pytest jobs (M04 in progress)

With an explicitly configured guarded `pytest` profile whose pinned image contains
`/opt/jeff/pytest_job_runner.py`, run the following from the registered project:

```bash
jeff tests run --input service.py --input test_service.py
jeff execution approve <approval-id> --digest <manifest-sha256>
jeff execution start <approval-id> --digest <manifest-sha256>
jeff tests results <job-id> --json
```

`tests run` only proposes. List every reviewed input explicitly; Jeff does not
copy the whole checkout or prepare the image. The result uses the job ID, not
the approval ID. A missing or rejected report is not a substitute for the job’s
exit status. The report is separate from ordinary logs and expires with them.

## Start and use a project

```bash
jeff --help
jeff start --json
cd /path/to/your/project
jeff init --json
jeff config --json
jeff status --json
jeff ask "what branch am I on?"
jeff ask "what changed?" --json
jeff history --limit 50 --json
jeff debug last --json
jeff doctor --json
jeff stop --json
```

Choose the project deliberately; the path above is illustrative. Start/stop/doctor work outside Git. Help and syntax perform no project/runtime selection. There is no autostart or client-local action fallback. Stopped init/config/ask/history/debug fail instead of executing locally; status remains read-only.

Init preserves stable repository-root identity and private configuration. Registration is not branch-specific. Unsafe/tracked/moved configuration refuses; never delete state to repeat a test. YAML/ignore/SQL publication is not one atomic transaction. Private absolute `JEFF_DATA_DIR` and `JEFF_RUNTIME_DIR` overrides select locations, not credentials or authority; use them consistently. Agents need approved synthetic fixtures rather than default user state.

Questions are deterministic and model-free. Change answers expose filtered metadata, not raw diff/file contents or complete-clean proof. Protected paths/submodules and filter-backed change reads refuse or are excluded; fixed Git argv does not permit arbitrary commands. [Git boundary](src/jeff/integrations/git/README.md).

History/debug reads are project-scoped, bounded and preserve unknown/retired distinctions. Debug selects accepted init/ask requests, not arbitrary latest completions. Event subscriptions are an installed-client API, not a follow/watch CLI. Doctor is read-only: no autostart, repair, migration, live SQL reader or secret dump. [Runtime contracts](src/jeff/application/runtime/README.md), [diagnostics](src/jeff/infrastructure/runtime_diagnostics/README.md).

## Approved execution — explicitly configured, default disabled

A trusted installation owner must supply private settings4 backend declarations, verified CLI/config/HOME resources, pinned installed JFG2 guard artifacts/timing policy, and explicitly classified **development** profiles. Startup does not discover/start Docker, pull images or run jobs. Readiness is not proof an engine/image is available. No permissive host or unguarded fallback exists.

These are syntax examples, not runnable acceptance handoffs. Replace IDs, profile, inputs and reviewed digest with actual inspected values:

```text
jeff execution propose --profile NAME --input source.py --request-id UUID --json -- EXECUTABLE ARG...
jeff execution approve APPROVAL_ID --digest REVIEWED_SHA256 --request-id UUID --json
jeff execution deny APPROVAL_ID --digest REVIEWED_SHA256 --request-id UUID --json
jeff execution start APPROVAL_ID --digest REVIEWED_SHA256 --request-id UUID --json
jeff execution cancel APPROVAL_ID --digest REVIEWED_SHA256 --request-id UUID --json
jeff execution show APPROVAL_ID --json
jeff execution logs APPROVAL_ID --json
jeff execution approvals list --limit 16 --json
jeff execution jobs list --limit 16 --after PREVIOUS_NEXT_AFTER --json
```

- Review the **full outer guarded manifest** and approve its exact digest. Old review-only/unguarded approvals never become guarded authority. Legacy `run`/`approvals`/`jobs` remain permanently review-only.
- Inputs are literal project-relative files, not globs/directories. The owner/operator must review argv and file contents as public/non-secret: snapshots and metadata can persist before approval. Names, hashes, read-only mounts and redaction do not certify arbitrary contents or destructive intent. Production/unclassified targets refuse.
- Start acknowledges a confirmed claim, not guest readiness/success. Cancel acknowledges intent, not termination. Retain request UUIDs and reconcile through scoped show/logs; missing replies never authorize retry.
- Fresh lists are project-scoped newest-first pages (default16/max32), not snapshots. A reserved job UUID is not a launched job. Use approval IDs for show/logs/cancel. Discovery never transfers mutation authority.
- Unavailable target verification before preparation/authority returns `execution_unavailable`, without download/fallback. The same raw fault after persistence/claim or uncertain helper cleanup remains unknown; do not infer nonexecution from an error code alone.
- Only the original captured worker/controller can stop/remove its verified owned job. Guard terminal, namespace exit, output EOF, removal and native daemon exit are separate facts. Incomplete output stays labelled incomplete.
- Unknown work stays quarantined/consumed across restart. No automatic retry, adoption, PID/CID/label-based control, DB reset or quarantine-clear API exists. Independent administrative removal does not rewrite Jeff's unknown outcome or refund authority. See [operator guidance](src/jeff/interfaces/README.md#m03-policy-and-uncertain-work-guidance).

`jeff stop` drains actual work. It may honestly return `runtime_stop_unconfirmed` even when the original daemon child subsequently exits: native exit alone is not confirmation of a job outcome/cleanup. Ctrl-C/disconnection/deactivating the venv is not cancellation.

## Verification scope

M03 includes matched installed native success, typed nonzero exit versus guard timeout, owned cancel/cleanup, natural approval expiry and restart/no replay; real missing engine/image refusal; active shutdown; and operator-performed owner/provider interruption with honest unknown/log inspection and verified stopped-orphan administrative removal. Original failed scopes remain failed, not retrospectively passed.

The matched four-job proof uses wheel `cf202f…`. The availability correction's wheel `09fbcd…` has focused installed guard/availability regressions and actual native unavailable/interruption proof; no full four-job rerun on that newer wheel is claimed. Source/fake, installed, native, physical and human evidence are different tiers, not additive unique counts. Current overall accounting is36actual/53charged, not new execution capacity.

Limits remain explicit: no universal clock-floor/hostile-pressure guarantee, independent VM-wide task census, universal secret/destructive classifier, AMD64 runtime proof, complete engine privacy audit, fresh-machine upgrade or current full-suite/hosted-CI pass. Both Linux guard architectures are packaged; runtime evidence is selected ARM64 Docker Desktop. The GitHub workflow is published under explicit approval; its runs/output have not been audited here. No deployment or later-milestone result follows.

Detailed acceptance/history lives in local ignored `docs/specs/m03-acceptance-checklist.md`, `docs/jeff-progress.md` and `.agent-work/`; those may be absent from fresh clones. Component READMEs keep the operational contracts available without private receipts. Publication is not normal-state migration authority.

## Development and safety

From the source root in the existing development environment, these are repository check commands—not a report that they were run during documentation work:

```bash
.venv/bin/python -m pytest tests/unit tests/architecture tests/integration tests/acceptance -q
.venv/bin/python -m ruff check src tests scripts
.venv/bin/python -m ruff format --check src tests scripts
.venv/bin/python -m mypy src tests scripts
.venv/bin/python -m pip check
```

Select an approved finite test set and a **fresh private Git-negative arena outside every checkout** before execution. Ignored scratch inside Jeff is still inside Git ancestry. Pytest may clear `--basetemp`; never target existing arbitrary data. Native process/crash tests, provider operations and Docker jobs require their own finite authority; skips and historical grants are not permission or passes. Documentation edits do not justify replaying spent handoffs.

Python files stay within400 code-bearing lines; docstrings, comment-only/blank lines and multiline literal spans are excluded by the shared AST/token counter. Ruff's100-character line limit is separate. Domain is stdlib-only; application owns contracts, infrastructure/integrations own I/O, interfaces render and bootstrap composes. [Architecture](tests/architecture/README.md).

`.env`/secret-bearing files need explicit approval; Jeff does not load them. No project distribution/license route is inferred. [Agent guidance](AGENTS.md), where present, preserves separate approval gates for publication, dependencies, infrastructure and milestone transitions.

## Disposable examples

The existing generator creates independent ignored repositories with deterministic baselines, preserving recognized existing work and refusing unrelated targets:

```bash
.venv/bin/python scripts/create_demo_projects.py
```

It never starts Jeff/services or resets work. Its deliberate1-pass/1-fail pytest and Compose fixtures do not imply M04/M05 runtime delivery. [Examples](examples/README.md), [generator contract](scripts/README.md).
