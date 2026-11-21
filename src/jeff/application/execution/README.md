# Execution contracts and policy

Application values/ports for review-only legacy work and distinct guarded fresh execution. `manifests.py`, `profiles.py`, `prepared_inputs.py` and `docker_target.py` bind reviewed workload/target policy; `authority.py`, `launch.py`, `completion.py` and stop models distinguish permission, dispatch and durable outcomes. Requests/replies/views expose public facts, not controller handles.

`guard_execution.py` defines the executable JFG2 wrapper. Protocol/lifetime modules validate bounded candidate frames and one-shot timing; parsing alone proves neither an exclusive transport nor execution. JFG1 drafts and legacy approvals are never promoted. M04 uses wire14/DB12/settings4 guarded opt-in/default2.

`pytest_report.py` bounds and validates workload-supplied reports against the reviewed run token, known exit status and prepared paths. It does not authenticate the workload’s claims. Report v2 permits failed-only retry; v1 stays readable. `log_tail.py` holds recent sanitized bytes independently of the durable archive. `watch.py` defines project-scoped terminal notifications and explicit ACK; a read or log cursor is not an ACK.

Trusted owners classify development targets and review public/non-secret argv/files. Unknown outcomes retain quarantine/consumption; no stored identity restores execution or cleanup authority. See [policy/operator guidance](../../interfaces/README.md#m03-policy-and-uncertain-work-guidance), [coordinator](../execution_coordinator/README.md) and [guarded composition tests](../../../../tests/integration/test_execution_composition/README.md).
