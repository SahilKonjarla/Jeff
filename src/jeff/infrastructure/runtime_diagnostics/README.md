# Read-only runtime diagnostics

`adapter.py` assembles Doctor checks; `live.py` uses authenticated cached live observations; `stopped.py` inspects supported existing metadata only under stopped ownership; `reasons.py` maps safe failures.

Doctor never starts/repairs/migrates a runtime, creates a key/DB, opens a second live SQL reader or dumps environment/settings. Healthy base readiness does not certify optional Docker engine/image/isolation availability. Missing/unsafe/unconfirmed evidence remains explicit rather than authorizing cleanup.

See [runtime contracts](../../application/runtime/README.md), [diagnostic integration tests](../../../../tests/integration/test_runtime_diagnostics/README.md) and [installed Doctor checks](../../../../tests/acceptance/test_installed_doctor/README.md).
