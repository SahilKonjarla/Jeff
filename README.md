# Jeff

A local developer assistant for macOS and Linux: one text and hold-to-talk voice control surface for your projects, coding sessions, and development tools.

> **Status: M01 macOS automated and manual checks passed; formal handoff pending.** Packaging, project registration/configuration inspection, two read-only Git questions, and reproducible demos work locally. Clean-environment tests and bounded technical probes passed, and the supplied test-branch transcript matched the manual checklist. Linux validation is deferred; no hosted CI run is claimed. M02 now has an internal macOS authenticated control lifecycle, strict private settings and a versioned SQLite project registry. M02 slice 4 adds authenticated daemon-routed init/config and both Git questions, minimal request receipts and guarded identity/partial-write handling. Slice-5 checkpoint A implements internal scoped durable evidence/trace replay tied to actual Git observations, but its final source/installed validation was interrupted and remains unconfirmed. Public history/debug, live subscriptions and expiry are not yet implemented. Public `start`/`stop` and runtime-aware `status` are implemented; startup requires complete dispatcher/storage readiness. No autostart or in-process fallback. Public history/debug, live event subscriptions/retention expiry, doctor/stale recovery and later product behavior remain unimplemented; full M02 acceptance is pending.

## What Jeff is being built to do

- Answer project questions and supervise multiple projects through a shared local daemon.
- Run commands, pytest, development processes, and Docker workflows with explicit approvals.
- Launch and coordinate Pi coding sessions and specialized agents, with human-approved handoffs.
- Provide a PySide6 desktop console, macOS menu bar/Linux tray, and hold-to-talk voice interaction.
- Inspect Git, GitHub/CI, repository symbols, and explicitly registered SSH hosts.
- Connect to approved MCP servers over native stdio, Docker stdio, and authenticated remote HTTP.
- Retain redacted structured activity, recover jobs and watchers safely, and deliver local notifications.

These are planned requirements, not claims of available functionality. See the [design](docs/jeff-design.md) for the full scope and the [progress tracker](docs/jeff-progress.md) for implementation evidence.

## Local first, human supervised

Deterministic tools answer factual questions. Pi handles coding tasks; local models handle Jeff's reasoning. Local speech is the default, with cloud reasoning and speech fallback explicitly opt-in. Local-AI mode blocks cloud AI; fully offline mode also blocks remote integrations.

The microphone is off except during deliberate hold-to-talk capture. Raw audio is not retained, and voice transcripts stay in the active conversation. Commands require explicit, single-use authorization bound to their exact action and target. Models propose actions; they cannot approve them. Destructive production actions remain denied.

These are design requirements to implement and verify, not protections already provided by this repository.

## Architecture

The planned implementation is one Python package with a local asyncio daemon, SQLite persistence, authenticated Unix-domain socket IPC, and shared application contracts for every client.

| Layer | Responsibility |
| --- | --- |
| Domain | Pure rules, identities, events, and state transitions; standard library only |
| Application | Use cases, policy decisions, orchestration, and adapter ports |
| Infrastructure / integrations | Persistence, IPC, execution, and external tool/provider adapters |
| Interfaces | CLI, desktop, voice interaction, and MCP server clients of the application API |
| Bootstrap | Dependency wiring and lifecycle composition |

Inner layers do not import outer layers. Clients do not execute commands or access storage directly. The current foundation uses Python 3.12+, Typer, Pydantic, PyYAML, pytest, Ruff, and mypy; direct versions are pinned. The internal macOS control/storage foundation uses stdlib asyncio/SQLite; PySide6 and broader platform validation remain planned.

## Getting started

After cloning or opening this repository, run the following from the **Jeff source root** in a macOS/Linux bash or zsh terminal. You need Python **3.12+** and a working system Git (`git --version`; on macOS, `/usr/bin/git --version` avoids an incompatible non-system Git). Current CLI validation is limited to macOS with Python 3.12.11; Linux/fresh-machine verification is still pending.

### 1. Create and activate the virtual environment

```bash
python3 --version  # Must be 3.12 or newer; use python3.12 below if needed.
python3 -m venv .venv
source .venv/bin/activate
```

