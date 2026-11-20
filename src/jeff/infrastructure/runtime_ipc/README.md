# Authenticated native IPC

`connection.py`, `requests.py`, `project_client.py` and `activity.py` own bounded client exchanges/streams; server modules authenticate native peers, validate envelopes and bind operations to the current instance and registered project before dispatch.

Wire14 has no older-wire or local-execution fallback. Request identity, admission, committed result and delivered reply are distinct; lost ACKs never grant replay or restore consumed approval. Activity cursors represent delivery positions, not completed client processing.

Transport does not construct policy/executors or export private controller identities. See [dispatch contracts](../../application/dispatch/README.md), [installed RPC checks](../../../../tests/acceptance/test_installed_project_rpc/README.md) and [activity-stream checks](../../../../tests/acceptance/test_installed_activity_stream/README.md).
