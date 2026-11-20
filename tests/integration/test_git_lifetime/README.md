# Git child lifetime integration

Runner/worker protocol and lifetime-lease fixtures cover bounded native Git work, caller/owner loss and cleanup accounting. Cases mix controlled ports with actual helper/process scenarios; inspect the selected case before execution.

Only genuine captured processes grant control. Unknown exits must quarantine rather than release admission or signal a remembered PID. Native/fault selections need approved finite outside-Git fixtures. See [worker contract](../../../src/jeff/integrations/git/worker/README.md), [integration policy](../README.md) and [suite map](../../README.md).
