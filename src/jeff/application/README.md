# Application — tier1

Owns use cases, typed public results, policy and narrow ports. Depends only on domain/application contracts, stdlib and Pydantic—not concrete sockets, processes, SQL/YAML adapters or bootstrap. M02 is delivered/accepted; general execution/approvals remain later work. [Tier map](../README.md).

## Interfaces

| Module / seam | Contract |
| --- | --- |
|`projects.ProjectService.execute(init/config/status, directory, deadline=...)`|Bind canonical Git root, initialize or inspect through discovery/store ports; return `Success|Failure` with identity only|
|`questions.QuestionService.execute(question, directory, deadline=..., observer=...)`|Recognize exactly branch/change intents before project/Git access; require registration, return timestamped escaped metadata or fixed failure|
|`storage.ProjectRegistry` / `RegistryService`|Index/lookup validated UUID/root; identical index is no-op, conflicts/moves refuse, YAML remains authority|
|`storage.RequestLedger`|Typed claim/finish/inspection/history/replay/stream reads; no exposed SQL, raw-answer replay or identity repair|
|`dispatch.ProjectDispatcher`|Scope/expected-target validation, durable action claim, one execution and fixed terminal facts; config is read-only/unindexed|
|`dispatch.ProjectClient` / `EvidenceClient`|Daemon action/evidence ports, separate local read-only status; no autostart/local action fallback|
|`dispatch.ActivityClient` / `ActivityStream`|Explicit owned subscription, one reader, finite frames, explicit resume/resync; EOF need not include a terminal frame|
|`runtime.RuntimeControl`|Explicit start/status/stop; typed observations are not themselves process/authentication authority|
|`diagnostics.RuntimeDiagnostics.inspect()`|Seven ordered finite checks/report; preserve unhealthy data, no lifecycle/repair work|

Protocols declare adapter obligations; model construction/declared budgets do not enforce native ownership, framing or cancellation. Bootstrap wires implementations. Help/syntax must not invoke factories or select installation paths.

## Project and question semantics

Discovery returns a canonical non-bare root without executing user commands. Store initialize preserves valid existing identity/settings; read never initializes. Rebind is not implemented. Init generates a UUID only when registration is absent; mismatched stored roots fail without replacing identity.

Success has `ok/data/error`; project data exposes ID/root/config path/registration/created, never arbitrary settings. Status explicitly adds runtime observations. Fixed errors do not forward raw parser/OS/Git details or validation inputs.

Questions accept only case/whitespace-normalized “what branch am I on?”/“what changed?” with optional final `?`; unsupported input is neither echoed nor sent to Git. Git ports return domain values. Application filters both rename sides and JSON-escapes metadata. Changes remain `complete:false`/`visible_clean`, with protected/submodule exclusions; pre-query hidden paths cannot be counted and no full-clean/content-secret-detection claim is made. Observer failures cannot rewrite uncertain work as ordinary success. No model/general execution authority.

## Identity, admission and evidence

- Directory/expected ID/root are untrusted assertions; server binds actual Git/YAML identity. Init claims an unregistered root with null UUID, then associates validated YAML identity once while running. Missing YAML at an indexed root cannot generate a replacement.
- Durable claim precedes action mutation, not initial read-only discovery. Transport/worker admission is not acceptance. Duplicate/retired UUIDs never rerun actions or replay cached answers. Lost claim/delivery/commit acknowledgement stays unknown; absent receipt/details is not nonexecution proof.
- YAML/ignore/SQL are not one transaction. Published files survive SQL failure; explicit new init may index validated identity, not replay the old action or erase its unknown truth.
- Config/status/evidence/doctor/lifecycle diagnostics do not index implicitly, record access timestamps or displace latest relevant init/ask. Nullable-init exact inspection binds the same actual root and never repairs missing YAML.
- Actual Git observations contain fixed category/code/start/duration/byte facts, not raw argv/streams/questions/answers. Durable transitions/events share SQL ownership and commit. Legacy metadata is explicit, never invented history/backfill.

## History, streaming and retention

History uses project/root/epoch/generation-bound keyset cursors with a snapshot acceptance ceiling; membership stays stable while receipt state can advance. Late identity association cannot enter earlier snapshots. Exact debug reads receipt+trace atomically; latest chooses scoped accepted init/ask, not completion order. Empty/no-history is explicit and nonfatal.

Stream cursors are distinct from history cursors. Observed UUID filtering never retrofits old null-ID events behind forward positions; exact replay/debug retains original truth. Exhausted intervals advance through committed high-water. Received positions are not processing ACKs. No automatic reconnect/action replay/cursor persistence; idle wait is distinct from partial-frame/write deadlines.

DB4 keeps UUID-only permanent tombstones, preserving registrations and unknown/unfinished ownership. Validated retention defaults30days, restart-loaded; same-owner scheduling retires≤100 eligible completed requests per≥60s interval. UTC chooses eligibility, monotonic time cadence; invalid/backward samples defer. `RetentionProgress`/`RetentionPosition` are cached internal facts, not SQL reads or another event journal. Checked committed epoch/generation/floor publication precedes hints; rollback/lost ACK cannot publish speculative positions. Setup/pages/suspended writers recheck invalidation independently of the global read pump. Already-sent bytes cannot be recalled; logical expiry is not secure erasure.

## Runtime and doctor values

Wire6/DB4/settings1 are independent of package/local proof versions; no version negotiation/downgrade. `RuntimeLayout` derives fixed names from trusted host-selected roots; construction does not create state/authority. Inspection contains seven fixed roles and bounded metadata, never content. Native adapters enforce path/credential/peer/deadline/admission rules; PID/UUID/model fields alone authorize no lifecycle action.

Doctor healthy requires verified full readiness/all supported capabilities; stopped facts require a non-creating lease and current settings/schema, not invented live identity. Live cached active retention differs from stopped configured inactive retention; huge values use `above_report_limit`. Output≤8KiB, safe unavailable/unhealthy reasons, no environment dump/recovery/provider start. Command-specific failures retain their report rather than discarding diagnostic data.

[Project tests](../../../tests/unit/test_projects.py), [question tests](../../../tests/unit/test_questions.py), [real dispatcher tests](../../../tests/integration/test_project_dispatch.py), [installed checks](../../../tests/acceptance/README.md) exercise different seams. Use approved outside-Git fixtures and the [test map](../../../tests/README.md); model/port tests alone prove no native behavior. Detailed limits/recovery and acceptance qualifications live in the local [runtime contract](../../../docs/specs/m02-runtime-and-state.md) and [progress](../../../docs/jeff-progress.md), which may be absent from a fresh clone.
