# Bootstrap — composition root

**Status:** M01 CLI/discovery/configuration/question wiring implemented; daemon lifecycle and other adapters remain unimplemented.

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

**Owning milestones:** M01 local composition; M02 later adds daemon state/lifecycle. [Installed tests](../../../tests/acceptance/README.md) exercise public entry points outside the source repository; [architecture checks](../../../tests/architecture/README.md) reject inward bootstrap imports.

Run `.venv/bin/python -m pytest tests/acceptance tests/architecture -q` from the root after installation. Add explicit wiring only when a real adapter exists. Full daemon lifecycle/recovery and cross-platform acceptance remain pending. See [package map](../README.md).
