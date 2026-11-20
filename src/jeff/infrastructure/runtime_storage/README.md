# Serialized durable runtime state

One SQL owner (`runner.py`, `submission.py`, `work.py`, `transactions.py`) manages checked opening/catalog, registry/history/events/retention and execution authority/journal/results/stop evidence. Other modules provide narrow operations rather than independent live readers. Current schema is DB10.

Authority consumption, launch intent, completion and caller delivery are not one process/SQL transaction. Unknown/quarantined work and UUID exclusion survive restart/retirement; missing payloads do not prove nonexecution. Current-reader historical inspection validates original ownership without transferring mutation authority or clearing quarantine.

Filesystem/SQLite recovery proofs and stop payloads preserve existing digests/schema compatibility; older binaries cannot decode new guarded JSON. Logical retirement is not secure erasure. See [execution persistence tests](../../../../tests/integration/test_runtime_execution_authority/README.md), [storage tests](../../../../tests/integration/test_runtime_storage/README.md) and [recovery tests](../../../../tests/integration/test_runtime_recovery/README.md).
