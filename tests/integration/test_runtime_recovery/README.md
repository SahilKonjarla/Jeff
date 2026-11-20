# Proof-bound storage recovery

Failure-boundary and permit-lifecycle fixtures distinguish eligible original-owned SQLite recovery from missing/foreign/unsafe proof. Injected write/publication errors and actor transitions are not universal physical I/O or power-loss tests.

Keep fixture ownership/proofs and failed state intact. No automatic key/anchor/DB reset, force adoption or broad cleanup; fault/process setup needs its own approved finite scope outside Git. See [recovery/state contract](../../../src/jeff/infrastructure/runtime_state/README.md), [integration policy](../README.md) and [suite map](../../README.md).