Skip creation if you already have the intended `.venv`; just activate it. Activation makes `python`, `pip`, and the installed `jeff` command use this environment. It applies only to the current terminal.

### 2. Download dependencies and install Jeff

For CLI use:

```bash
python -m pip install -e .
```

This downloads the runtime requirements from `pyproject.toml` and installs Jeff in editable mode, so source edits are reflected without reinstalling. **`requirements.txt` alone is not enough**: it contains only the compatibility PyYAML pin, not the complete CLI installation.

For development, including pytest, Ruff, and mypy, use this instead:

```bash
python -m pip install -r requirements-dev.lock -e '.[dev]'
```

The lock snapshot is from the tested macOS/Python environment, not a verified cross-platform lock. To resolve development dependencies without that snapshot, use `python -m pip install -e '.[dev]'`; that alternative has not been validated across platforms.

### 3. Run the CLI

```bash
jeff --help
python -m jeff --help
```

Both help entry points exit successfully without project I/O or runtime-location selection. No arguments displays usage with exit code 2; unimplemented commands such as `run` are rejected. The public daemon currently runs on macOS; Linux execution remains deferred. There is no desktop app yet. Internal protocol v4 and DB schema 3 are incompatible with older control clients/DB readers; stop an older instance with its matching older environment before upgrading. Do not use internal entry points against normal user state as a smoke test.

To use an existing project, **activate Jeff's venv before changing directories**, then replace the example path below with your target Git repository:

```bash
jeff start --json  # Explicit; works outside Git and waits for authenticated readiness.
cd /path/to/your/project
jeff init --json
jeff config --json
jeff status --json
jeff ask "what branch am I on?"
jeff ask "what changed?" --json
```

`init` writes private project registration and adds local-state ignore rules in that target repository. Config/status do not mutate application state; questions do not write project files but persist minimal request metadata. While stopped, init/config/ask return `daemon_not_running` without project writes or fallback; help and registration-aware status remain usable. Stop explicitly with `jeff stop --json` (also outside Git). Daemon shutdown drains actual work and confirms native exit; exiting the CLI or deactivating the venv does not stop it. To try an isolated demo instead, follow the next section.

### Resume later or leave the environment

In a new terminal, return to the Jeff source root and run `source .venv/bin/activate` again; you do not need to recreate the venv or reinstall unchanged requirements. Then change to your registered project and run `jeff` commands.

Without activation, run `.venv/bin/jeff --help` or `.venv/bin/python -m jeff --help` from the Jeff source root. From another directory, use the absolute path to Jeff's `.venv/bin/jeff`.

When finished:

```bash
deactivate
```

Deactivation only restores your terminal's previous environment; it does not delete the venv or project registration.

### Generate the demo repositories

From the Jeff source root after installation:

```bash
.venv/bin/python scripts/create_demo_projects.py
source .venv/bin/activate
jeff start --json
cd examples/sandbox-project
jeff init --json
jeff config --json
jeff status --json
jeff ask "what branch am I on?"
jeff ask "what changed?" --json
```

Generation creates independent ignored `sandbox-project` and `second-project` Git repositories on `main`, each with a deterministic baseline commit, a recognition receipt, and untracked `demo-notes.txt`. It does not initialize Jeff, start services, or touch the parent index/history. Repeated generation preserves all existing work/settings; unrelated or unsafe targets are refused. There is no automatic reset/delete/repair. Use a new `--output-dir` location for a fresh copy instead of discarding work.

The sandbox contains a loopback-first HTTP service (`python service.py --port 0`), a **deliberately failing** pytest fixture (**1 pass, 1 fail, exit 1**), and a Compose definition. No new Python dependencies, secrets, image pulls, or running containers are required to generate it. Loopback service and expected-failure checks passed locally; Compose syntax passed, but Docker lifecycle is not verified. See [examples](examples/README.md) and [script safety/commands](scripts/README.md) for details and explicit single-file restoration guidance.

### Register and inspect a project

Activate the venv from the Jeff source root with `source .venv/bin/activate`, then change to the Git repository you intend to register. Run:

