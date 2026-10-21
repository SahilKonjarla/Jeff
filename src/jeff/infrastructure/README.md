# Infrastructure — tier 2

**Status:** M01 local project configuration adapter implemented. SQLite, IPC, execution isolation, credentials, and runtime recovery remain unimplemented.

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

Add future persistence/runtime adapters only against actual application ports. See [package map](../README.md).
