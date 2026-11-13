"""Declared review-only actions and narrow use-case ports; no executor or Docker I/O."""

from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from jeff.application.projects import ErrorData, Failure, ProjectService
from jeff.domain.execution import ARGV_BYTES, ARGV_COUNT, ReviewState, fingerprint, public_text
from jeff.domain.projects import Project


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


def absolute_path(value: Path) -> Path:
    text = str(value)
    if not value.is_absolute() or ".." in value.parts or text.startswith("//"):
        raise ValueError("A canonical absolute path is required.")
    public_text(text, 1023)
    return value


class ExecutionProfile(_Model):
    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    project_id: UUID
    canonical_root: Path
    environment: Literal["development"] = "development"
    engine_id: str = Field(min_length=1, max_length=128)
    endpoint: str = Field(min_length=8, max_length=1023)
    image_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    platform: Literal["linux/arm64", "linux/amd64"]
    toolchain: str = Field(min_length=1, max_length=128)
    executable: str = Field(min_length=1, max_length=255)
    container_cwd: Literal["/project"] = "/project"
    project_view: Literal["sanitized_read_only"] = "sanitized_read_only"
    scratch_target: Literal["/scratch"] = "/scratch"
    network: Literal["none"] = "none"
    user: Literal["65532:65532"] = "65532:65532"
    root_filesystem: Literal["read_only"] = "read_only"
    privileges: Literal["drop_all_no_new_privileges"] = "drop_all_no_new_privileges"
    log_driver: Literal["none"] = "none"
    cpu_millis: StrictInt = Field(default=1000, ge=1, le=1000)
    memory_bytes: StrictInt = Field(default=268435456, ge=16777216, le=268435456)
    pids: StrictInt = Field(default=64, ge=2, le=64)
    scratch_bytes: StrictInt = Field(default=67108864, ge=4096, le=67108864)
    timeout_seconds: StrictInt = Field(default=300, ge=1, le=3600)
    output_bytes: StrictInt = Field(default=1048576, ge=1, le=1048576)
    public_environment: tuple[tuple[str, str], ...] = ()

    @field_validator("canonical_root")
    @classmethod
    def root(cls, value: Path) -> Path:
        return absolute_path(value)

    @field_validator("engine_id", "toolchain", "executable", "endpoint")
    @classmethod
    def public(cls, value: str) -> str:
        return public_text(value, 1023)

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if not self.endpoint.startswith("unix:///"):
            raise ValueError("Only an explicitly declared local Unix endpoint is supported.")
        absolute_path(Path(self.endpoint[7:]))
        executable = PurePosixPath(self.executable)
        if (
            not executable.is_absolute()
            or ".." in executable.parts
            or self.executable.startswith("//")
        ):
            raise ValueError("The declared executable must be an absolute container path.")
        allowed = {
            "LANG": {"C", "C.UTF-8", "en_US.UTF-8"},
            "LC_ALL": {"C", "C.UTF-8"},
            "PYTHONDONTWRITEBYTECODE": {"1"},
        }
        keys = [key for key, _ in self.public_environment]
        if len(keys) != len(set(keys)) or any(
            key not in allowed or value not in allowed[key]
            for key, value in self.public_environment
        ):
            raise ValueError("Only fixed public environment values are supported.")
        return self

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.model_dump(mode="json"))


class ExecutionSettings(_Model):
    profiles: tuple[ExecutionProfile, ...] = Field(default=(), max_length=16)

    @model_validator(mode="after")
    def unique(self) -> Self:
        identities = [(p.project_id, p.name) for p in self.profiles]
        if len(identities) != len(set(identities)):
            raise ValueError("Duplicate project/profile binding.")
        return self


class ActionManifest(_Model):
    profile: ExecutionProfile
    argv: tuple[str, ...] = Field(min_length=1, max_length=ARGV_COUNT)
    authority_kind: Literal["review_only_v1"] = "review_only_v1"
    target_verification: Literal["declared_only"] = "declared_only"
    execution_available: Literal[False] = False

    @field_validator("argv")
    @classmethod
    def public_arguments(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            public_text(value)
        if sum(len(value.encode("utf-8")) for value in values) > ARGV_BYTES:
            raise ValueError("Argument budget exceeded.")
        return values

    @model_validator(mode="after")
    def exact(self) -> Self:
        if self.argv[0] != self.profile.executable:
            raise ValueError("Manifest executable differs from its declaration.")
        fingerprint(self.model_dump(mode="json"))
        return self

    @property
    def canonical_root(self) -> Path:
        return self.profile.canonical_root

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.model_dump(mode="json"))


