# Native Git lifetime worker

`helper.py`, `runner.py`, `request_policy.py` and `limits.py` implement the narrow installed helper/runner protocol around fixed native Git capabilities. The inherited lifetime lease remains owned through real Git cleanup, so a caller/daemon disconnect cannot silently release live work.

Only captured known exits reconcile cleanup. Abnormal/unknown outcomes quarantine admission; stored PIDs and marker bytes never become replacement control handles. This is not a general host executor or a Docker containment boundary.

See [Git policy](../README.md), [lifetime integration checks](../../../../../tests/integration/test_git_lifetime/README.md) and [installed active-lifetime checks](../../../../../tests/acceptance/test_installed_active_lifetime/README.md). Process/fault tests need their own approved finite scope.
