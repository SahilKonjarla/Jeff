# Infrastructure — tier2

Owns concrete project files, private runtime state, native/authenticated IPC, lifecycle, SQL, committed event delivery and diagnostics. Depends inward on application/domain plus stdlib/PyYAML; no bootstrap/client policy or sibling integration calls. M02 is delivered/accepted on Mac; general execution isolation is not proved. M03 selects declared Docker jobs, not a general native sandbox rewrite; no container executor is implemented. Linux Jeff client/runtime compatibility is out of scope. [Tier map](../README.md).

## Adapter map

| Module | Responsibility |
| --- | --- |
|`project_files.py`|No-follow bounded YAML/ignore reads, stable project identity and atomic per-file publication|
|`runtime_paths.py`|Pure explicit layout resolution and fixed-role metadata snapshots; no state creation/content reads|
|`runtime_state.py`|Operation-time namespace checks, lifetime ownership, diagnostic leases, existing key, endpoint/storage provenance|
|`runtime_settings.py`|Strict private settings2 with project-bound execution declarations; empty defaults; byte-preserving legacy settings1 reuse|
|`runtime_ipc.py`|Strict framed native-peer/authenticated local IPC and owned client/stream connections|
|`runtime_process.py`|Installed child launch, authenticated readiness/concurrent convergence and verified native stop|
|`runtime_storage.py`|Single SQL-owner connection, checked DB5 migration/registry/receipts/evidence/recovery/retirement and durable review-only decisions|
|`runtime_dispatch.py`|Four owned workers/16 pending, typed queueing and actual drain|
|`runtime_events.py`|One serialized read pump, coalesced hints, retained pages/writers and committed-position invalidation|
|`runtime_diagnostics.py`|One-budget finite doctor projection: cached live facts or safely leased stopped reads|

Concrete Git lives in [integrations](../integrations/README.md); bootstrap injects its owned runner. Presentation never constructs these adapters or gains arbitrary SQL/command authority.

## Files and namespace authority

- `FileProjectStore.initialize/read` preserves mode0600 `jeff.local.yaml` identity/settings, private ignore rules and root binding. Config≤64KiB/ignore≤1MiB; YAML aliases/anchors/tags/excess structure/invalid identity refuse safely. `.jeff/` is reserved, not created. Existing files/unsafe links/hardlinks/special files never get silently repaired.
- Exclusive config publication refuses clobbering. Ignore replacement checks the observed fingerprint before atomic replacement; **check+replace is not atomic compare-and-swap**. Ignore/config/SQL are not one transaction. Failures may preserve partial files/rules and uncertainty; moved-root rebind is absent.
- Layout selection uses explicit trusted host inputs/overrides and known Darwin base aliases, not arbitrary symlink resolution or temp write probes. No-follow directory descriptors inspect fixed roles. Missing/present never establishes stopped/running/owned status. Snapshot checks narrow observed races; later operations revalidate.
- Operation-time directory checks include ancestry owner/writeability, private0700 leaves and0600 single-link files/socket. One coherent directory-only resample handles legitimate SQLite metadata interleaving without relaxing identity/security fields. POSIX/native checks are not complete ACL/malicious-same-UID isolation.
- `Ownership` is a real flock description. Exec inherits the same description; parent closes only its copy, never `LOCK_UN`. Stable empty anchor/key persist through stop. Child/helper ownership cannot be recreated from a PID, pathname or an old metadata snapshot.

## IPC, lifecycle and bounded work

Four-byte network-order UTF-8 JSON framing rejects duplicate/nonfinite/nesting/type/version/size errors before dispatch; advertised size is checked before body allocation. Verify actual native Mac UID/PID before credential exchange, constant-time credential checks and bound instance/request IDs. No unsupported-host success fallback, raw auth/frame/OS logging or ambient Git verbosity.

Native16 connections/15 ordinary preserve a control opportunity under ordinary saturation—not unlimited auth-flood protection. Streams share those slots, with at most8 peers and≤64 event credits **per peer**, including fetched page/current write and no hidden second queue. Internal4-active/16-pending workers and SQL1-active/16-pending are separate, not additive native connection allowances. Cancelling/timing out a caller is not thread/process/SQL completion or early slot release.

`RuntimeController` launches the installed interpreter with isolated environment/FDs and full authenticated readiness, not startup-pipe/PID/socket existence. Basic descriptor validation and metadata inspection precede adoption; credential contents follow ownership. Failed-start signals apply only to its captured Popen. Stop sends once and requires authenticated native exit plus released ownership/endpoint, not acknowledgement/disappearance. No name/PID kill, guessed replacement, default-state repair or action replay.

## Single-owner SQL and recovery

All SQL initialization/work/close stays on one non-daemon thread. Registry/ledger methods accept typed validated scope, expose immutable results and parameterized queries, not SQL callbacks/connections. Settings load once at startup; invalid/partial files fail without overwriting. Timed-out running SQL retains its slot; busy/closing/lost-ACK/close failures remain bounded or unconfirmed, never replayed.