```bash
jeff init --json
jeff config --json
jeff status --json
```

Init creates a stable project ID and canonical root in private `jeff.local.yaml`, and appends `/jeff.local.yaml` and `/.jeff/` ignore rules without discarding existing rules. Running from a subdirectory targets the same repository. Repeated init preserves identity, configuration contents, and unrelated settings.

Config exposes **identity fields only**, never arbitrary settings or branch information. Status preserves these identity fields and adds explicit `runtime` control/storage/application readiness; it validates local registration before selecting runtime locations and never starts the daemon or indexes a project. Unregistered repositories return an actionable error; inspection never initializes them implicitly.

Success exits 0 with `{ok: true, data, error: null}`. Init/config data includes `project_id`, `root`, `config_path`, `registered`, and `created`; status additionally includes `runtime`. Start/stop return their own typed control result. Transport fields do not contaminate init/config/ask bodies; duplicate or uncertain requests emit only `Request: <uuid>` on stderr, including with `--json`. This is metadata correlation, not a public receipt-browsing/history command. A missing receipt does not authorize retry. Recognized command failures exit 1 with `{ok: false, data: null, error: {code, message}}`. Without `--json`, errors go to stderr. CLI syntax/unknown-command errors use Typer's usage output and exit 2.

Initialization rejects malformed/unsupported configuration, unsafe links/special files, tracked private configuration, and mismatched stored roots. Moved-project rebind is not implemented; Jeff will not silently replace the ID. Config creation is atomic and refuses overwrite, but ignore/config changes are **not a cross-file transaction**: a failed config write can leave added ignore rules, which the error reports. Static checks and these safe storage operations are not an OS execution sandbox.

### Ask a read-only Git question

With `jeff start` completed, inside an initialized target repository:

```bash
jeff ask "what branch am I on?"
jeff ask "what changed?" --json
```

Both questions accept `--json`, case/whitespace normalization, and an optional final question mark. Matching is deterministic with no model calls. With a running daemon, unsupported questions return `unsupported_intent` without echoing the input or invoking Git. Unregistered projects return an error; questions never initialize them.

Branch answers report the observed name/commit, detached HEAD, or an unborn branch. Change answers report visible staged/unstaged/untracked, renamed, and conflicted metadata—never file contents or raw diffs. Results identify the registered project/root and UTC observation time. Queries do not stage, commit, fetch, or update the index.

**Filtered scope:** protected paths are excluded before Git status and checked again before rendering. This includes environment-family paths (even `.env.example`), private Jeff state, credential/secret names/directories, and common key-file patterns. `.env.example` remains Git-trackable; this metadata policy does not change ignore rules. Submodule contents are excluded. Changes return `complete: false`, `visible_clean`, and explicit exclusion flags; “no visible changes” is not a claim of a fully clean repository. `filtered_records` counts only additional parsed records withheld, not all paths excluded before the query. Path-name rules are conservative heuristics, not content-based secret detection.

Filenames and branch names use reversible JSON string-body escaping (`metadata_encoding: json_escaped`), including controls, backslashes, non-ASCII, and invalid UTF-8 filename bytes. Canonical roots remain normal JSON strings; terminal rendering escapes them too. Jeff does not open source files; Git internally computes metadata for included paths.

Each Git invocation has a five-second timeout and a 1 MiB combined stdout/stderr cap; changes are capped at 10,000 records. Limit/time/failure errors return no partial success. Git transports/lazy fetching and hooks/fsmonitor are disabled, and no credentials are forwarded. Repositories with clean/process filter configuration return `git_filters_unsupported` for change questions: executing those commands is unsafe, while disabling them could misreport changes. Branch queries disable the discovered filters. This is not a general OS sandbox or a fully atomic repository snapshot.

Historical M01 validation on macOS 27.0.1 arm64 with CPython 3.12.11, including a non-editable wheel in new build/application venvs: 126 tests, lint/format/type/dependency checks, and the installed CLI scenario passed. This is not a fresh OS or published release test. Linux and other Python versions remain unverified.

The `docs/` directory is kept locally and excluded from Git, so documentation links below are available only in a checkout with those local files; they will not resolve in a fresh clone. Start with:

