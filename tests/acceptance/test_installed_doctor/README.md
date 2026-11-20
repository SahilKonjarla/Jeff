# Installed Doctor scenarios

Protocol/cache/transition checks for absent, stopped and authenticated live diagnostics, with read-only tripwires and bounded support fixtures. Distinguish real installed command behavior from injected cache/protocol failures.

Doctor must not start/repair/migrate state, create credentials or open a second live SQL reader. Process-backed setup still needs explicit finite authority and owned outside-Git paths; read-only product behavior does not make setup side-effect-free. See [acceptance policy](../README.md), [diagnostics adapter](../../../src/jeff/infrastructure/runtime_diagnostics/README.md) and [suite map](../../README.md).
