# Application — tier 1

**Status:** M01 project identity and deterministic branch/change questions implemented. No daemon, arbitrary execution, or approval system yet.

**Ownership:** `projects.py` contains `ProjectService`, discovery/storage protocols, and Pydantic result contracts. Filesystem operations, YAML, Git subprocesses, and presentation belong elsewhere.

**Dependencies:** domain, application-owned contracts, standard library, and Pydantic. No concrete adapters, SDKs, subprocesses, clients, or bootstrap imports.

## Public interface

`ProjectService(discovery: ProjectDiscovery, store: ProjectStore)` exposes:

`execute(operation: Literal["init", "config", "status"], directory: Path) -> Success | Failure`

- `ProjectDiscovery.find_root(directory) -> Path` returns a validated canonical non-bare Git root.
- `ProjectStore.initialize(project) -> (Project, created)` preserves existing identity/settings and ensures private ignore rules.
- `ProjectStore.read(root) -> Project` is read-only and rejects missing/unsafe/stale state.

**Flow:** bind root through discovery → initialize or inspect through storage → reject a mismatched identity/root → return typed public fields. Initialization generates an ID only for new registration. Config/status return an actionable error for unregistered projects; they do not register them implicitly.

JSON results contain `ok`, `data`, and `error`. Success data includes `project_id`, canonical `root`, `config_path`, `registered: true`, and `created`. Failures contain only a stable error code and safe message. Unknown settings and raw adapter failures never enter the public result. Models forbid extra result fields; no runtime events are emitted yet.

## Repository question interface

`questions.py` exposes `QuestionService(projects, git: GitRead, clock=None).execute(question: str, directory: Path) -> QuestionSuccess | Failure`. `GitRead` declares `read_branch(root) -> BranchState` and `read_changes(root) -> tuple[FileChange, ...]`.

Only case/whitespace-normalized “what branch am I on?” and “what changed?” are accepted (final `?` optional). Unsupported input fails before project/Git access and is not echoed. Recognized questions bind the existing validated identity through `ProjectService`; they cannot initialize a project or execute commands.

Success data is a typed `BranchAnswer` or `ChangesAnswer` with project ID, canonical root, UTC observation time, and `metadata_encoding: json_escaped` for branch/file metadata. Branch results distinguish unborn/detached/named states. Changes include index/worktree codes, staged/unstaged/untracked/conflicted flags, and optional rename origin. Protected origin or destination removes the entire record before rendering.

Changes intentionally report `complete: false`, `visible_clean`, protected-path/submodule exclusion flags, and `filtered_records` for additional post-parse omissions. Pre-query excluded paths cannot be counted; no full-clean or total-hidden-change claim is made. Limit/time/adapter failures return `Failure`, never partial success. Filesystem/command behavior and output limits remain adapter responsibilities. Filter-equipped repositories fail closed for change questions; no model or general permission system is involved.

**Example:** `jeff ask "what changed?" --json` → normalized intent → registered project → Git port → scoped/escaped metadata → typed result. See [question unit tests](../../../tests/unit/test_questions.py) and [installed checks](../../../tests/acceptance/test_installed_questions.py).

**Configuration/security:** adapters own schema/file formats. The application requires stable project targeting and never silently rebinds moved projects. This slice exposes narrow registration behavior, not a general action approval/execution system.

**Owning milestone:** M01. [Unit tests](../../../tests/unit/test_projects.py) use controlled ports; real storage/discovery and installed checks are under [integration](../../../tests/integration/README.md) and [acceptance](../../../tests/acceptance/README.md).

Run `.venv/bin/python -m pytest tests/unit tests/integration tests/acceptance -q` after [installation](../../../README.md). Extend with typed use cases and ports when the next real behavior needs them. See [package map](../README.md).
