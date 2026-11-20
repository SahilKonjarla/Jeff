# Native runtime contracts

`base.py`, `paths.py`, `protocol.py`, `observations.py`, `limits.py` and `failures.py` define application-owned layout, lifecycle, readiness and bounded control observations. Implementations live in infrastructure and are composed by bootstrap.

Only explicit start creates a daemon. Readiness, native process exit, storage availability and job confirmation are distinct. Stop may return runtime_stop_unconfirmed without granting arbitrary signals, replay or cleanup. Current wire14 requires matching client/daemon versions; no older-wire fallback.

See [bootstrap](../../bootstrap/runtime/README.md), [control-lifecycle checks](../../../../tests/integration/test_runtime_control_lifecycle/README.md) and [root usage](../../../../README.md).
