# Unit tests

**Status:** M01 CLI/domain/application tests and M02 runtime/control contracts, pure paths, strict v3 codec and private settings/storage contracts implemented.

- [test_demo_safety.py](test_demo_safety.py): generator preflight/refusal, symlinks/traversal/markers, no writes or Git on preserved repos, missing Git, partial-state refusal, and prevention of parent-repository Git fallback.
- [test_questions.py](test_questions.py): deterministic routing, unsupported input without Git access/echo, registered targeting/time, protected origin/destination filtering, and safe terminal metadata.
- [test_projects.py](test_projects.py): absolute roots, stable repeat initialization, inspection without implicit registration, and mismatched roots from a controlled port.

- [test_runtime_contracts.py](test_runtime_contracts.py): immutable identity/state invariants, strict safe result construction/serialization, fixed inspection roles and protocol/limit declarations; no process ownership claim.
- [test_runtime_paths.py](test_runtime_paths.py): injected Mac defaults, independent overrides, no cwd/environment fallback, safe invalid-input failures, known system aliases, byte-length bounds and honest unverified-platform refusal. Resolver tests perform no installation I/O.

- [test_runtime_control_protocol.py](test_runtime_control_protocol.py): strict framed control requests, duplicate keys/non-finite/UTF-8/version/type/nesting rejection, length limits before body receive, truncated frames and bounded output. Socketpair cases use owned descriptors only.

- [test_runtime_storage_contracts.py](test_runtime_storage_contracts.py): strict immutable settings/readiness, no invented schema/application success and hidden validation inputs.
- [test_runtime_settings.py](test_runtime_settings.py): bounded strict YAML, tags/anchors/aliases/duplicates/unknowns, defaults, byte preservation and no-follow/type/link/mode/size refusal under actual fixture ownership.

- [test_project_rpc_contracts.py](test_project_rpc_contracts.py): v3 instance-bound finite messages, strict budgets/path/auth identity and receipt payload refusal.
- [test_cli.py](test_cli.py): lazy application client/control factories, lifecycle delegation, no client action fallback, JSON/stderr correlation separation; native behavior is proved separately in acceptance.
- [test_activity_contracts.py](test_activity_contracts.py): strict finite observation/trace/replay values and evidence request scope/shape, no raw payloads.
- [test_project_dispatch.py](test_project_dispatch.py): init/config/both-question dispatch, duplicate/scoped inspection, read-only diagnostics, missing-indexed-YAML refusal and real YAML/SQLite partial faults through the application interface; owned Git/YAML/SQLite and explicit synthetic privacy sink checks.

**Dependencies:** pytest, Typer test runner, Pydantic result contracts, domain/application/interfaces, pure path selection and control codec. Controlled discovery/storage doubles exercise the same public interface as adapters without invoking Git or storage. Real filesystem/process checks belong under integration/acceptance.

From the repository root after [installation](../../README.md):

```bash
.venv/bin/python -m pytest tests/unit -q
```

**Owning milestones:** M01 (formal acceptance separate) and M02 foundation only. Real control lifecycle is tested separately under integration/acceptance. Unit contracts/codec alone prove no runtime ownership/readiness. Approvals/jobs/providers remain unimplemented. Read-only Git-question rules remain covered. Fixtures contain only synthetic data, no credentials or environment files. Extend through real public contracts rather than test-only execution paths. See [test map](../README.md).
