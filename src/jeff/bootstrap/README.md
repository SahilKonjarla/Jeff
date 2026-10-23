# Bootstrap — composition root

**Status:** M01 public CLI wiring and M02 internal authenticated control-child composition implemented. Slice 3 composes actual private settings/SQLite readiness. Public lifecycle commands and full application readiness remain unimplemented.

**Ownership:** `main.py` constructs `ProjectService(GitProjectDiscovery(), FileProjectStore())` and `QuestionService(projects, GitRepositoryRead())`, then passes both to `create_app`. It contains no project rules, storage implementation, policy decisions, or provider logic.

**Dependencies:** composition may import all tiers. Only entry points and bootstrap itself import bootstrap; domain/application/clients cannot depend on it.

## Entry points and flow

`main() -> None` is declared as `jeff.bootstrap.main:main` in `pyproject.toml`. `jeff/__main__.py` invokes it under the main guard.

After the [root install](../../../README.md):

```bash
.venv/bin/jeff --help
.venv/bin/python -m jeff --help
```

Both use the same service/adapters. Constructing them performs no project I/O; help is side-effect-free. Explicit `init`, `config`, `status`, or the two supported `ask` questions invoke application use cases, which call their declared ports. No daemon, provider, credential loading, runtime event bus, or job execution is initialized.

**Configuration/security:** the file adapter owns private schema/storage; Git discovery owns its narrow subprocess environment. Bootstrap must preserve those responsibilities and never become an alternate authorization path.

## Internal control composition — M02 slice 2

`runtime.create_runtime_controller() -> RuntimeControl` reads the two documented directory overrides plus trusted host metadata only when called. It constructs an installation-bound controller, not an alternate public CLI path. With both overrides it skips home/temp lookup. Otherwise standard Darwin TMPDIR metadata (canonical /private/tmp when unset) selects the temp base without tempfile.gettempdir's write probes; explicit startup performs actual path validation/creation. `runtime.main()` is the internal child entry point used with a validated inherited lifetime-lock FD, one-way startup-result pipe and non-secret instance UUID. It refuses invalid descriptors before state access, adopts real ownership, applies a restrictive child umask, then runs authenticated control IPC. No credential argv/environment, user-state scan, project dispatcher or autostart. Slice 3 starts the dedicated storage owner without blocking control on settings/SQL initialization.

The child retains its raw lock descriptor until kernel process exit. `application_ready` remains false; cached storage state separately reports initialization/readiness/failure. Parent start requires authenticated matching-instance control plus actual schema/settings readiness. The bounded pipe carries only safe startup metadata, never authority or raw errors. Failed initialization closes storage/control and exits; normal stop waits for actual owner-thread closure. Do not invoke this entry manually against default user paths or treat it as `jeff start`; public CLI wiring remains unchanged until the storage gate. [Installed tests](../../../tests/acceptance/test_installed_runtime_control.py) demonstrate parent/child installation identity, survival after launcher exit and authenticated shutdown from another caller.

**Owning milestones:** M01 local composition and M02 internal control lifecycle. [Installed tests](../../../tests/acceptance/README.md) exercise public entry points outside the source repository; [architecture checks](../../../tests/architecture/README.md) reject inward bootstrap imports.

Run `.venv/bin/python -m pytest tests/acceptance tests/architecture -q` from the root after installation. Add explicit wiring only when a real adapter exists. Full daemon lifecycle/recovery and cross-platform acceptance remain pending. See [package map](../README.md).
