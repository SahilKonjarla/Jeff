# Pinned Docker CLI transport

`transport.py` owns bounded captured CLI processes; `metadata.py`, `queries.py`, `verification.py` and `diagnostics.py` handle declared-target observations. `lifecycle.py` implements owned container operations. `guarded.py` owns the original separate-pipe JFG2 attachment and framed guard stderr; workload stdout remains raw bounded data.

Immutable observations must match declared policy, but PID/CID/name/label/nonce facts alone are never mutation authority. Guard protocol/transport uncertainty retains original captures observe-only: no automatic attach kill, input closure, Stop/remove, retry or adoption. Engine logs must satisfy the declared log-driver-none policy.

No default credential discovery, pull or host execution fallback. See [owned controller](../runtime_execution/README.md), [infrastructure map](../README.md) and [unit lifecycle tests](../../../../tests/unit/test_docker_owned_lifecycle/README.md).
