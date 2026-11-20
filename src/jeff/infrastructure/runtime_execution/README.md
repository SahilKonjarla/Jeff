# Original-owned guarded job execution

`controller.py`, `session.py`, `create.py`, `run.py`, `completion.py`, `cancellation.py` and `cleanup.py` implement the existing claimed/journalled job lifecycle. `policy.py`/`arguments.py` check the exact container contract. Guard policy/run/artifact modules wrap the original reviewed workload without a parallel executor.

The two packaged Linux guard binaries are immutable allowlisted resources: no compile/download/setup hook. Verify artifact/filter/platform/ownership/file identity and exact read-only mount/argv before authority and execution. JFG1 source/drafts remain historical; configured executable work uses JFG2.

Only genuine original controller/session/attachment captures confer mutation rights. Typed workload terminal, deadline, logical EOF, namespace exit and verified removal are separate. Protocol/transport uncertainty blocks automatic mutation/release/retry/adoption, even after a daemon restart or administrative orphan removal.

Runtime proof is selected ARM64 Docker Desktop, not AMD64 execution, universal clock-floor or VM-wide census. See [CLI transport](../docker_cli/README.md), [application contracts](../../application/execution/README.md), [composition tests](../../../../tests/integration/test_execution_composition/README.md) and [owned lifecycle tests](../../../../tests/unit/test_docker_owned_lifecycle/README.md).
