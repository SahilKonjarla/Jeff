# Application — tier 1

**Status:** M01 project/Git use cases and M02 runtime/control contracts implemented. Public CLI lifecycle/project routing, scoped history/debug/subscriptions/retention and read-only doctor contracts are implemented. Arbitrary execution and approvals remain later work. Runtime values distinguish control/storage/full readiness and validated registry use cases.

**Ownership:** `projects.py` contains `ProjectService`, discovery/storage protocols, and Pydantic result contracts. Filesystem operations, YAML, Git subprocesses, and presentation belong elsewhere.

**Dependencies:** domain, application-owned contracts, standard library, and Pydantic. No concrete adapters, SDKs, subprocesses, clients, or bootstrap imports.

## Read-only doctor interface

`diagnostics.RuntimeDiagnostics.inspect() -> DoctorResult` exposes seven ordered checks, bounded finite reasons, coherent summary/error data and optional authenticated `DoctorRuntimeData`. The command-specific failure retains its report; generic Failure/control/project envelopes are unchanged. Healthy requires current full readiness including all ten operations; stopped requires validated current settings/schema without invented live identity. Configured inactive and cached active retention are distinct, positive int64 or `above_report_limit`; the result is capped at8KiB. Concrete ownership/credential/SQLite/IPC behavior stays in infrastructure and is composed lazily by bootstrap. Doctor is not recovery or arbitrary integrity inspection.

## Public interface

`ProjectService(discovery: ProjectDiscovery, store: ProjectStore)` exposes:

`execute(operation: Literal["init", "config", "status"], directory: Path) -> Success | Failure`

- `ProjectDiscovery.find_root(directory) -> Path` returns a validated canonical non-bare Git root.
- `ProjectStore.initialize(project) -> (Project, created)` preserves existing identity/settings and ensures private ignore rules.
- `ProjectStore.read(root) -> Project` is read-only and rejects missing/unsafe/stale state.

**Flow:** bind root through discovery → initialize or inspect through storage → reject a mismatched identity/root → return typed public fields. Initialization generates an ID only for new registration. Config/status return an actionable error for unregistered projects; they do not register them implicitly.

JSON results contain `ok`, `data`, and `error`. Success data includes `project_id`, canonical `root`, `config_path`, `registered: true`, and `created`. Failures contain only a stable error code and safe message. Unknown settings and raw adapter failures never enter the public result. Models forbid extra result fields; request activity is emitted through the dispatcher journal, not by direct ProjectService calls.

## Repository question interface

`questions.py` exposes `QuestionService(projects, git: GitRead, clock=None).execute(question: str, directory: Path) -> QuestionSuccess | Failure`. `GitRead` declares `read_branch(root) -> BranchState` and `read_changes(root) -> tuple[FileChange, ...]`.

Only case/whitespace-normalized “what branch am I on?” and “what changed?” are accepted (final `?` optional). Unsupported input fails before project/Git access and is not echoed. Recognized questions bind the existing validated identity through `ProjectService`; they cannot initialize a project or execute commands.

Success data is a typed `BranchAnswer` or `ChangesAnswer` with project ID, canonical root, UTC observation time, and `metadata_encoding: json_escaped` for branch/file metadata. Branch results distinguish unborn/detached/named states. Changes include index/worktree codes, staged/unstaged/untracked/conflicted flags, and optional rename origin. Protected origin or destination removes the entire record before rendering.

Changes intentionally report `complete: false`, `visible_clean`, protected-path/submodule exclusion flags, and `filtered_records` for additional post-parse omissions. Pre-query excluded paths cannot be counted; no full-clean or total-hidden-change claim is made. Limit/time/adapter failures return `Failure`, never partial success. Filesystem/command behavior and output limits remain adapter responsibilities. Filter-equipped repositories fail closed for change questions; no model or general permission system is involved.

**Example:** `jeff ask "what changed?" --json` → normalized intent → registered project → Git port → scoped/escaped metadata → typed result. See [question unit tests](../../../tests/unit/test_questions.py) and [installed checks](../../../tests/acceptance/test_installed_questions.py).

**Configuration/security:** adapters own schema/file formats. The application requires stable project targeting and never silently rebinds moved projects. This slice exposes narrow registration behavior, not a general action approval/execution system.

## Runtime contracts — M02 slice 1

`runtime.py` owns frozen, extra-forbidden `RuntimeIdentityData`, `RuntimeStatusData`, `RuntimeLayout`, `PathObservation` and `RuntimePathInspection`. Status validates identity/state coherence and uses allowlisted issue codes for unknown/unhealthy observations. Inspection contains exactly seven unique roles: two directories plus database/settings/credential/socket/lock. Missing/present have no issue; unsafe/unknown require one. No credential body or arbitrary reason field exists. Human-readable validation hides input values; callers must still never forward raw validation exceptions or `.errors()` data as public results.

