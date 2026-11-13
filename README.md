# Jeff

Local, human-supervised developer assistant. **M02 is accepted on macOS:** explicit daemon lifecycle, authenticated local IPC, durable project state/history/events, read-only doctor and proven owned recovery. Desktop, voice, models, general execution, Pi/MCP/SSH and broader supervision remain future milestones—not shipped features. [Product scope](docs/jeff-design.md).

The accepted M02 baseline passed697 source/343 fresh installed cases with33 source/build/wheel/site module matches. Later test cleanup collected692 source cases/selected290 installed and ran112+9 affected cases; the comments/docstrings pass proved unchanged executable AST and passed static checks, without another runtime/package run. These are distinct records, not a new full-matrix pass. [Current status](docs/jeff-progress.md), [M02 completion/limitations](.agent-work/reports/m02-engineering-completion.md).

Jeff targets macOS only; Linux client/runtime compatibility is out of scope. General local code/test/build jobs are planned for explicitly declared Docker environments (potentially Linux guests), not transparent host-venv execution. Bounded disposable Docker feasibility passed, but product execution is not implemented/accepted; it does not containerize Jeff or change the native installation/development commands below. [Decision](docs/adr/0005-declared-docker-job-environments.md), [milestone impacts](docs/specs/m03-docker-milestone-impact.md). Alternate-user, hosted-CI and fresh-machine execution remain deferred/unperformed. M01 Mac automated/manual evidence exists; its formal acceptance/licensing disposition is not inferred from M02 acceptance. No project license/distribution route is established. Local docs/evidence/toolkit files are ignored and may be absent in a fresh clone.

## Install

From the **Jeff source root**, use Python3.12+ and system Git. Recorded runtime validation is macOS/CPython3.12.11. `/usr/bin/git` avoids an incompatible non-system Git on the reference Mac.

```bash
python3 --version
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

For development, install this **instead** of the final command:

```bash
python -m pip install -r requirements-dev.lock -e '.[dev]'
```

The lock is a tested Mac/Python snapshot, not a hash-verified cross-platform/build-tool lock. `requirements.txt` alone contains only the compatibility PyYAML pin and does not install the CLI. Resolving with `python -m pip install -e '.[dev]'` is an alternative, not a cross-platform validation claim.

Reactivate the existing venv in later terminals before changing to a target project; unchanged dependencies need no reinstall. Without activation, use the absolute path to `.venv/bin/jeff`. `deactivate` restores the shell environment—it does not stop the daemon or delete registration.

## Start and use a project

```bash
jeff --help
python -m jeff --help
jeff start --json                 # Explicit; works outside Git.
cd /path/to/your/project          # Choose the intended repository deliberately.
jeff init --json
jeff config --json
jeff status --json
jeff ask "what branch am I on?"
jeff ask "what changed?" --json
jeff stop --json                  # Also works outside Git.
```

- Help/syntax performs no project/runtime selection or state creation. No arguments/unknown commands exit2.
- Start waits for authenticated full readiness; repeat/concurrent starts converge. There is no autostart or in-process action fallback. Current Phase2 development uses wire7/DB5/settings2 (accepted M02 used wire6/DB4/settings1). Legacy settings1 is read without rewriting; older wires are incompatible; stop an older instance using its matching environment before upgrading. No automatic rekey/owner replacement.
- Init writes mode0600 `jeff.local.yaml` with stable UUID/canonical root and appends root-anchored private-state ignore rules. Repeated init preserves identity/settings. Subdirectories bind the same root; moved-root rebind is not implemented. Unsafe/tracked/malformed/moved configuration refuses rather than regenerating identity.
- Config exposes identity only. Status adds runtime facts after local registration validation, works stopped and never indexes/initializes. Questions record minimal allowlisted request metadata but do not write project files.
- Stopped init/config/ask/history/debug return `daemon_not_running`, not fallback execution. Exiting the CLI/deactivating the venv does not stop the daemon. Stop drains actual work and confirms native exit; unconfirmed shutdown is an error, not permission to signal a PID.
- Success exits0, recognized failures1, syntax2. JSON keeps `ok/data/error`; duplicate/uncertain correlation is `Request: <uuid>` on **stderr**, not transport fields added to existing result bodies. Missing replies/receipts never authorize retry.

Registration belongs to a repository root, not a branch. Ignore/config publication is atomic per file, **not** a cross-file/SQL transaction; failures can leave ignore rules or published identity. Preserve and inspect partial state, never delete registration to repeat a test. `.jeff/` is reserved, not created by init.

For deliberate isolated installations, use validated absolute `JEFF_DATA_DIR` and `JEFF_RUNTIME_DIR` consistently on every command. They select locations, not credentials/authority. Agents must use approved private fixtures, not default user state.

## Git question boundaries

Only the two shown intents are accepted, with case/whitespace normalization and optional final `?`; no model call. Unsupported input is not echoed and does not invoke Git. Answers bind project/root and UTC observation time; branch metadata distinguishes named/detached/unborn HEAD.

Changes expose staged/unstaged/untracked/renamed/conflicted **metadata**, not contents/raw diffs. Protected environment-family paths (including templates), private state, credential/key names and submodules are excluded before querying and again before rendering. `complete:false`/`visible_clean` means **not full-clean proof**; `filtered_records` cannot count pre-query omissions. Naming rules are conservative, not content-based secret detection. Metadata uses reversible JSON string-body escaping, including controls/non-UTF8 filenames.

Queries never stage/commit/fetch/update the index. Fixed Git controls disable hooks/fsmonitor/transports/lazy fetch; five-second/1MiB combined-output/10,000-change limits fail without partial success. Clean/process filters cause change questions to refuse rather than execute drivers or misreport conversions; branch questions disable discovered filters. This is not a general sandbox or atomic repository snapshot. [Git adapter](src/jeff/integrations/README.md).

## History, debug and committed events

Inside the registered project with the daemon started:

```bash
jeff debug last --json
jeff debug last --request-id <request-uuid> --json
jeff history --limit 50 --json
jeff history --cursor <opaque-cursor> --json
```

Debug selects the latest **accepted init/ask**, not latest completion or another project's request. Exact IDs stay scoped; running/unknown/legacy states remain honest. Empty history succeeds; default debug without instrumented membership returns `no_history`. Coverage starts at instrumentation/migration, not repository creation.

History defaults50/max100, with epoch/root/project/snapshot/generation-bound cursors. Membership is stable while outcome states remain current per read. Stale/foreign cursors require explicit resync. Reads do not index, timestamp accesses, trigger cleanup or archive raw questions/answers. Retention defaults30days, restart-loaded from validated private settings; batches≤100 at intervals≥60s retire only eligible succeeded/failed details. Unknown/nonterminal evidence remains and UUID-only tombstones permanently refuse replay. Missing details are not nonexecution proof.

Subscriptions use the installed client, not a follow/watch CLI:

```python
from pathlib import Path
from jeff.application.dispatch import SubscriptionQuery
from jeff.bootstrap.runtime import create_activity_client

