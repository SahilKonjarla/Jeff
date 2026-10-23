# Bootstrap — composition root

**Status:** M02 slice-4 C wires public explicit lifecycle, runtime-aware status and authenticated daemon-dependent project commands. Full M02 history/events/doctor/recovery and cross-platform acceptance remain pending.

**Ownership:** `main.py` passes lazy `create_project_client` and `create_runtime_controller` factories to CLI presentation. No location selection, project discovery, credential read or connection happens for help/syntax. There is no client-local question adapter or fallback path. Only the daemon composes `QuestionService(..., GitRepositoryRead())` after actual storage initialization.

**Dependencies:** composition may import all tiers; only entry points/bootstrap import bootstrap. No project/domain/storage policy is implemented here.

## Public entry points

`jeff.bootstrap.main:main` is the installed console entry; `python -m jeff` invokes the same function. Both use the same factories and application interfaces.

`runtime.create_project_client() -> ProjectClient` supplies the authenticated RPC adapter with lazy layout selection and `ProjectStatusService`: the latter first validates local Git/YAML, then selects the runtime controller for read-only status. The local project service is never an init/config/ask fallback. Status preserves identity fields plus explicit runtime observations; invalid/unregistered projects retain their local errors before runtime selection.

`runtime.create_runtime_controller() -> RuntimeControl` selects only the documented JEFF_DATA_DIR/JEFF_RUNTIME_DIR overrides and needed trusted host metadata. Both overrides skip home/temp lookup. Darwin TMPDIR selection avoids tempfile write probes; actual creation/validation is explicit startup only. Public start waits for authenticated matching-instance full application readiness (all five handlers, live workers and schema/settings readiness), not the startup pipe or control hello alone.

## Internal daemon ownership

`runtime.main()` remains an internal child entry with inherited lifetime-lock FD, bounded one-way startup-result pipe and non-secret instance UUID. It rejects invalid descriptors before state access, adopts actual ownership, applies restrictive umask, starts the dedicated SQL owner and composes bounded dispatch. No credential argv/environment, user-state scan or autostart.

The child retains its raw lock descriptor until kernel process exit. Cached control observations distinguish storage/handler/full readiness; partial control/storage startup does not claim application-ready. Diagnostic startup-pipe metadata is not authority. Initialization failure closes/exits; shutdown drains actual project workers before the SQL owner and native process exit. No arbitrary PID/name signalling or default-state repair.

Do not invoke internal entry points against normal/user installations as fixtures. [Installed control](../../../tests/acceptance/test_installed_runtime_control.py) proves matching installation/native identity, launcher exit survival and authenticated shutdown. [Public workflow](../../../tests/acceptance/test_daemon_cli_workflow.py) proves real public start/stop/project/restart/status and uncertain-delivery correlation, with no fallback.

After [installation](../../../README.md), run documented tests only in approved owned outside-Git arenas. [Architecture checks](../../../tests/architecture/README.md) enforce tier direction. Help/native local proof is not Linux/CI, desktop/audio/provider or formal milestone acceptance. See [package map](../README.md).
