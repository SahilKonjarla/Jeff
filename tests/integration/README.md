# Integration tests

**Status:** M01 real configuration/discovery and branch/change metadata checks implemented. No daemon, SQLite, execution sandbox, or remote adapter tests exist yet.

## Current coverage

- [test_demo_generation.py](test_demo_generation.py): actual standalone generator repositories, unchanged parent Git metadata, deterministic baseline hashes, preservation of edits/branches/registrations, expected 1-pass/1-fail nested pytest result, and Compose YAML/mount structure.
- [test_demo_http.py](test_demo_http.py): brief loopback-only service startup, OS-allocated port, real health/404 checks, port collision/validation, and owned-process termination. No listener remains after each check; proxies are explicitly bypassed for loopback requests.
- [test_project_storage.py](test_project_storage.py): initial/repeated registration, preserved settings and bytes, subdirectory identity, read-only inspection, malformed/unsafe/oversized YAML, links/special files, real project moves, write failure cleanup/partial state, and detected concurrent changes.
- [test_git_questions.py](test_git_questions.py): real unborn/named/detached HEAD, staged/unstaged/untracked changes, renames, merge conflicts, protected paths, control-character filenames, query index/HEAD immutability, output/time limits, owned-child cleanup, malformed/non-UTF-8 metadata, and repository-filter non-execution. Non-UTF-8 names are parser-injected; controls use actual filesystem fixtures.
- [test_git_discovery.py](test_git_discovery.py): canonical symlink targeting, missing Git, timeout without retry, and rejection of a simulated tracked private configuration response.

The test-only [fixture](../conftest.py) initializes and mutates Git repositories beneath pytest temporary directories with controlled Git config/template behavior. Staging, commits, branches, merge conflicts, and synthetic filter configuration are confined to these fixtures; hooks/signing are disabled. Synthetic environment/key/credential paths contain no real secrets. No mutations to the Jeff repository's index/history occur. The tracked-config refusal is simulated rather than staging fixture files. Synthetic private values prove error/identity output does not echo document/parser details. Generated demos in tests are separate temporary copies, not the user's local examples. Compose is not launched by tests; local syntax-only validation is recorded separately. No image pulls or external network are needed.

**Dependencies:** pytest, actual local adapters, system Git, PyYAML, and Unix filesystem primitives on the current supported platform. Tests may compose concrete adapters; production clients may not bypass application policy.

Run from the source root after [installation](../../README.md):

```bash
.venv/bin/python -m pytest tests/integration -q
```

**Owning milestone:** M01. Local macOS evidence does not establish Linux or other Python versions. Static/no-follow checks and atomic files are not full adversarial filesystem isolation, cross-file transactions, or power-loss durability proofs. Hardware/providers remain separately approved future checks. See [test map](../README.md).
