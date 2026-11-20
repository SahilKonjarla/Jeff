# Project dispatch contracts

Application-owned requests/replies, project scope, observation/history and activity-subscription ports. `dispatcher.py` routes use cases; `evidence.py`, `status.py` and `subscriptions.py` define observable results. Concrete IPC, SQL and subprocess adapters belong outside this package.

Request acceptance, processing, committed evidence and caller delivery are separate events. Lost delivery or missing/retired detail never authorizes replay. Reads remain project-scoped; event positions are not client processing acknowledgements.

See the [application map](../README.md), [dispatch integration checks](../../../../tests/integration/test_project_dispatch/README.md) and [test execution boundary](../../../../tests/README.md).