with create_activity_client().subscribe(SubscriptionQuery(directory=Path.cwd())) as stream:
    message = stream.receive(wait_seconds=10)  # None means still idle.
    # Caller owns processing and any saved message.cursor.
```

No cursor starts live from the acknowledged committed position; a supplied cursor explicitly resumes/replays. No automatic reconnect, action replay or cursor storage. One reader per owned socket; received positions are not processing ACKs. Up to8 streams,≤64 event credits **each**, within15 ordinary/16 total native connections; internal worker/queue limits are not additive connection slots. Healthy idle differs from five-second partial-frame/write bounds. Retention invalidates stale streams; already-sent bytes cannot be recalled. Earlier null-ID init events remain exact-debug history, not retroactively routed feed events.

## Doctor and recovery

```bash
jeff doctor --json
jeff doctor
```

Doctor works outside Git, stopped or live, under one5s budget. It reads authenticated live caches or safely leased stopped settings/schema; never starts/repairs/migrates, creates a key/DB, dumps environment or opens a second live SQL reader. Active versus inactive retention is labelled; huge valid values use a finite marker. Healthy/stopped/uninitialized exit0; unhealthy/unsafe/required uncertainty1; syntax2. Atime invariance/full DB integrity/hostile-same-user isolation are not promised. [Doctor guidance](docs/runbooks/m02-doctor.md).

Only explicit start may recover supported residue under existing ownership:

- **Endpoint:** signed receipt1 matching exact namespace/key/stable-anchor/socket/self identities and a definitively refused peer, or validated receipt-only cleanup. Missing/copied/replaced/unsafe/unrecorded proof is preserved. Free lock/missing PID/connect failure alone grants no cleanup authority.
- **SQLite:** permit1 binds the actual DELETE journal before business DML. An eligible genuine hot journal is authenticated/pinned before SQLite rolls it back; current schema/activity validation precedes readiness. SQLite alone manages journal removal. Recovery-only limits: DB256MiB/journal16MiB; WAL/SHM/super-journal, unsupported proof or unproved creation/migration refuse.
- **Native Git:** a single-use helper owns actual Git plus the inherited lifetime lease; replacement stays excluded until real cleanup/helper exit. Only captured normal exits reconcile late cleanup; abnormal/unknown outcomes quarantine admission.

No key/anchor/DB reset, logged-PID signalling, fallback, action replay or caller-deadline extension. SQL rollback does not undo YAML/ignore publication. Arbitrary power loss, malicious-same-UID key access and hard native-I/O cancellation are outside the guarantee. [Recovery guidance](docs/runbooks/m02-recovery.md).

## Disposable demos

From the Jeff source root after installation:

```bash
.venv/bin/python scripts/create_demo_projects.py
```

Then explicitly start Jeff and register `examples/sandbox-project` or `examples/second-project`. Generation creates independent ignored Git roots with deterministic baselines and untracked notes; it preserves recognized existing work and refuses unrelated/unsafe targets. It never starts services/Jeff, resets work or mutates the parent index/history. Choose a new explicit output location instead of clearing user data.

The sandbox has a loopback service, a **deliberate1-pass/1-fail pytest example**, and Compose structure. Local service/expected-failure/Compose-syntax evidence is not Docker runtime success. [Examples](examples/README.md), [generator safety and commands](scripts/README.md).

## Development and safety

From the source root, after selecting a **fresh private Git-negative arena outside every checkout**:

```bash
.venv/bin/python -m pytest tests/unit tests/architecture tests/integration tests/acceptance -q
.venv/bin/python -m ruff check src tests scripts
.venv/bin/python -m ruff format --check src tests scripts
.venv/bin/python -m mypy src tests scripts
.venv/bin/python -m pip check
```

Ignored scratch inside Jeff still has Git ancestors. Pytest may clear `--basetemp`; never point it at arbitrary existing data. Native crash/compatibility acceptance needs its own finite manifest, verified fixtures and **fresh explicit fault authority**; ordinary skips are not guarded acceptance passes. The old M02 fault envelope is exhausted. [Test map and current selection](tests/README.md).

Dependencies/tool settings live in `pyproject.toml`; static architecture checks enforce selected tier rules, not dynamic isolation. Domain is stdlib-only, application owns contracts, adapters own I/O, interfaces render, bootstrap wires dependencies. [Source layout](src/README.md).

No hosted/self-hosted runner, Docker environment or deployment is enabled by keeping the local CI draft. Branches do not isolate execution/billing. Native Mac runtime evidence does not pass GUI/audio/provider work. `.env`/secret files need approval; this application does not load them. `.env.example` is a placeholder-only exception to ignores, not a credential store. Configuration reads are bounded/strict, preserve valid settings and expose no arbitrary values.

## Documentation and planned work

[Documentation index](docs/README.md) → [design](docs/jeff-design.md), [milestones](docs/jeff-implementation-plan.md), [progress](docs/jeff-progress.md), [domain glossary](docs/context/DOMAIN_MODEL.md), [architecture](docs/architecture/README.md), [runtime contract](docs/specs/m02-runtime-and-state.md). These local ignored documents may be absent from a fresh clone. Completed checkpoint plans/reports and the old M01 manual transcript are retained in the [historical archive](.agent-work/reports/m02-history-index.md), not current instructions.

The complete product still requires approved execution/jobs, desktop/hold-to-talk voice, local-first reasoning with opt-in cloud, actual Pi/MCP/GitHub/registered-SSH integrations and recovery/supervision. Models cannot approve actions; exact single-use human authorization and production-destructive denial remain design requirements, not delivered execution controls. IDE/unsaved-buffer plugins, wake-word listening, semantic/vector history and remote notification delivery are out of scope. See design for authoritative detail. M03 Phase1's declared-Docker contract and bounded feasibility proof are accepted after Sahil's manual checks. Phase2 implements durable review-only proposals/approvals/jobs; human acceptance is pending. `run --profile NAME -- EXECUTABLE ARG...` only proposes declared public argv. `approvals list/show/approve/deny` and `jobs list/show` inspect/decide durable facts. Private daemon settings must bind the project/profile; defaults contain no profiles. Never put secrets in argv: exact public manifests are stored, and known sensitive channels refuse without universal secret detection. Engine/image declarations are unverified; no Docker query/pull/launch is performed. Approved jobs remain blocked, with no launch authority. Phase2 decisions never activate later: Phase3 requires fresh proposals/approvals. Native probes are paused; the executor remains unimplemented. See the [review acceptance check](tests/acceptance/test_execution_review_cli.py) and [current evidence/limits](docs/jeff-progress.md).
