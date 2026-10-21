# Integrations — tier 2

**Status:** M01 root discovery and read-only branch/change metadata implemented. Other integrations and Git-write commands remain unimplemented.

**Ownership:** `git.py` implements `GitProjectDiscovery.find_root(directory) -> Path` and the application's `GitRead` port through `GitRepositoryRead.read_branch(root)` / `read_changes(root)`. Discovery canonicalizes non-bare roots and rejects tracked private config without changing the index.

**Dependencies:** domain/application ports and technical standard-library modules. No client/bootstrap or sibling integration implementation imports. Bootstrap injects adapters; they never grant execution authority.

## Queries and protections

The private runner resolves system-path Git and uses direct argv, never shell text. Every invocation gets five seconds and a 1 MiB combined stdout/stderr budget, bounded while reading. On failure it kills/reaps only its owned process and returns a fixed public-safe error. No raw stderr, retries, truncation-as-success, credentials, or inherited Git targeting variables.

Fixed controls disable hooks, fsmonitor, untracked-cache writes, global/system Git config/attributes, user attributes files, optional locks, submodule summaries, transports, lazy fetching, and prompting. No network or index mutation is authorized by a query. These controls are not a general OS sandbox.

- Discovery: `rev-parse --show-toplevel`, then `ls-files -- jeff.local.yaml`.
- Filter safety: effective config **key names only** from `config --includes --null --name-only --get-regexp` with a fixed filter-key pattern. Driver names are restricted before generating command-line disabling overrides. No command values are rendered. Change questions fail with `git_filters_unsupported` if drivers exist: running them is unsafe; disabling their conversion may misreport status. Branch-only status disables discovered drivers and excludes all file paths.
- Metadata: `status --porcelain=v2 --branch -z`, explicit untracked/rename options, `--ignore-submodules=all`, and `--no-ahead-behind`. Changes receive centralized protected-path exclusions before the query; branch-only queries exclude all paths. Repository config is never rewritten.

The NUL-delimited parser handles ordinary changes, rename origins, untracked paths, conflicts, and unborn/detached HEAD without line-splitting filenames. It rejects malformed/traversing metadata and over 10,000 changes, with no partial result. The application filters both sides of renames again and escapes display metadata.

**Scope:** no contents or raw diffs are returned; Git internally computes status for included files. Protected-pattern filtering is conservative, not arbitrary-secret detection. Environment templates are withheld from answers but remain Git-trackable. Changes explicitly exclude protected paths and submodule contents, so no full-clean claim is possible. Queries are observations, not a locked cross-command snapshot.

**Example:** `jeff ask "what changed?" --json` → registered project → safe filter preflight/status → parsed domain values → scoped application result. See [root usage](../../../README.md).

**Owning milestone:** M01. Run `.venv/bin/python -m pytest tests/integration tests/acceptance -q` after installation. [Discovery tests](../../../tests/integration/test_git_discovery.py) and [question tests](../../../tests/integration/test_git_questions.py) cover real Git states, query immutability, bounded stdout/stderr/time, malformed output, and filter-command non-execution. [Installed tests](../../../tests/acceptance/test_installed_questions.py) exercise console and module commands outside the source tree. Fixture-only Git mutations are explicitly approved; no Jeff repository index/history changes.

Linux, linked-worktree/submodule/partial-clone-specific scenarios, all hostile config combinations, and complete execution isolation remain unverified. Add other adapters only with real milestone behavior and their declared ports. See [package map](../README.md).