`RuntimePathInputs` carries trusted explicit platform/home/temp/UID values and independent optional `data_dir`/`runtime_dir` strings. The pure contracts do not read the environment. The bootstrap selects these fields lazily from `JEFF_DATA_DIR`/`JEFF_RUNTIME_DIR`; public help/syntax does not invoke location selection. `RuntimeLayout` derives fixed filenames from its roots. The expected UID is trusted host input, not client-selected authority.

`PROTOCOL_REVISION = 5` and `DEFAULT_RUNTIME_LIMITS` declare the reviewed framing/admission/deadline/history budgets; they implement no codec, deadline, readiness or authentication. Mac preflight constants reserve terminators: socket 103 bytes, pathname 1023 bytes, component 255 bytes. Linux selection/verification is deferred. See [contract tests](../../../tests/unit/test_runtime_contracts.py) and [path tests](../../../tests/unit/test_runtime_paths.py).

## Authenticated control contracts — M02 slice 2

`RuntimeControl.start/status/stop() -> ControlSuccess | Failure` binds one installation. `ControlObservation` separates process/control readiness, complete live dispatcher `application_ready` and a strict `storage` snapshot; schema facts appear only when storage is ready. Stopped status reports `not_checked`. Unknown outcomes are fixed-code failures, not guessed stopped results. `ControlFault`/`control_failure` map allowlisted descriptions without raw exceptions or frames.

`AuthRequest` masks its credential in repr and validates fixed-format input; it is internal transport data, never a public result. `AuthAccepted`, `ControlRequest` and `ControlResponse` are strict typed control messages, with protocol/request metadata separate from M01 JSON bodies. Hello/status/targeted stop remain control operations; checkpoint A adds separate typed project/receipt messages. No answer replay or complete history feature. Adapters must enforce actual framing/auth/deadlines; model construction alone confers no authority. See [codec tests](../../../tests/unit/test_runtime_control_protocol.py).

## Private storage contracts — M02 slice 3

`storage.py` owns immutable schema-1 settings (strict positive retention days, default 30), cached `StorageReadiness` and fixed privacy-safe storage errors. Protocol revision, settings schema and DB schema are independent. `StartupResult` in `runtime.py` is bounded child diagnostic data, never authenticated readiness authority. V1/V2/V3 do not negotiate or silently downgrade; an older peer closing the handshake remains unconfirmed, not a guessed bad credential.

`ProjectRegistry.index(validated_project) -> (Project, created)` and `lookup(validated_project) -> Project | None` expose immutable identity/root entries only. `RegistryService(projects, registry).execute("index" | "lookup", directory)` first validates current Git/YAML registration with `ProjectService`. Lookup never indexes, same-target index is a no-op, and conflicts/moves cannot rebind. No YAML settings, credentials or questions cross the registry port. Synchronous calls belong outside the control loop. `root_claimed(canonical_root)` is a read-only exact-root boolean that blocks missing-YAML identity regeneration; it never recovers an identity. YAML/DB updates are not a cross-file transaction.

## Project dispatch — M02 slice 4 checkpoints A/B/C

`dispatch.ProjectDispatcher.execute(ProjectCall, absolute_deadline)` supports init/config and both Git intents. It validates current Git/YAML and expected target, commits a unique receipt before init/registry/question work, revalidates the result scope and stores only a fixed terminal outcome before success. Duplicate IDs return metadata, never execute again or replay cached answers. Config only reads/revalidates; it never indexes or records activity. Unregistered init claims the canonical root with null UUID, then associates the actual YAML UUID exactly once before indexing. Possible cross-file side effects plus failure are unknown/partial, never false success or permission to regenerate identity. Checkpoint C now routes public project commands through this interface, without autostart/fallback. Full readiness requires all handlers/storage/control live; startup waits for it.

`inspect_receipt(ReceiptQuery, absolute_deadline)` revalidates project scope and returns metadata only. A nullable init receipt can be inspected at the same actual Git root without creating/repairing YAML; malformed/moved/linked YAML and expected-UUID conflicts still refuse. Established receipt UUIDs cannot be reassociated. A missing receipt is not proof that work never ran. `storage.py` owns frozen receipt values/ledger interface; schema 2 adds only receipts/index. Clean restart marks prior-instance unfinished receipts unknown, never reruns them. No raw questions/results/config/auth persistence; no history/debug/retention UI. Existing services accept optional explicit deadlines; direct M01 calls keep default behavior.

`ProjectStatusService` validates local Git/YAML before invoking the lazy runtime-control factory. `ProjectStatusSuccess` preserves ProjectData and explicitly adds `runtime`; it is not a project-action wire result. Config/status/inspection never implicitly index or create activity/access timestamps. `ProjectClient.status` is composed separately from daemon-dependent execution, not a fallback.

## Committed streams — slice5 C

`SubscriptionQuery`, finite ready/event/progress/closed frames and `ActivityClient`/owned `ActivityStream` live alongside project-scope orchestration. `subscribe` can raise fixed `ControlFault` for native/setup capability failures or `ActivityFault` carrying an existing application Failure; a terminal frame is not guaranteed before EOF. One consumer, explicit close/resume, no automatic reconnect/cursor storage/action replay. Scope is revalidated at setup and each bounded `StreamQuery`; no root-only init exception for feeds.

