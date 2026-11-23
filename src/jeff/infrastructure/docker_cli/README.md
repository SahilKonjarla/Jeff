# Pinned Docker CLI transport

`transport.py` owns bounded captured CLI processes; `metadata.py`, `queries.py`, `verification.py` and `diagnostics.py` handle declared-target observations. `lifecycle.py` implements owned container operations. `guarded.py` owns the original separate-pipe JFG2 attachment and framed guard stderr; workload stdout remains raw bounded data.

Immutable observations must match declared policy, but PID/CID/name/label/nonce facts alone are never mutation authority. Guard protocol/transport uncertainty retains original captures observe-only: no automatic attach kill, input closure, Stop/remove, retry or adoption. Engine logs must satisfy the declared log-driver-none policy.

`compose.py` lists only the declared fixture, then projects Docker healthcheck status for exact matching container IDs without reading health output, logs, or environment. `jeff docker status --json` preserves `states` and aligns `health` and `published_ports` with it. Published ports come from Docker's runtime binding table, not from native-process discovery or configured/exposed ports. An empty list means no bindings were published; `null` means the binding observation was unconfirmed. A binding does not prove readiness. A changed observation or failed health probe is `unknown`; a Docker `healthy` result is not independent application readiness. Use matching client and daemon builds.

`jeff docker logs api --json` requires `allow_logs: true` under the private `compose_status` target and exactly one confirmed container for the requested service. Enable it only for fixtures whose output may be disclosed. The one-shot tail requests 40 lines and captures at most 16 KiB across both streams before sanitizing for display. Over-budget or sanitizer-withheld output is not returned or persisted. Pattern-based sanitization does not detect every possible secret. This is not log following or service control.

No default credential discovery, pull or host execution fallback. See [owned controller](../runtime_execution/README.md), [infrastructure map](../README.md) and [unit lifecycle tests](../../../../tests/unit/test_docker_owned_lifecycle/README.md).
