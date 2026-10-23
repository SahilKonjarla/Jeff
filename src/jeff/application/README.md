# Application — tier 1

**Status:** M01 project/Git use cases and M02 runtime/control contracts implemented. Actual internal control lifecycle exists in adapters; public CLI routing, arbitrary execution and approvals remain later work. M02 slice 3 adds separate storage readiness and validated registry use cases.

**Ownership:** `projects.py` contains `ProjectService`, discovery/storage protocols, and Pydantic result contracts. Filesystem operations, YAML, Git subprocesses, and presentation belong elsewhere.

**Dependencies:** domain, application-owned contracts, standard library, and Pydantic. No concrete adapters, SDKs, subprocesses, clients, or bootstrap imports.

## Public interface

`ProjectService(discovery: ProjectDiscovery, store: ProjectStore)` exposes:

`execute(operation: Literal["init", "config", "status"], directory: Path) -> Success | Failure`

- `ProjectDiscovery.find_root(directory) -> Path` returns a validated canonical non-bare Git root.
- `ProjectStore.initialize(project) -> (Project, created)` preserves existing identity/settings and ensures private ignore rules.
- `ProjectStore.read(root) -> Project` is read-only and rejects missing/unsafe/stale state.

**Flow:** bind root through discovery → initialize or inspect through storage → reject a mismatched identity/root → return typed public fields. Initialization generates an ID only for new registration. Config/status return an actionable error for unregistered projects; they do not register them implicitly.

JSON results contain `ok`, `data`, and `error`. Success data includes `project_id`, canonical `root`, `config_path`, `registered: true`, and `created`. Failures contain only a stable error code and safe message. Unknown settings and raw adapter failures never enter the public result. Models forbid extra result fields; no runtime events are emitted yet.

## Repository question interface

`questions.py` exposes `QuestionService(projects, git: GitRead, clock=None).execute(question: str, directory: Path) -> QuestionSuccess | Failure`. `GitRead` declares `read_branch(root) -> BranchState` and `read_changes(root) -> tuple[FileChange, ...]`.

Only case/whitespace-normalized “what branch am I on?” and “what changed?” are accepted (final `?` optional). Unsupported input fails before project/Git access and is not echoed. Recognized questions bind the existing validated identity through `ProjectService`; they cannot initialize a project or execute commands.

Success data is a typed `BranchAnswer` or `ChangesAnswer` with project ID, canonical root, UTC observation time, and `metadata_encoding: json_escaped` for branch/file metadata. Branch results distinguish unborn/detached/named states. Changes include index/worktree codes, staged/unstaged/untracked/conflicted flags, and optional rename origin. Protected origin or destination removes the entire record before rendering.

Changes intentionally report `complete: false`, `visible_clean`, protected-path/submodule exclusion flags, and `filtered_records` for additional post-parse omissions. Pre-query excluded paths cannot be counted; no full-clean or total-hidden-change claim is made. Limit/time/adapter failures return `Failure`, never partial success. Filesystem/command behavior and output limits remain adapter responsibilities. Filter-equipped repositories fail closed for change questions; no model or general permission system is involved.

**Example:** `jeff ask "what changed?" --json` → normalized intent → registered project → Git port → scoped/escaped metadata → typed result. See [question unit tests](../../../tests/unit/test_questions.py) and [installed checks](../../../tests/acceptance/test_installed_questions.py).

**Configuration/security:** adapters own schema/file formats. The application requires stable project targeting and never silently rebinds moved projects. This slice exposes narrow registration behavior, not a general action approval/execution system.

## Runtime contracts — M02 slice 1

`runtime.py` owns frozen, extra-forbidden `RuntimeIdentityData`, `RuntimeStatusData`, `RuntimeLayout`, `PathObservation` and `RuntimePathInspection`. Status validates identity/state coherence and uses allowlisted issue codes for unknown/unhealthy observations. Inspection contains exactly seven unique roles: two directories plus database/settings/credential/socket/lock. Missing/present have no issue; unsafe/unknown require one. No credential body or arbitrary reason field exists. Human-readable validation hides input values; callers must still never forward raw validation exceptions or `.errors()` data as public results.

`RuntimePathInputs` carries trusted explicit platform/home/temp/UID values and independent optional `data_dir`/`runtime_dir` strings. The pure contracts do not read the environment. The internal slice-2 bootstrap now selects these fields from `JEFF_DATA_DIR`/`JEFF_RUNTIME_DIR`; public CLI behavior remains unchanged. `RuntimeLayout` derives fixed filenames from its roots. The expected UID is trusted host input, not client-selected authority.

`PROTOCOL_REVISION = 2` and `DEFAULT_RUNTIME_LIMITS` declare the reviewed framing/admission/deadline/history budgets; they implement no codec, deadline, readiness or authentication. Mac preflight constants reserve terminators: socket 103 bytes, pathname 1023 bytes, component 255 bytes. Linux selection/verification is deferred. See [contract tests](../../../tests/unit/test_runtime_contracts.py) and [path tests](../../../tests/unit/test_runtime_paths.py).

## Authenticated control contracts — M02 slice 2

`RuntimeControl.start/status/stop() -> ControlSuccess | Failure` binds one installation. `ControlObservation` separates process/control readiness from `application_ready: false` and a strict `storage` snapshot; schema facts appear only when storage is ready. Stopped status reports `not_checked`. Unknown outcomes are fixed-code failures, not guessed stopped results. `ControlFault`/`control_failure` map allowlisted descriptions without raw exceptions or frames.

`AuthRequest` masks its credential in repr and validates fixed-format input; it is internal transport data, never a public result. `AuthAccepted`, `ControlRequest` and `ControlResponse` are strict typed control messages, with protocol/request metadata separate from M01 JSON bodies. Only hello/status/targeted stop exist; no project dispatcher or durable replay/history claim. Adapters must enforce actual framing/auth/deadlines; model construction alone confers no authority. See [codec tests](../../../tests/unit/test_runtime_control_protocol.py).

## Private storage contracts — M02 slice 3

`storage.py` owns immutable schema-1 settings (strict positive retention days, default 30), cached `StorageReadiness` and fixed privacy-safe storage errors. Protocol revision, settings schema and DB schema are independent. `StartupResult` in `runtime.py` is bounded child diagnostic data, never authenticated readiness authority. V1/V2 do not negotiate or silently downgrade; an older peer closing the handshake remains unconfirmed, not a guessed bad credential.

`ProjectRegistry.index(validated_project) -> (Project, created)` and `lookup(validated_project) -> Project | None` expose immutable identity/root entries only. `RegistryService(projects, registry).execute("index" | "lookup", directory)` first validates current Git/YAML registration with `ProjectService`. Lookup never indexes, same-target index is a no-op, and conflicts/moves cannot rebind. No YAML settings, credentials or questions cross the registry port. Synchronous calls belong outside the control loop; no project RPC exists yet. YAML/DB updates are not a cross-file transaction.

**Owning milestones:** M01 and M02 control/storage foundation. [Unit tests](../../../tests/unit/test_projects.py) use controlled ports; real storage/discovery and installed checks are under [integration](../../../tests/integration/README.md) and [acceptance](../../../tests/acceptance/README.md).

Run `.venv/bin/python -m pytest tests/unit tests/integration tests/acceptance -q` after [installation](../../../README.md). Extend with typed use cases and ports when the next real behavior needs them. See [package map](../README.md).
