# Fresh execution coordination

Pytest runs and retries still use the ordinary proposal, approval and first-claim path. Retries derive selection from a retained v2 report and recheck the original profile and file inventory. Admission attaches only a read-only log tail; watch registration, delivery reads and ACK are scoped SQL-owner operations, not job control.

`coordinator.py` and `requests.py` route fresh project-scoped operations through supplied application ports. `proposal.py` verifies target/prepares inputs; `binding.py` revalidates reviewed identity; `admission.py`, `lifecycle.py` and `cancellation.py` coordinate single consumption, owned work and stop intent.

A proven read-only unavailable target refuses before preparation/authority. Faults after persistence/claim and uncertain helper cleanup stay unknown; identical error strings do not prove identical side-effect phases. ACK is not guest completion, and duplicate reads/requests never reconstruct a launch claim.

No concrete Docker/storage construction or second executor belongs here. See [contracts](../execution/README.md), [bootstrap composition](../../bootstrap/runtime/README.md) and [composition/refusal coverage](../../../../tests/integration/test_execution_composition/README.md).
