# Owned Docker lifecycle unit tests

Controlled ports model completion ownership, drift cleanup, exact mounts and stop lifetimes. These tests verify decisions/call provenance, not actual Docker/kernel behavior or VM-wide descendant absence.

Only original live handles may mutate; metadata mismatch and uncertainty must not become kill/remove/retry authority. Use approved synthetic fixture inputs and finite selections. See [controller contract](../../../src/jeff/infrastructure/runtime_execution/README.md), [unit guidance](../README.md) and [suite map](../../README.md).
