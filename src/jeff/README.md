# Jeff package

**Status:** M01 public CLI plus M02 internal authenticated control lifecycle implemented. M02 slice 3 adds private settings, SQLite schema/registry persistence and separate storage readiness. Checkpoints A/B add internal v3 init/config/branch/changes RPC, schema-2 receipts and partial-write/identity guards; checkpoint C adds public start/stop, runtime-aware status, daemon-dependent project routing and full dispatcher readiness. Slice-5 A adds internal schema-3 durable traces/events with scoped read/replay and actual Git observations. Public history/debug, live subscriptions, expiry and doctor/recovery remain pending.

**Ownership:** shared local developer-assistant runtime and client contracts. External Pi extension code and packaging assets belong outside this package when their milestones create them.

## Dependency map

| Directory | Responsibility | Permitted internal dependencies |
| --- | --- | --- |
| [domain](domain/README.md) | Identities, values, state transitions, events | Standard library only |
| [application](application/README.md) | Use cases, policy, orchestration, ports | Domain and its own contracts |
| [infrastructure](infrastructure/README.md) | Storage, IPC, execution, credentials | Application ports and domain |
| [integrations](integrations/README.md) | External tools, models, audio | Application ports and domain |
| [interfaces](interfaces/README.md) | CLI, desktop, voice, MCP server | Application API and domain contracts |
| [bootstrap](bootstrap/README.md) | Adapter wiring and lifecycle | All tiers; imported only by entry points |

Inner tiers never import outer tiers. Sibling integrations communicate through application contracts/events rather than each other's implementations. Concrete adapter construction belongs in bootstrap.

**Implemented example:** `jeff ask "what branch am I on?"` routes deterministically, validates the registered project through existing use cases, calls a narrow Git-read adapter, and renders timestamped branch metadata. It uses no model or arbitrary execution. General capability/approval enforcement remains future M03 work.

**Owning milestones:** M01 starts the package and dependency checks; M02 adds the shared daemon; later milestones extend the same request path.

## Implemented entry points

- Installed `jeff` → `jeff.bootstrap.main:main` → injected `jeff.interfaces.cli:create_app`.
- `python -m jeff` → `__main__.py` → the same bootstrap function.
- `--help` exits 0 without project I/O; no arguments and unknown commands exit 2.
- `init`, `config`, and `status` render identity; `ask` supports branch/change questions, optionally as typed `--json` envelopes. Recognized application failures exit 1.

`domain/projects.py` defines identity/errors; `application/projects.py` owns use cases/ports/results; `integrations/git.py` discovers canonical Git roots; `infrastructure/project_files.py` stores private YAML and ignore rules. Bootstrap alone wires concrete adapters into the CLI. `domain/repository.py` owns metadata values and disclosure policy; `application/questions.py` owns deterministic routing/results and the Git-read port. Separately, `application/runtime.py` owns runtime/control contracts; infrastructure runtime modules implement private state, lifetime locking, authenticated control IPC and an installed child process. `bootstrap/runtime.py` alone composes the internal control entry. Control-v4 reports actual cached storage/schema/handler readiness; full application readiness is true only with all live handlers/storage/control. `application/storage.py` adds validated registry use cases; `runtime_settings.py`/`runtime_storage.py` implement private settings and one bounded SQLite owner. `application/dispatch.py` and `runtime_dispatch.py` provide project/receipt client seams and bounded workers. Public lifecycle/project routing now uses lazy bootstrap factories; only the server composes Git question execution, with no fallback. Status composes local read-only registration and runtime observations. Jobs, approvals, providers, raw diff/content questions and runtime events remain unimplemented.

Installed entry-point tests require temporary directories genuinely outside Git repositories (including all parent Git ancestors), with initialization/staging/commits/branch switches/merge conflicts confined to disposable fixtures. Query checks verify unchanged index/HEAD and no filter-command execution. Storage/discovery tests cover preservation, unsafe paths, stale roots, parser/error privacy, and detected concurrent changes. Static architecture checks cover positive and forbidden import examples. See [tests](../../tests/README.md) and the [root README](../../README.md) for verified install/check commands. Mac M01 automated and supplied manual checks passed; formal acceptance remains a human decision; M02 slices 1–2 were explicitly approved for implementation, not full milestone acceptance. Speech/model/GUI feasibility exists only in ignored experiments, not imported providers. Local native checks are the recorded route; no Docker/self-hosted/hosted CI execution is claimed, and Linux verification is deferred.