def build_manifest(
    project: Project, profile: ExecutionProfile, argv: tuple[str, ...]
) -> ActionManifest:
    if profile.project_id != project.project_id or profile.canonical_root != project.root:
        raise ValueError("Execution profile does not bind this project.")
    if not argv or argv[0] not in (profile.executable, PurePosixPath(profile.executable).name):
        raise ValueError("Executable is not declared by this profile.")
    return ActionManifest(profile=profile, argv=(profile.executable, *argv[1:]))


ExecutionOperation = Literal[
    "propose", "approve", "deny", "approval.show", "approval.list", "job.show", "job.list"
]


class ExecutionCall(_Model):
    request_id: UUID = Field(default_factory=uuid4)
    directory: Path
    expected_root: Path | None = None
    expected_project_id: UUID | None = None
    operation: ExecutionOperation
    profile_name: str = Field(default="default", pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    argv: tuple[str, ...] = Field(default=(), max_length=ARGV_COUNT)
    object_id: UUID | None = None
    after: StrictInt = Field(default=0, ge=0, le=9223372036854775807)
    limit: StrictInt = Field(default=16, ge=1, le=32)

    @field_validator("directory", "expected_root")
    @classmethod
    def path(cls, value: Path | None) -> Path | None:
        return absolute_path(value) if value is not None else None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if (self.operation == "propose") != bool(self.argv):
            raise ValueError("Only proposals carry argv.")
        if (self.operation in {"approve", "deny", "approval.show", "job.show"}) != (
            self.object_id is not None
        ):
            raise ValueError("Only targeted operations carry an object ID.")
        if not self.operation.endswith(".list") and (self.after != 0 or self.limit != 16):
            raise ValueError("Only list operations carry pagination.")
        for value in self.argv:
            public_text(value)
        return self


class ReviewRecord(_Model):
    sequence: StrictInt = Field(gt=0)
    proposal_id: UUID
    approval_id: UUID
    job_id: UUID
    manifest: ActionManifest
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: ReviewState
    created_at_ms: StrictInt = Field(ge=0)
    expires_at_ms: StrictInt = Field(ge=0)
    owner_instance_id: UUID
    job_state: Literal["waiting_for_approval", "blocked"]
    reason: Literal[
        "approval_required",
        "execution_unavailable",
        "approval_denied",
        "approval_expired",
        "approval_invalidated",
    ]
    execution_available: Literal[False] = False
    launch_authority_issued: Literal[False] = False

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.manifest_digest != self.manifest.fingerprint:
            raise ValueError("Stored manifest fingerprint does not match.")
        waiting = self.state == ReviewState.PENDING
        if waiting != (self.job_state == "waiting_for_approval"):
            raise ValueError("Review state does not match job disposition.")
        reasons = {
            ReviewState.PENDING: "approval_required",
            ReviewState.APPROVED: "execution_unavailable",
            ReviewState.DENIED: "approval_denied",
            ReviewState.EXPIRED: "approval_expired",
            ReviewState.INVALIDATED: "approval_invalidated",
        }
        if self.reason != reasons[self.state] or self.expires_at_ms < self.created_at_ms:
            raise ValueError("Review facts are inconsistent.")
        return self


class ExecutionData(_Model):
    items: tuple[ReviewRecord, ...] = Field(default=(), max_length=32)
    next_after: StrictInt | None = Field(default=None, gt=0)
    execution_available: Literal[False] = False
    message: Literal[
        "Review only: execution is unavailable. Fresh Phase3 approval is required."
    ] = "Review only: execution is unavailable. Fresh Phase3 approval is required."


class ExecutionSuccess(_Model):
    ok: Literal[True] = True
    data: ExecutionData
    error: None = None


ExecutionResult = ExecutionSuccess | Failure


class ExecutionReply(_Model):
    request_id: UUID
    identity: UUID | None = None
    result: ExecutionResult


class ReviewOperation(_Model):
    call: ExecutionCall
    project_id: UUID
    canonical_root: Path
    owner_instance_id: UUID
    manifest: ActionManifest | None = None
    configuration_fingerprint: str | None = None


class ReviewFault(Exception):
    """Fixed review refusal, not a raw storage or validation exception."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ReviewStore(Protocol):
    def review(self, operation: ReviewOperation, *, deadline: float) -> ExecutionData: ...


class ExecutionClient(Protocol):
    def review(self, call: ExecutionCall) -> ExecutionReply: ...


def execution_failure(code: str) -> Failure:
    messages = {
        "execution_profile_missing": "No declared execution profile binds this project.",
        "execution_invalid": "The public action or profile is invalid; no review was stored.",
        "execution_target_mismatch": "Execution declaration changed; no authority issued.",
        "execution_not_found": "No review record exists in this validated project scope.",
        "execution_conflict": "Review identity or decision conflicts; inspect existing facts.",
        "execution_expired": "The review expired or was invalidated; fresh review is required.",
        "execution_capacity": "The bounded review ledger is full; no additional review admitted.",
        "execution_clock": "Review clock is unconfirmed; no approval was granted.",
        "storage_busy": "Storage admission is busy; no automatic retry was made.",
        "storage_closing": "Storage is closing; inspect existing facts after restart.",
        "storage_outcome_unconfirmed": "Storage outcome is unknown; inspect rather than retry.",
        "storage_failed": "Storage failed; no automatic repair was attempted.",
    }
    safe = code if code in messages else "execution_invalid"
    return Failure(error=ErrorData(code=safe, message=messages[safe]))


class ExecutionService:
    def __init__(
        self,
        projects: ProjectService,
        store: ReviewStore,
        profiles: Callable[[], tuple[ExecutionProfile, ...]],
        instance_id: UUID,
    ) -> None:
        self.projects, self.store, self.profiles, self.instance_id = (
            projects,
            store,
            profiles,
            instance_id,
        )

    def execute(self, call: ExecutionCall, deadline: float) -> ExecutionReply:
        # ProjectService uses fixed internal discovery, never the proposed argv.
        observed = self.projects.execute("config", call.directory, deadline=deadline)
        if not observed.ok:
            return ExecutionReply(request_id=call.request_id, result=observed)
        project = Project(UUID(observed.data.project_id), Path(observed.data.root))
        if (call.expected_root is not None and call.expected_root != project.root) or (
            call.expected_project_id is not None and call.expected_project_id != project.project_id
        ):
            return ExecutionReply(
                request_id=call.request_id, result=execution_failure("execution_target_mismatch")
            )
        try:
            name = call.profile_name
            if call.operation == "approve":
                current = self.store.review(
                    ReviewOperation(
                        call=call.model_copy(update={"operation": "approval.show"}),
                        project_id=project.project_id,
                        canonical_root=project.root,
                        owner_instance_id=self.instance_id,
                    ),
                    deadline=deadline,
                )
                name = current.items[0].manifest.profile.name
            matches = [
                p
                for p in self.profiles()
                if p.name == name
                and p.project_id == project.project_id
                and p.canonical_root == project.root
            ]
            profile = matches[0] if len(matches) == 1 else None
            if call.operation == "propose" and profile is None:
                return ExecutionReply(
                    request_id=call.request_id,
                    result=execution_failure("execution_profile_missing"),
                )
            manifest = (
                build_manifest(project, profile, call.argv)
                if call.operation == "propose" and profile is not None
                else None
            )
            operation = ReviewOperation(
                call=call,
                project_id=project.project_id,
                canonical_root=project.root,
                owner_instance_id=self.instance_id,
                manifest=manifest,
                configuration_fingerprint=profile.fingerprint if profile is not None else None,
            )
            data = self.store.review(operation, deadline=deadline)
            result: ExecutionResult = ExecutionSuccess(data=data)
        except ReviewFault as error:
            result = execution_failure(error.code)
        except ValueError:
            result = execution_failure("execution_invalid")
        return ExecutionReply(request_id=call.request_id, identity=self.instance_id, result=result)
