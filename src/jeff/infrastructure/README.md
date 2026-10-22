# Infrastructure — tier 2

**Status:** M01 storage plus M02 paths/private state, lifetime ownership and real authenticated control IPC/process adapters implemented. SQLite, project dispatch, public CLI lifecycle, general execution isolation and stale recovery remain unimplemented.

**Ownership:** `project_files.py` provides `FileProjectStore.initialize(Project) -> (Project, created)` and `read(root) -> Project` for the application storage port. Policy decisions belong in application; Git discovery belongs in integrations.

**Dependencies:** application contracts/domain, standard library, and PyYAML. No clients, bootstrap, or sibling integration implementations.

## Configuration and flow

`jeff.local.yaml` contains `schema_version: 1` and `project` with string `id` (UUID) and `root` (canonical absolute path). Init creates it with mode 0600. Existing valid files are never rewritten, preserving unrelated settings, comments, and identity byte-for-byte. Config/status return identity only; they do not dump settings.

Initialization validates existing state and `.jeff`, then appends root-anchored `/jeff.local.yaml` and `/.jeff/` rules after existing ignore content. It does not create runtime state. New configuration is installed atomically without replacing an existing entry. Detected concurrent creation is read/validated rather than overwritten.

## Safety and limits

- Directory-relative, no-follow opens; reject symlink/special/hardlinked configuration/ignore files, non-owned files, and symlink/special `.jeff` entries.
- Configuration read limit: 64 KiB; existing ignore-file limit: 1 MiB. YAML aliases/anchors, nesting over 20, excessive parse events, unsafe tags, invalid identities, and unsupported schemas fail with safe messages.
- Temporary writes are mode 0600 and file-synced; new ignore files use 0644, existing ignore mode is preserved. Temporary paths are cleaned on handled failures.
- Ignore replacement checks the observed file fingerprint; detected concurrent edits fail without replacing them. This is not a filesystem transaction or defense against all malicious same-user races.
- Ignore/config updates are separate operations. A failed config write can leave added ignore rules; errors report possible partial state. No blind retries or promised cross-file rollback.
- A moved project is rejected; rebind is not implemented. No secrets are required or copied into configuration. Raw parser/filesystem error details are not rendered.

**Example:** installed `jeff init --json` calls the application, which invokes this adapter; a second init preserves the file and ID. See the [root usage](../../../README.md).

**Owning milestone:** M01. Run `.venv/bin/python -m pytest tests/integration tests/acceptance -q` after installation. [Storage tests](../../../tests/integration/test_project_storage.py) cover preservation, unsafe paths, synthetic private values, malformed/oversized YAML, moves, write failures, and detected races. Linux, power-loss durability, and complete execution isolation are not accepted by this slice.

## Runtime locations and inspection — M02 slice 1

`runtime_paths.resolve_layout(RuntimePathInputs) -> RuntimeLayout | Failure` is pure: no environment/home/temp lookup or filesystem access. Mac defaults select `<home>/Library/Application Support/Jeff` and `<temp>/jeff-<uid>`; explicit absolute directory overrides are independent. Known Darwin `/tmp`, `/var`, `/etc` base aliases normalize lexically to `/private/...`; arbitrary symlinks are not resolved. Empty/relative/NUL/dot-dot/ambiguous-double-slash/non-UTF-8 inputs fail safely. Encoded socket/path/component lengths are checked before metadata walks; no truncation or fallback.

`inspect_layout(layout) -> RuntimePathInspection | Failure` inspects only the two selected roots and five derived file roles. Directory-relative no-follow metadata traversal rejects leaf/ancestor links, wrong types, unexpected owner, non-0700 Jeff directories, non-0600 files/socket and file hardlinks. A rejected Jeff directory is not traversed, including when one root is nested inside the other. Platform parent directories need not be mode 0700. Directory handles close on success/failure; regular files are never opened/read. Observed replacement/disappearance/denial yields bounded unknown observations, not guessed absence. Unsafe/missing state is never repaired or created.

Only fixed role/state/issue facts are reported, not file bodies or raw OS errors. Missing does not prove stopped; a socket/lock does not prove running, ownership or readiness. This is a metadata snapshot, not a lease or complete hostile same-user race defense; actual open/create/lock operations must revalidate. Path/role counts are bounded, not a hard wall-clock deadline on every filesystem syscall. SQLite sidecars remain later work. Actual short-path socket binding is tested separately by slice 2, not by this read-only inspector.

[Pure resolver tests](../../../tests/unit/test_runtime_paths.py) and [owned filesystem tests](../../../tests/integration/test_runtime_path_inspection.py) cover these contracts. Socket-type acceptance and alternate-file-owner/race failures include controlled OS-metadata injection; no real bind/alternate-user security claim is made. No production installation state is used.

## Private control lifecycle — M02 slice 2

- `runtime_state.RuntimeStateStore` prepares only selected private roots/key/lock through operation-time checked descriptors. No settings/DB creation, credential rotation or unsafe-state repair. Roots cannot collide with fixed file roles. Ancestors must be root/current-user owned, without group/other write except protected sticky directories; they need not all be mode 0700. Existing state files retain 0600/no-follow/single-link policy. Credential reads are bounded and internal, unlike the content-free slice-1 inspector.
- `Ownership` represents an actual flock descriptor. Parent-to-child exec inherits the same locked description; closing the parent copy must not explicitly unlock it. The empty private lock anchor and credential remain across stop/restart; never unlink/recreate a live flock namespace. Metadata-failure cleanup includes late directory-context validation. Do not construct ownership from a PID/socket pathname.
- `runtime_ipc` implements strict length-prefixed UTF-8 JSON, duplicate/non-finite/nesting/type/version/size refusal, authentication before dispatch, constant-time credential comparison, native Darwin UID/PID checks, bounded asynchronous control admission and deadlines. Native getpeereid uses the SDK's uint32 UID/GID types; LOCAL_PEERPID binds returned identity to the real peer. No unsupported-platform fallback. Only hello/status/instance-targeted stop; no arbitrary/project operations.
- `runtime_process.RuntimeController` spawns the current installed interpreter with isolated argv/environment/FDs, verifies real control readiness, converges concurrent starts and authenticates stop. A native kqueue exit observation plus released ownership/absent endpoint confirms shutdown. An acknowledgement or missing socket alone is insufficient. No PID/name kill fallback; failed-start cleanup can signal only its genuinely created Popen child and otherwise returns unconfirmed ownership.

Applicable auth/frame/connection/readiness/stop budgets are enforced; project-worker/SQLite/subscription budgets remain declarations. Slow control I/O does not block the loop. Credential values never enter argv, environment, results or logs. Child bootstrap holds the raw lock FD until kernel process exit. Cleanup unlinks only a verified owned socket; ambiguous/stale collisions are refused, not recovered. Failed starts may leave valid private state or a stale path; no transactional rollback claim.

[State tests](../../../tests/integration/test_runtime_state.py), [native lifecycle tests](../../../tests/integration/test_runtime_control_lifecycle.py) and [installed control tests](../../../tests/acceptance/test_installed_runtime_control.py) cover real Mac sockets, lock handoff, concurrent starts, actual deadline failures and process cleanup. No production installation was used. POSIX modes/ownership, not a full ACL/same-user adversary audit; no privileged alternate-user or Linux validation. Full application readiness, DB flush/Git-work draining and public CLI lifecycle remain unimplemented.

Add future persistence/runtime adapters only against actual application ports. See [package map](../README.md).