1. [Documentation index](docs/README.md) — where to find each source of truth.
2. [Product design](docs/jeff-design.md) — required behavior and boundaries.
3. [Implementation plan](docs/jeff-implementation-plan.md) — M01–M17 and acceptance scenarios.
4. [Progress tracker](docs/jeff-progress.md) — actual status, evidence, and next step.

Within **M01: install Jeff and ask a real project question**, help, `init`, `config`, registration-only `status`, and the two `ask` questions now work locally. Demo generation and local fixture checks are also implemented. Bounded technical probes and clean-environment CLI checks have macOS evidence. Technical choices and an architecture map are recorded locally. The supplied manual test-branch transcript passed the checklist below; formal milestone acceptance and the M02 handoff still require human approval. CI execution is a separate choice, not a claimed hosted success. Linux validation is explicitly deferred, not removed from product scope. Other implementation-plan commands remain future contracts.

## Manual M01 acceptance on your test repository

Historical M01 checklist/transcript below; it is not a new required human check. Current M02 requires explicit `jeff start` before init/config/ask and adds `runtime` to status. Do not use normal/user installations as test fixtures or delete registration to repeat a check.

Install Jeff and activate its venv using Getting started above, then change to your **already prepared test repository/branch**. Jeff registration belongs to the repository root, not a branch; do not copy `jeff.local.yaml` between clones. Init writes private registration and ignore rules, so use a disposable/test checkout rather than an unrelated working project.

### 1. Help and registration

`init` is a Jeff subcommand: run `jeff init`, not bare `init` in the shell.

```bash
jeff --help
python -m jeff --help
jeff init --json
jeff config --json
jeff status --json
jeff init --json
```

Expect exit 0, valid JSON, the correct canonical root, and the **same `project_id`** throughout. The second init reports `created: false` and preserves existing settings. If the repository was already registered, the first init also reports false; do not delete its configuration to force a fresh result. `status` reports registration, not daemon/process health.

### 2. Real branch and change answers

On macOS, use system Git for the reference branch (some existing non-system Git binaries may be incompatible with the machine):

```bash
/usr/bin/git branch --show-current
jeff ask "what branch am I on?" --json
jeff ask "what changed?" --json
```

Expect the actual branch, or an explicit detached/unborn state where applicable. Change results include visible metadata, `complete: false` and exclusion flags—not file contents, raw diffs, or a claim that the entire repo is clean.

Create one unique synthetic file without overwriting existing work:

```bash
probe_file="$(mktemp ./jeff-m01-check.XXXXXX)" &&
  printf 'Synthetic M01 check\n' > "$probe_file"
jeff ask "what changed?" --json
```

Expect that filename as untracked, unless your test repository deliberately ignores it. You can also make a known edit/stage it using your own usual tools and compare the reported state. Jeff's questions do not stage, commit, fetch or update the index. Optional cleanup removes **only** the synthetic file you just created:

```bash
rm -- "$probe_file"
```

### 3. Safe failures

These commands intentionally return exit **1**; run interactively or handle that expected status in scripts:

```bash
jeff ask "unsupported M01 test" --json
outside="$(mktemp -d /private/tmp/jeff-m01-manual.XXXXXX)"
(cd "$outside" && jeff status --json)
```

Expect `unsupported_intent` for the first and `not_a_repository` for the second. The outside-repository check is read-only and must use a genuinely outside-Git directory, not a folder nested under Jeff or your test repository. Optional `rmdir -- "$outside"` removes only that empty temporary directory. Before initializing an unregistered test repository, `jeff status --json` should instead report `project_not_initialized`; skip that case if already registered, rather than deleting state.

**Report back:** whether help, stable identity/repeat init, actual branch, visible file changes and both safe failures behaved as expected. These confirm the manual M01 CLI acceptance. Daemon, desktop and microphone/model integration are later milestones; the feasibility probes are not shipped voice features. No credentials or `.env` setup are needed.

## Development checks

From the Jeff source root. Any custom `TMPDIR` or pytest `--basetemp` must be outside Git repositories: ignored scratch inside a checkout still inherits its Git ancestors.

