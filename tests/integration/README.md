# Integration tests

**Status:** M01 real storage/Git checks and M02 runtime path/private state/authenticated control lifecycle tests implemented. No SQLite, project dispatcher, execution sandbox or remote adapter tests yet.

## Current coverage

- [test_demo_generation.py](test_demo_generation.py): actual standalone generator repositories, unchanged parent Git metadata, deterministic baseline hashes, preservation of edits/branches/registrations, expected 1-pass/1-fail nested pytest result, and Compose YAML/mount structure.
- [test_demo_http.py](test_demo_http.py): brief loopback-only service startup, OS-allocated port, real health/404 checks, port collision/validation, and owned-process termination. No listener remains after each check; proxies are explicitly bypassed for loopback requests.
- [test_project_storage.py](test_project_storage.py): initial/repeated registration, preserved settings and bytes, subdirectory identity, read-only inspection, malformed/unsafe/oversized YAML, links/special files, real project moves, write failure cleanup/partial state, and detected concurrent changes.
- [test_git_questions.py](test_git_questions.py): real unborn/named/detached HEAD, staged/unstaged/untracked changes, renames, merge conflicts, protected paths, control-character filenames, query index/HEAD immutability, output/time limits, owned-child cleanup, malformed/non-UTF-8 metadata, and repository-filter non-execution. Non-UTF-8 names are parser-injected; controls use actual filesystem fixtures.
- [test_git_discovery.py](test_git_discovery.py): canonical symlink targeting, missing Git, timeout without retry, and rejection of a simulated tracked private configuration response.

- [test_runtime_path_inspection.py](test_runtime_path_inspection.py): owned short-path external fixtures for missing/private/unsafe entries, symlinked roots/ancestors, hardlinks/FIFOs/types/modes, nested unsafe roots, no content reads/writes/repairs, handle closure and import safety. Controlled OS-metadata faults exercise owner/type/replacement/denial/disappearance policy, including changed ancestors of missing roots. No socket is bound and no real alternate-user permission enforcement is claimed. Regular synthetic files named `ipc.key` contain no generated credential.

- [test_runtime_state.py](test_runtime_state.py): private preparation, real flock inheritance across exec, repeat-safe key/anchor preservation, unsafe credentials/roots/ancestors/collisions and 49 metadata-failure schedules checking descriptor closure. Only owned roots are mutated; generated fixture credentials are removed after use.
- [test_runtime_control_lifecycle.py](test_runtime_control_lifecycle.py): actual Mac native peer UID/PID, framed authentication, targeted shutdown, concurrent starts/restart, no rekey under ambiguous ownership, capacity, slow sender/deadline behavior, collision/start failures and genuine owned-child cleanup. The real 35-second case keeps a test process alive after socket removal/stop acknowledgement and requires unconfirmed shutdown, then terminates only its directly owned Popen fixture. No constant was shortened for that proof.

The test-only [fixture](../conftest.py) initializes and mutates Git repositories beneath pytest temporary directories with controlled Git config/template behavior. Those temp arenas must be genuinely outside Git ancestors, as explained in the [test map](../README.md); ignored scratch inside the source checkout is unsafe for outside-repository cases. Staging, commits, branches, merge conflicts, and synthetic filter configuration are confined to these fixtures; hooks/signing are disabled. Synthetic environment/key/credential paths contain no real secrets. No mutations to the Jeff repository's index/history occur. The tracked-config refusal is simulated rather than staging fixture files. Synthetic private values prove error/identity output does not echo document/parser details. Generated demos in tests are separate temporary copies, not the user's local examples. Compose is not launched by tests; local syntax-only validation is recorded separately. No image pulls or external network are needed.

**Dependencies:** pytest, actual local adapters, system Git, PyYAML, and Unix filesystem primitives on the current supported platform. Tests may compose concrete adapters; production clients may not bypass application policy.

Run from the source root after [installation](../../README.md):

```bash
.venv/bin/python -m pytest tests/integration -q
```

**Owning milestones:** M01 and M02 foundation. Slice-2 checks exercise current source and a separately built noneditable feature wheel, not the retained M01 wheel. Only internal control lifecycle is accepted by these tests; public CLI lifecycle, application dispatch, SQLite and full M02 acceptance remain unrun. Recorded M01 macOS suite/installed-wheel and user manual evidence does not establish Linux or other Python versions. Linux validation is explicitly deferred; a future Linux Docker run would not prove native Mac input/GUI behavior or every Linux desktop/backend. Static/no-follow checks and atomic files are not full adversarial filesystem isolation, cross-file transactions, or power-loss durability proofs. Hardware/providers remain separately approved future checks. See [test map](../README.md).
