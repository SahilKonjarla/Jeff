# Legacy review persistence

Review lifecycle, schema4 migration and storage-failure fixtures cover project-scoped permanently review-only proposals/approvals/jobs. Approved legacy work remains nonexecuting; it cannot be upgraded to a fresh guarded claim.

SQL/filesystem injection tests do not prove physical crash recovery. Use approved synthetic outside-Git fixtures; preserve old serialization/digests and failed publication states. See [execution contracts](../../../src/jeff/application/execution/README.md), [integration policy](../README.md) and [suite map](../../README.md).
