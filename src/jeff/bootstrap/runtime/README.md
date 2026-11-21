# Runtime composition

`clients.py` supplies lazy client/control factories; `child.py` and `serving.py` establish authenticated daemon ownership/storage/handlers; `execution.py` assembles optional execution ports. Entry points alone select concrete adapters; help/syntax performs no runtime discovery or connection.

Guarded launches require explicit private settings4 development profiles, matching pinned CLI/config/fresh managed HOMEs and installation-owned guard verification. Default settings2 has no backend; settings3 remains readable but cannot authorize a new guarded launch. Current M04 uses wire14/DB12; use matching client and daemon builds. Bootstrap injects pytest report capture, live log tail attachment and the watch store into the existing execution coordinator. No image acquisition or job launch occurs during startup.

Initialization uses existing bounded workers and actual readiness/drain, without Docker queries, pulls or jobs at startup. Expected backend faults disable only optional execution; owner/storage faults remain fatal. No new executor, host fallback or automatic resource adoption.

See [composition ownership](../README.md), [fresh coordinator](../../application/execution_coordinator/README.md) and [bootstrap/guard checks](../../../../tests/integration/test_execution_composition/README.md).