New DB marker/ledger and ordered migrations validate supported structures; existing zero-byte/foreign/corrupt/newer/extra-object/mismatched-ledger candidates refuse without reset/downgrade. SQLite uses an escaped pathname URI checked before/after, **not** a verified-FD open or complete hostile-race guarantee. DELETE/FULL/trusted-schema-off/disabled extensions and bounded busy waits are enforced.

Endpoint receipt1 authenticates exact native namespace/key/anchor/socket/self bindings, nonce/instance and no-clobber publication before accept. Explicit start alone may retire a proven stale pair or matching receipt-only residue after exclusive ownership and definite refusal; doctor merely observes residue. Responding/unconfirmed/foreign/unsafe proof remains preserved.

Storage permit1 binds actual journal/DB/key/namespace/anchor/self identities before business DML on the same owner. BEGIN/real source-schema journal priming/permit publication precede mutations: schema4 before the4→5 migration, schema5 before current business writes. Legacy schema4 HMAC format is retained; recovered authenticated source schema/catalog/ledger must validate before migration. No-op writes roll back the primer. COMMIT/ROLLBACK cleanup retires only matching permission after SQLite removes its journal. Failure after COMMIT is not rollback proof. Rollback/proof-retirement exceptions resolve the active caller as unconfirmed, fail storage and seal admission rather than escaping with an unresolved future. Four bounded source checks cover that failure, signed legacy-format/migration ordering, actual DDL rollback and pre-DDL publication refusal; they do not demonstrate physical-crash/hot-journal recovery of DB5.

Startup authenticates/pins genuine hot DELETE-journal header/identities/bounds (recovery-only DB≤256MiB/journal≤16MiB) before writable SQLite recovery and current-schema/activity/integrity validation. WAL/SHM/super-journal, missing/replaced/unsupported proof and unproved creation/migration refuse. SQLite alone manages journals. No reset/rekey/manual journal deletion; project-file publication survives SQL rollback. Unknown prior-owner work is reconciled, never re-executed. [Recovery guidance](../../../docs/runbooks/m02-recovery.md).

## Review-only work — M03 Phase2, acceptance pending

Authenticated wire7 carries a separate typed review request through the existing bounded workers and single SQL owner. DB5 stores exact public manifests, project/root-bound proposal/approval/job identities and mutation UUID exclusion. Decisions are atomic, finite and permanently nonexecutable; approved jobs remain blocked, with no Docker/process adapter. The daemon loads private profiles once at startup. Restart invalidates unused review state; declarations are explicitly unverified and profiles are not repository-granted authority. Inspection may transactionally expire/retire review detail, unlike read-only M02 diagnostic/history interfaces.

## Evidence, retirement and streams

Receipt transitions/evidence commit together. Fixed observed Git category/code/start/duration/count facts never retain raw argv/streams/questions/answers/config/auth. Legacy metadata is explicit, without fabricated backfill. Debug/history reads use one owner item, stable scope/epoch/generation/snapshot membership and no diagnostic timestamps/indexing/cleanup. Missing/retired details are not nonexecution proof.

Retirement atomically tombstones≤100 eligible completed UUIDs before deleting detail and advancing generation/floor; retains registration and unknown/nonterminal ownership. Existing schema/index migration can scan beyond that batch bound. Clock/cutoff validation, ready-time cadence≥60s and completion-based backoff prevent busy-spin/catch-up loops. Scheduling is constructor-opt-in, enabled by actual bootstrap; diagnostics do not trigger it. Pause seals new maintenance without cancelling active SQL.

Checked committed retention position is published before hints; failure withdraws it. Hints contain no events and coalesce; periodic polling remains fallback. Setup/current pages/suspended sends compare positions even if the global read pump is busy. Abort only captured stale transports; bytes already delivered cannot be recalled. Writer64KiB high-water plus one-frame overshoot and5s drain bound do not cap kernel buffers. Stop gives streams an aggregate≤5s final-read/flush inside35s while actual workers/SQL remain accounted. Idle healthy streams have no total-lifetime setup deadline.

## Read-only diagnosis and validation

Doctor uses one5s deadline and existing checked credentials/native cache while live—no live client settings/SQLite read or second owner. Stopped metadata/settings/immutable SQLite reads retain a non-creating shared lease with final revalidation. Held observations must remain genuinely conflicting; an expired/closed lease grants nothing. Sidecar/ownership/deadline changes discard facts, not repair them. Active/configured retention labels and finite unavailable reasons expose no arbitrary environment/secret values.

[Integration tests](../../../tests/integration/README.md) and [installed checks](../../../tests/acceptance/README.md) cover distinct seams, including exact native identities, actual SQL/full/transaction faults, strict refusals and captured owner/child death. [Current evidence](../../../docs/jeff-progress.md) distinguishes accepted baseline from targeted cleanup/static-only checks. Use approved fresh Git-negative private fixtures; no user state/index/history, actual secrets or consumed fault slots. Jeff targets macOS only. Alternate-user/hosted, native device EIO/power-loss, hard syscall cancellation and general execution isolation remain unproved.