```bash
.venv/bin/python -m pytest tests/unit tests/architecture tests/integration tests/acceptance -q
.venv/bin/python -m ruff check src tests scripts
.venv/bin/python -m ruff format --check src tests scripts
.venv/bin/python -m mypy src tests scripts
.venv/bin/python -m pip check
```

`pyproject.toml` owns runtime/dev requirements and tool settings. Direct dependencies are pinned to the locally tested versions. PyYAML 6.0.3 parses bounded private project configuration through the filesystem adapter. `requirements.txt` retains the same pin for compatibility with existing local tooling. `requirements-dev.lock` snapshots resolved dependencies for this macOS/Python environment; it is not hash-verified, does not lock isolated build tools, and has not been validated cross-platform.

Pydantic validates the public application result contracts. Static AST architecture checks enforce tier imports and selected client restrictions, including negative test cases. They do not sandbox execution or detect dynamic imports.

## Local checks and CI options

The Development checks above run natively on your Mac; GitHub-managed compute is not required to execute them. A local passing run is not a hosted CI result. There is no new single-command runner, Docker validation image or self-hosted runner setup shipped in M01.

A macOS GitHub Actions draft was prepared and locally validated in the original development checkout. It is not published by this documentation update and need not exist in a fresh clone or the testing checkout. It covers installation, pytest, Ruff, mypy and dependency consistency, not models, audio, desktop or deployment. `.github/` normally stays trackable; private runtime data does not belong there.

Execution choices remain explicit:

- **Native Mac:** use the existing development commands; this is the current recorded automated evidence.
- **Docker on Mac:** a Linux container can provide additional Linux CLI checks, not native macOS GUI/shortcut/permission validation. No image pull/container run is authorized merely by discussing it.
- **Self-hosted runner:** GitHub dispatches jobs to your machine; registration, untrusted-PR security and current pricing need separate review. None is registered here.
- **GitHub-hosted runner:** uses GitHub-managed compute/quota; no job is enabled or run by keeping a draft locally. A testing branch isolates code changes, not runner type or billing.

Use `testing/jeff-test` for testing changes without advancing `main`. Creating a branch alone does not publish or enable a workflow; trigger configuration and explicit publication/dispatch matter. Linux validation remains deferred, not removed from product scope.

## Configuration and secrets

`jeff.local.yaml` stores `schema_version: 1` and a `project` mapping with generated UUID `id` and canonical absolute `root`. Init creates the file with mode 0600 and never rewrites existing valid settings. `.jeff/` is reserved and ignored for future runtime files; this slice does not create it. Configuration reads are limited to 64 KiB; unsupported YAML tags, anchors/aliases, excessive nesting, and invalid identities are rejected. Credentials must use supported credential-store or explicitly selected environment-variable references, not committed values.

`.env` and `.env.*` are ignored, **except `.env.example`**, which is an empty, committed starting point for a future placeholder-only configuration template. No environment-variable contract exists yet; environment loading is not implemented. Never put real credentials in an example file.

Local documentation (`docs/`), agent-toolkit files and metadata, and virtual environments are ignored. Ignore rules do not remove files or submodules already tracked by Git.

## Scope boundaries

Voice, desktop, local reasoning, multi-project supervision, and every required MCP transport are part of the complete product, not optional future implementation work. Individual features may be configurable once working.

IDE-specific plugins, wake-word/toggle listening, vector/semantic history search, and remote notification delivery services are outside the current scope. Registered SSH administration is in scope, subject to exact per-command approval and host/privilege boundaries.

## Working on Jeff

Implement small, runnable milestones in plan order. Define acceptance scenarios before coding, update component READMEs with behavior, and record only checks actually run. Required real integrations and macOS/Linux verification cannot be replaced by mocked success. Major implementation stages require human review.

Broader context lives in [PROJECT_CONTEXT.md](docs/context/PROJECT_CONTEXT.md); canonical vocabulary lives in [DOMAIN_MODEL.md](docs/context/DOMAIN_MODEL.md). No distribution route or project license has been established yet.