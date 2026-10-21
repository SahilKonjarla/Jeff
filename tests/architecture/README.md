# Architecture tests

**Status:** initial M01 static import checks implemented in [test_dependencies.py](test_dependencies.py).

**Purpose:** enforce the [package dependency graph](../../src/jeff/README.md) without adding a checker dependency or production framework. The test parses all `src/jeff/**/*.py` files with the standard-library AST parser; imports are inspected, not executed.

## Enforced rules

- No production tier imports development `scripts` or `tests`.
- Domain imports only the standard library and its own tier.
- Application imports its own tier/domain, standard library, and Pydantic contracts.
- Infrastructure/integrations import their own tier, application, and domain, not clients/bootstrap.
- Sibling integrations do not import each other's implementations.
- Interfaces import their own tier/application/domain and approved presentation libraries, not concrete storage/execution/provider adapters.
- Selected technical libraries (`sqlite3`, `subprocess`, `socket`, `ctypes`) are rejected in application/interfaces.
- Only entry points and bootstrap itself may import bootstrap; composition can import all tiers.

The checker normalizes absolute, relative, and `from ... import ...` forms. Positive and negative examples test the checker itself, including sibling adapters and forbidden client imports.

Run from the repository root after [installation](../../README.md):

```bash
.venv/bin/python -m pytest tests/architecture -q
```

**Limits:** static imports only. Dynamic imports, arbitrary I/O through otherwise allowed standard-library modules, runtime authorization, subprocess isolation, dependency supply-chain integrity, and network behavior are not proved. Explicitly review checker changes when real client libraries arrive; do not weaken it to bypass application policy.

**Owning milestone:** M01 establishes this guard; every later slice maintains it. Passing static checks are not evidence of full runtime behavior or an execution sandbox. No external credentials or services are needed. See [test map](../README.md).
