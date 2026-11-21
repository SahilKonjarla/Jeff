# CLI presentation

M04’s [user guide](../../../../README.md#pytest-jobs-m04) covers the command sequence and declared image setup. `execution_commands.py` registers `tests`, `jobs logs/cancel` and `watch`; `project_commands.py` routes `what failed?` to scoped pytest readback, not Git. `jobs list/show` remain review-only; filtered fresh jobs are under `execution jobs list`.

`app.py` registers commands; project/runtime/history/review/execution command modules call lazy application factories. `invocation.py` handles invocation/correlation and `rendering.py` emits human/JSON results. Presentation never constructs storage/Docker adapters or approves actions itself.

Help/syntax does no project/runtime selection. Legacy run/approvals/jobs stay review-only; guarded execution uses the distinct execution group with exact outer digest and approval IDs. Start/Cancel ACKs are not completion/termination. Uncertain replies preserve request IDs and direct read-only reconciliation, not retry.

See the [current interface and operator guidance](../README.md), [root examples](../../../../README.md) and [composition/CLI coverage](../../../../tests/integration/test_execution_composition/README.md). Concrete bootstrap wiring remains outside this package.