`StreamCursor` is distinct `activity.v1`, epoch/root/UUID/generation/position-bound; `StreamPage` advances exhausted intervals through committed high-water. Observed UUID filtering prevents old null-ID steps appearing behind a forward cursor; raw A replay stays unchanged. Ready does not jump a supplied cursor past its replay backlog. Per-frame cursors represent received positions, not consumer processing ACKs; debug remains the full trace interface. No new schema/dependency or raw archive.

## Public history/debug — slice 5 checkpoint B

Wire5's shared control model retains B's nine-capability subset for compatible parsing. C actually composes/advertises the tenth `activity.subscribe` only with live stream/worker/storage/drain wiring; new C start checks that capability instead of treating a B owner's nine-op readiness as complete C. DB remains3/settings1. Old wire4 is deliberately incompatible, with matching-old shutdown required.

`history.py` adds strict immutable history scope/read/item/page and bounded stateless cursor values without storage/dispatch imports. History uses indexed descending acceptance keysets, a fixed snapshot high-water and actual routing establishment as-of that ceiling; a nullable init becoming associated later cannot enter an earlier snapshot. States remain current per read. Exact debug reuses atomic scoped receipt/trace reading; latest selects only scoped instrumented membership. `no_history` is a nonfatal read result, never SQL-owner failure. No diagnostic inserts, access timestamps, replay, legacy backfill or expiry.

## Internal durable evidence — slice 5 checkpoint A

`history.py` owns immutable finite observation/event/trace/replay values without runtime/storage/dispatch imports. Actual Git completion emits only fixed category/code, process-start fact, monotonic duration and observed byte count after cleanup; prelaunch refusal cannot pretend a process started. Per-request sinks are explicit, never shared mutable request context. Evidence failure does not turn a partial request into ordinary success.

Schema 3 atomically appends accepted/running/identity/terminal/interrupted events with receipt transitions. At most 32 events/request with terminal capacity reserved. Earlier init observations keep truthful null known identity while their validated routing identity is associated once. Old schema-2 receipts remain exact-ID legacy metadata, with no invented events/backfill. Duplicate IDs do not create new activity.

`EvidenceClient.inspect_evidence(EvidenceQuery)` is an internal installed-client seam: exact scoped trace+receipt snapshot, or bounded project-root/UUID replay with epoch/sequence checks (max64). V4 evidence requests use the same native/auth/instance/frame/admission/deadline policy and bounded workers. Reads do not index/write timestamps/advance high-water. These were A-only contracts; B now adds `DebugQuery`/`HistoryQuery` through the same typed evidence envelope and bounded workers, not another broker. Public history/debug and opaque history cursors are implemented; C subsequently implements installed-client subscriptions; retention remains D.

## Retired UUID refusal, scheduling and invalidation — slice5 D1–D3 (development)

Current wire6/schema4/settings1 adds `RetirementBatch` and `ActivityMeta.replay_floor`. Schema4's permanent UUID-only tombstones exclude replay after full eligible receipt/evidence metadata is removed. A retired claim returns fixed `request_retired`, known nonacceptance, no cached answer/finish attempt or global diagnostic tombstone lookup. Unknown/accepted/running evidence remains. History/stream generations and raw replay floors explicitly refuse invalidated positions. D2 provides opt-in same-owner maintenance; D3 now invalidates active buffered pages and enables the bootstrap scheduler. D4/D5/full acceptance remain pending.

`RetentionProgress` is a frozen internal cached view of scheduled attempts/active work, next monotonic opportunity and last bounded committed batch/fixed failure. Reading it submits no SQL and lists no retired UUIDs. UTC samples choose cutoff; monotonic samples choose≥60s after ready/attempt completion. Invalid/observed-backward clocks defer rather than reuse a stale cutoff. Settings remain restart-loaded; a clock jump forward is not a trusted-time guarantee. Maintenance pause seals new retirement without claiming actual SQL/worker/native resource release.

D3's immutable `RetentionPosition` contains only checked-committed database epoch/generation/floor, never event payload or an alternate journal. Publication precedes hints; rollback/lost ACK never publishes speculative positions and storage failure withdraws the cache. Existing-stream pages, current sends and initial setup all compare the committed position; global generation changes require explicit resync, including unrelated-project streams. Already-admitted/sent frames can race expiry and cannot be recalled. Ready metadata includes the actual replay floor; the owned client rejects setup positions below it. No consumer ACK/exactly-once/reconnect or new retention RPC/CLI is introduced.

**Owning milestones:** M01 and M02 public control/storage/project-RPC foundation. [Unit tests](../../../tests/unit/test_projects.py) use controlled ports; real storage/discovery and installed checks are under [integration](../../../tests/integration/README.md) and [acceptance](../../../tests/acceptance/README.md).

Run `.venv/bin/python -m pytest tests/unit tests/integration tests/acceptance -q` after [installation](../../../README.md). Extend with typed use cases and ports when the next real behavior needs them. See [package map](../README.md).
