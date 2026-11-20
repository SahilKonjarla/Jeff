# CLI presentation

`app.py` registers commands; project/runtime/history/review/execution command modules call lazy application factories. `invocation.py` handles invocation/correlation and `rendering.py` emits human/JSON results. Presentation never constructs storage/Docker adapters or approves actions itself.

Help/syntax does no project/runtime selection. Legacy run/approvals/jobs stay review-only; guarded execution uses the distinct execution group with exact outer digest and approval IDs. Start/Cancel ACKs are not completion/termination. Uncertain replies preserve request IDs and direct read-only reconciliation, not retry.

See the [current interface and operator guidance](../README.md), [root examples](../../../../README.md) and [composition/CLI coverage](../../../../tests/integration/test_execution_composition/README.md). Concrete bootstrap wiring remains outside this package.
