# Prepared literal inputs

`filesystem.py`, `preparation.py`, `store.py`, `metadata.py` and `verification.py` implement bounded no-follow literal-file snapshots, metadata and integrity verification for the approved sanitized project view.

Inputs are explicit project-relative files, not glob/directory expansion. Trusted operators must review contents/argv as public and non-secret; snapshots/metadata may persist before approval. Protected names and byte hashes enforce exclusion/integrity, not universal secret certification. Preparation never grants launch permission or edits the source project.

See [execution contracts](../../application/execution/README.md), [input integration checks](../../../../tests/integration/test_prepared_execution_inputs/README.md) and [infrastructure map](../README.md).
