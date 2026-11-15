# Unit tests

Current Step3 source uses wire11/DB8/settings3(default2), with opt-in bootstrap activation covered by four integration groups (217 selected passes); default remains disabled and no installed/live Docker proof follows. New fresh request/reply group rejects legacy kind/current proof/claim/control extras, malformed conditional fields and mismatched operation/admission/UUID payloads; previous10 and future12 refuse. New CLI group verifies help/syntax factory laziness, separate `execution` namespace, literal argv/repeated files, required reviewed digest and typed unavailable output. Legacy run remains review-only.213 finite selected source cases passed with scoped review/static checks; production backend and installed/live proofs remain gated.

Settings3 integration adds grouped parser/owned-file cases in `test_runtime_settings.py` for exact whole-profile binding, disabled absence/null, strict version dispatch, legacy backend refusal, same old serialization/default bytes and existing file preservation. Existing doctor/storage-contract groups now cover schema3/scalar-only health, future4 refusal and unchanged legacy/default models. Real ready/disabled/legacy/failed-database cache + stopped-doctor behavior is covered by one integration group.201 selected source cases passed; no schema3 installed/Docker/public activation proof.

Step3's added `test_execution_authority_contracts.py` group covers standalone private backend declarations: required explicit fields, cold Path operations, roundtrip/freezing,1–16/aggregate32KiB limits, duplicate project/profile and path-overlap rejection, malformed identity/unknown credentials with hidden canary errors. Missing type was red before implementation; prior authority case is unchanged. This does not exercise settings3 storage, actual filesystem verification, daemon activation or Docker.199 selected source cases passed; existing settings/manifest defaults remain unchanged.

**Status:** M01 CLI/domain/application tests and M02 runtime/control contracts, pure paths, strict wire6 codec and private settings/storage contracts implemented.

- [test_demo_safety.py](test_demo_safety.py): generator preflight/refusal, symlinks/traversal/markers, no writes or Git on preserved repos, missing Git, partial-state refusal, and prevention of parent-repository Git fallback.
- [test_questions.py](test_questions.py): deterministic routing, unsupported input without Git access/echo, registered targeting/time, protected origin/destination filtering, and safe terminal metadata.
- [test_projects.py](test_projects.py): absolute roots, stable repeat initialization, inspection without implicit registration, and mismatched roots from a controlled port.

- [test_activity_stream_contracts.py](test_activity_stream_contracts.py): strict distinct activity cursors, finite subscription/frame discriminators/no external drain override, and safe exhausted-interval page positions. No native ownership claim.
- [test_history_contracts.py](test_history_contracts.py): strict bounded canonical opaque cursors (duplicates/nonfinite/types/positions), finite diagnostic envelopes and unchanged event-replay64 limit; no I/O.
- [test_runtime_contracts.py](test_runtime_contracts.py): immutable identity/state invariants, strict safe result construction/serialization, fixed inspection roles and protocol/limit declarations; no process ownership claim.
- [test_runtime_paths.py](test_runtime_paths.py): injected Mac defaults, independent overrides, no cwd/environment fallback, safe invalid-input failures, known system aliases, byte-length bounds and honest unverified-platform refusal. Resolver tests perform no installation I/O.

- [test_runtime_control_protocol.py](test_runtime_control_protocol.py): strict framed control requests, duplicate keys/non-finite/UTF-8/version/type/nesting rejection, length limits before body receive, truncated frames and bounded output. Socketpair cases use owned descriptors only.

- [test_runtime_storage_contracts.py](test_runtime_storage_contracts.py): strict immutable settings/readiness, no invented schema/application success and hidden validation inputs.
- [test_runtime_settings.py](test_runtime_settings.py): bounded strict YAML, tags/anchors/aliases/duplicates/unknowns, defaults, byte preservation and no-follow/type/link/mode/size refusal under actual fixture ownership.

- [test_project_rpc_contracts.py](test_project_rpc_contracts.py): v3 instance-bound finite messages, strict budgets/path/auth identity and receipt payload refusal.
- [test_cli.py](test_cli.py): lazy application client/control factories, lifecycle delegation, no client action fallback, JSON/stderr correlation separation; native behavior is proved separately in acceptance.
- [test_activity_contracts.py](test_activity_contracts.py): strict finite observation/trace/replay values and evidence request scope/shape, no raw payloads.
The seven real Git/YAML/SQLite dispatcher cases moved unchanged to [integration/test_project_dispatch.py](../integration/test_project_dispatch.py); they are not unit tests. Global help/syntax checks share the three-forbidden-factory seam; doctor-specific help/syntax remains separate. Revision decoding keeps representative old/future integers and distinct malformed type/encoding/framing cases.

**Dependencies:** pytest, Typer test runner, Pydantic result contracts, domain/application/interfaces, pure path selection and control codec. Controlled discovery/storage doubles exercise the same public interface as adapters without invoking Git or storage. Real filesystem/process checks belong under integration/acceptance.

From the repository root after [installation](../../README.md):

```bash
.venv/bin/python -m pytest tests/unit -q
```

- [test_execution_output.py](test_execution_output.py): six grouped memory-only collector cases covering every split of representative credential indicators, intersecting stream frames/EOF, UTF-8/control/bidi escapes, exact line/deferred/shared retention bounds, drain-after-drop and descriptive snapshot refusal semantics.
- [test_docker_owned_lifecycle.py](test_docker_owned_lifecycle.py): one mount-consistency group characterizes the project-only listing with mandatory exact HostConfig.Tmpfs (not a raw replay), retains explicit-scratch coverage, rejects20 unsafe create variants and three pre-run mutations without start, and follows a red-at-old-guard→green correction. Two additional grouped diagnostic-only cases inject synthetic configuration/initial-state refusals through the public controller, verify fixed check IDs/boolean summaries, reject raw/unallowlisted metadata, and preserve exact-owned cleanup without attach. They expose a missing-observability gap, not the original live Docker response. now also tests the actual collector/finalizer with exact live handle/sink binding, internally captured outcome, normal zero exit, post-EOF uncertainty, timeout-abort and one-shot completion/lost-ACK fencing; fake SQL result port here is distinct from integration SQL proof. grouped internal-controller cases for fresh one-attempt admission, copied-handle refusal, drift, ambiguity, timeout and exact owned cleanup; fake backend/journal/input/output ports only.
- [test_docker_lifecycle_transport.py](test_docker_lifecycle_transport.py): fixed CLI argv/environment, bounded chunks, captured-CLI failure cleanup and post-control guard uncertainty; real pipes/local UDS with fake Popen, no Docker/native child.

**Owning milestones:** M01 (formal acceptance separate), M02 foundation and M03 source slices. Real control lifecycle is tested separately under integration/acceptance. Unit contracts/codec alone prove no runtime ownership/readiness. Review-only approvals/jobs are delivered; executable Phase3 composition and providers remain unimplemented. Read-only Git-question rules remain covered. Fixtures contain only synthetic data, no credentials or environment files. Extend through real public contracts rather than test-only execution paths. See [test map](../README.md).
