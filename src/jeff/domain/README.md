# Domain — tier 0

**Status:** M01 project identity, repository metadata and disclosure policy implemented; M02 slice-1 runtime value invariants added. Jobs/approvals remain unimplemented; the M02 internal control lifecycle uses these values without putting OS behavior in domain.

**Ownership:** `projects.py` defines immutable `Project(project_id: UUID, root: Path)` and `ProjectError(code, message)`. Identity requires an absolute root; filesystem canonicalization and configuration parsing belong to adapters. Errors contain fixed actionable messages, not raw file/provider output.

**Dependencies:** standard library and this tier only. No application, filesystem I/O, framework, infrastructure, integration, client, or bootstrap behavior.

**Example flow:** an application initialization creates a UUID-backed identity for the root returned by discovery. The store preserves an existing identity instead of replacing it; the application renders only public identity fields.

`repository.py` adds immutable `BranchState` and `FileChange` values, `protected_path`, matching Git exclusion pathspecs, and reversible ASCII `escape_metadata`. The denylist covers environment-family names (including templates), private Jeff state, credential/secret directories, and common key-file patterns at any depth. It is separate from Git ignore rules and cannot identify secrets in arbitrary names/content. Policy tests and the adapter keep pre-query exclusions and post-parse filtering aligned.

**Example question flow:** a Git adapter produces domain metadata, the application rejects protected paths on either side of renames, and branch/file names are escaped before crossing the public interface. No file contents are represented.

**Configuration/events:** no user-configurable grants or events yet. No credentials, arbitrary settings, emitted events, jobs, or approvals are represented by this slice.

## Runtime values — M02 slice 1

`runtime.py` defines frozen `RuntimeIdentity(instance_id: UUID, pid: int)` and `RuntimeSnapshot(state, identity=None)`, plus the six `RuntimeState` values. PID must be a positive integer (not bool); running/stopping require identity, stopped forbids it, and starting/unknown/unhealthy may lack identity. Invalid values raise fixed-message `RuntimeFault`, without echoing supplied data.

These are observations, not a lifecycle coordinator: constructing one never discovers, authenticates, starts or signals a process. PID/UUID values alone confer no authority. [Runtime contract tests](../../../tests/unit/test_runtime_contracts.py) cover immutability and invalid combinations. No OS defaults or installation I/O occur at import.

**Owning milestones:** M01 and M02 foundation. [Unit tests](../../../tests/unit/test_projects.py) check absolute roots, stable initialization, and application handling of stale identities. [Question policy tests](../../../tests/unit/test_questions.py) cover routing, filtering, and safe metadata; [architecture checks](../../../tests/architecture/README.md) enforce static import restrictions.

Run `.venv/bin/python -m pytest tests/unit tests/architecture -q` from the repository root after [installation](../../../README.md). Add pure rules when actual use cases need them; do not pull storage or runtime concerns into identity values. See [package map](../README.md).
