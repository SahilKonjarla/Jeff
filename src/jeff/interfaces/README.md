# Interfaces — tier 3

**Status:** public CLI start/stop, runtime-aware status and authenticated daemon-dependent init/config/both Git questions are implemented in M02 slice 4 C. Slice5 B adds scoped read-only `history` and `debug last` over authenticated wire5/schema3. Slice5 C adds installed-client subscriptions without changing this CLI or adding follow/watch verbs. Current wire6/schema4/settings1 also has bounded retention and read-only doctor; desktop, voice, MCP, general execution and stale/crash recovery remain unimplemented.

**Ownership:** `cli.py` renders application results. `create_app(client_factory, control_factory, diagnostics_factory=None)` accepts lazy application-owned project/control/diagnostics factories (two-argument callers remain supported); no concrete adapters, Git/storage imports, autostart or local action fallback.

After [installation](../../../README.md):

```bash
jeff --help
jeff start --json
# Inside the intended Git repository:
jeff init --json
jeff config --json
jeff status --json
jeff ask "what branch am I on?"
jeff ask "what changed?" --json
jeff debug last --json
jeff history --limit 50 --json
jeff stop --json
```

Start/stop/doctor work outside Git. `doctor [--json]` renders the seven-check bounded application report with exit0 for healthy/stopped/uninitialized,1 for required fault/uncertainty and2 for syntax. Live output labels cached active retention; stopped output labels configured retention inactive. No lifecycle/repair action, raw paths/settings/keys, direct SQL or Git execution is added to presentation. Missing optional diagnostics composition returns explicit unavailable rather than fabricated health. Help, command help and syntax do not invoke factories or select runtime paths. While stopped, init/config/ask fail with `daemon_not_running` without project/ignore/registry writes. Status locally validates registration, then obtains read-only runtime facts; it works stopped and never implicitly indexes/initializes. Invalid/unregistered projects retain their errors before runtime selection.

Init/config/ask keep their existing result bodies. Status adds `data.runtime` to the identity fields. Lifecycle uses its own typed control result; start waits for full authenticated dispatcher/storage readiness, stop confirms actual native exit and released ownership. Acknowledgement/caller cancellation alone is not completion.

Success exits 0; fixed application failures exit 1; syntax/no arguments/unknown commands exit 2. JSON bodies remain on stdout, human errors on stderr. Duplicate/uncertain project outcomes also emit only `Request: <uuid>` on stderr (including JSON mode). `debug last [--request-id UUID]` returns scoped bounded receipt/trace metadata. `history [--limit N] [--cursor TOKEN]` returns acceptance-ordered summaries (50default/100max). Help/invalid UUID/limit stay lazy; reads never displace last, index, write access history or clean up. No-history debug fails explicitly; empty history succeeds. Cursor scope/epoch/generation mismatch requires explicit resync. Missing receipt never proves safe retry. No answer replay.

Questions remain deterministic, model-free and project-scoped; protected/submodule exclusions, reversible metadata escaping, actual Git limits/filter refusal and no raw content/diff rendering are unchanged. Questions persist only minimal allowlisted receipt metadata, not answers or raw input. Config/status do not record access history or perform cleanup.

**Dependencies:** application/domain and client presentation libraries (Typer/Pydantic); no infrastructure/bootstrap/provider imports. Concrete composition belongs to bootstrap. No `.env` loading, providers/audio, general approvals or events.

**Validation:** [unit CLI](../../../tests/unit/test_cli.py), [public installed workflow](../../../tests/acceptance/test_daemon_cli_workflow.py), [installed questions](../../../tests/acceptance/test_installed_questions.py), [architecture](../../../tests/architecture/README.md). Owned outside-Git fixtures only; Mac execution is not Linux/CI or full milestone acceptance. See [application contracts](../application/README.md), [test map](../../../tests/README.md) and [package map](../README.md).
