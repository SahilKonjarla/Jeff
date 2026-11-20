# Native runtime ownership and recovery proofs

Identity, ownership, endpoint/store and proof modules validate private namespace metadata, lifetime locks, authenticated endpoint publication and narrowly supported stale-state recovery. `storage_recovery.py` supplies owned SQLite rollback provenance; diagnostics remain separate/read-only.

Missing PID/socket, free lock or connection failure alone never grants cleanup authority. Preserve unsupported/unsafe residue; no key/anchor/DB reset, adoption from logs or arbitrary PID signal. Native process exit and released ownership are distinct from job outcome/cleanup.

See [runtime contracts](../../application/runtime/README.md), [state ownership tests](../../../../tests/integration/test_runtime_state/README.md) and [recovery-boundary tests](../../../../tests/integration/test_runtime_recovery/README.md).
