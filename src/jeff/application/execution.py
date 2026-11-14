"""Legacy review-only actions and fresh internal authority ports; no executor or Docker I/O."""

import re
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from jeff.application.projects import ErrorData, Failure, ProjectService
from jeff.domain.execution import (
    ARGV_BYTES,
    ARGV_COUNT,
    PREPARED_FILE_BYTES,
    PREPARED_FILE_LIMIT,
    PREPARED_TOTAL_BYTES,
    AuthorityState,
    ReviewState,
    fingerprint,
    public_text,
)
from jeff.domain.projects import Project
from jeff.domain.repository import protected_path


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
        "execution_outcome_unknown": "A prior launch is unconfirmed; new claims are quarantined.",
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


class PreparedFile(_Model):
    path: str = Field(min_length=1, max_length=1023)
    size: StrictInt = Field(ge=0, le=PREPARED_FILE_BYTES)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("path")
    @classmethod
    def safe_relative_file(cls, value: str) -> str:
        path = PurePosixPath(public_text(value, 1023))
        if (
            path.is_absolute()
            or ".." in path.parts
            or str(path) != value
            or value == "."
            or "\\" in value
            or protected_path(value)
            or any(character in value for character in "*?[]")
        ):
            raise ValueError("Only literal unprotected relative files are supported.")
        return value


class PreparedInputs(_Model):
    """Inventory assertions, not proof that bytes were safely copied or verified."""

    snapshot_id: UUID = Field(default_factory=uuid4)
    policy: Literal["literal_files_v1"] = "literal_files_v1"
    files: tuple[PreparedFile, ...] = Field(default=(), max_length=PREPARED_FILE_LIMIT)

    @model_validator(mode="after")
    def bounded_inventory(self) -> Self:
        names = [item.path for item in self.files]
        if names != sorted(names) or len({n.casefold() for n in names}) != len(names):
            raise ValueError("Inventory must be sorted and have no colliding paths.")
        if self.total_bytes > PREPARED_TOTAL_BYTES:
            raise ValueError("Prepared input byte budget exceeded.")
        return self

    @property
    def total_bytes(self) -> int:
        return sum(item.size for item in self.files)

    @property
    def digest(self) -> str:
        return fingerprint({"policy": self.policy, "files": [f.model_dump() for f in self.files]})


class ExecutionManifest(_Model):
    """Fresh action identity; the trusted verifier must still check target and inputs.

    Not a subclass or reinterpretation of the permanently review-only ActionManifest.
    No RPC/CLI route admits this type until real target/input verification exists.
    """

    authority_kind: Literal["execution_v1"] = "execution_v1"
    verification: Literal["required_before_authority"] = "required_before_authority"
    profile: ExecutionProfile
    argv: tuple[str, ...] = Field(min_length=1, max_length=ARGV_COUNT)
    inputs: PreparedInputs

    @model_validator(mode="after")
    def coherent_action(self) -> Self:
        ActionManifest(profile=self.profile, argv=self.argv)
        fingerprint(self.model_dump(mode="json"))
        return self

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.model_dump(mode="json"))


class AuthorityOperation(_Model):
    """Trusted installation-internal port; current digest is a verifier obligation.

    Digest equality alone does not establish actual engine or input verification.
    The future verifier must check these before propose, approve and worker claim.
    """

    operation: Literal["propose", "approve", "deny", "show", "claim"]
    request_id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    canonical_root: Path
    owner_instance_id: UUID
    approval_id: UUID | None = None
    manifest: ExecutionManifest | None = None
    current_manifest_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("canonical_root")
    @classmethod
    def root(cls, value: Path) -> Path:
        return absolute_path(value)

    @model_validator(mode="after")
    def shape(self) -> Self:
        if (self.operation == "propose") != (self.manifest is not None):
            raise ValueError("Only a fresh proposal carries a manifest.")
        if (self.operation != "propose") != (self.approval_id is not None):
            raise ValueError("Only targeted operations carry approval identity.")
        if (self.operation in {"propose", "approve", "claim"}) != (
            self.current_manifest_digest is not None
        ):
            raise ValueError("Authority changes require a current verifier binding.")
        return self


class AuthorityRecord(_Model):
    proposal_id: UUID
    approval_id: UUID
    job_id: UUID
    project_id: UUID
    canonical_root: Path
    owner_instance_id: UUID
    manifest: ExecutionManifest
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: AuthorityState
    created_at_ms: StrictInt = Field(ge=0)
    expires_at_ms: StrictInt = Field(ge=0)
    claim_id: UUID | None = None
    claim_state: Literal["claimed", "unknown", "finished"] | None = None
    claimed_at_ms: StrictInt | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if (
            self.manifest_digest != self.manifest.fingerprint
            or self.project_id != self.manifest.profile.project_id
            or self.canonical_root != self.manifest.profile.canonical_root
            or self.expires_at_ms < self.created_at_ms
        ):
            raise ValueError("Authority identity or manifest facts disagree.")
        consumed = self.state == AuthorityState.CONSUMED
        if any(
            (item is not None) != consumed
            for item in (self.claim_id, self.claim_state, self.claimed_at_ms)
        ):
            raise ValueError("Only consumed authority carries a complete launch claim.")
        if self.claimed_at_ms is not None and not (
            self.created_at_ms <= self.claimed_at_ms < self.expires_at_ms
        ):
            raise ValueError("Claim time must be within the original approval lifetime.")
        return self


class AuthorityResult(_Model):
    record: AuthorityRecord
    # First confirmed claim response only. Never a replayable public bearer token.
    launch_claim: UUID | None = None

    @model_validator(mode="after")
    def fresh_claim(self) -> Self:
        if self.launch_claim is not None and (
            self.launch_claim != self.record.claim_id or self.record.claim_state != "claimed"
        ):
            raise ValueError("Fresh claim does not match consumed authority.")
        return self


class AuthorityStore(Protocol):
    def authority(self, operation: AuthorityOperation, *, deadline: float) -> AuthorityResult: ...


class DockerTargetDeclaration(_Model):
    """Explicit public image defaults, separate from all persisted manifest schemas.

    These declarations are not automatic environment discovery or secret detection.
    Future creation must override image argv/user/cwd and verify the effective config.
    """

    profile: ExecutionProfile
    image_environment: tuple[tuple[str, str], ...] = Field(default=(), max_length=32)
    image_user: str = Field(default="", max_length=128)
    image_entrypoint: tuple[str, ...] = Field(default=(), max_length=ARGV_COUNT)
    image_command: tuple[str, ...] = Field(default=(), max_length=ARGV_COUNT)
    image_working_dir: str = Field(default="", max_length=1023)

    @model_validator(mode="after")
    def public_defaults(self) -> Self:
        names = [name for name, _ in self.image_environment]
        if names != sorted(names) or len(names) != len(set(names)):
            raise ValueError("Image environment must be sorted and unique.")
        for name, value in self.image_environment:
            if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", name) or re.search(
                r"(?:PASSWORD|PASSWD|TOKEN|SECRET|CREDENTIAL|API_?KEY|ACCESS_KEY|AUTHORIZATION)",
                name,
            ):
                raise ValueError("Sensitive environment channels are unsupported.")
            public_text(value)
        for value in (
            self.image_user,
            self.image_working_dir,
            *self.image_entrypoint,
            *self.image_command,
        ):
            public_text(value)
        fingerprint(self.model_dump(mode="json"))
        return self

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.model_dump(mode="json"))


class DockerCLIIdentity(_Model):
    path: Path
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: StrictInt = Field(gt=0, le=268435456)

    @field_validator("path")
    @classmethod
    def canonical_path(cls, value: Path) -> Path:
        return absolute_path(value)


class VerifiedDockerTarget(_Model):
    """Observed metadata, not isolation proof, input verification or launch authority."""

    declaration_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    profile_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    binary: DockerCLIIdentity
    endpoint: str = Field(max_length=1023)
    engine_id: str = Field(min_length=1, max_length=128)
    image_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    platform: Literal["linux/arm64", "linux/amd64"]
    client_version: str = Field(max_length=64)
    server_version: str = Field(max_length=64)
    image_configuration_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    effective_environment: tuple[tuple[str, str], ...] = Field(max_length=35)
    isolation_verified: Literal[False] = False
    inputs_verified: Literal[False] = False
    launch_authority_issued: Literal[False] = False

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.model_dump(mode="json"))


class DockerFailureFacts(_Model):
    """Bounded allowlisted failure metadata; never raw CLI text or arbitrary field names."""

    returncode: StrictInt = Field(ge=-255, le=255)
    stdout_bytes: StrictInt = Field(ge=0, le=65536)
    stderr_bytes: StrictInt = Field(ge=0, le=65536)
    category: Literal["template_field_unavailable", "stderr_on_success", "other_cli_failure"]
    field_name: (
        Literal[
            "ID",
            "Id",
            "OS",
            "Os",
            "Architecture",
            "Variant",
            "Config",
            "Env",
            "User",
            "Entrypoint",
            "Cmd",
            "WorkingDir",
            "Volumes",
            "Healthcheck",
            "OnBuild",
        ]
        | None
    ) = None

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if self.stdout_bytes + self.stderr_bytes > 65536 or (
            (self.category == "template_field_unavailable") != (self.field_name is not None)
        ):
            raise ValueError("Inconsistent bounded Docker failure metadata.")
        return self


class DockerBackendFault(Exception):
    """Fixed non-secret refusal; never attach raw CLI output or parser exceptions."""

    def __init__(
        self,
        code: Literal[
            "docker_unavailable",
            "docker_unsafe_configuration",
            "docker_target_mismatch",
            "docker_unsupported",
            "docker_protocol_error",
            "docker_output_limit",
            "docker_output_failed",
            "docker_timeout",
            "docker_command_failed",
            "docker_cleanup_unconfirmed",
            "docker_busy",
            "docker_authority_required",
            "docker_ownership_unconfirmed",
            "docker_policy_mismatch",
            "docker_mutation_unconfirmed",
            "docker_journal_unconfirmed",
        ],
        *,
        diagnostics: DockerFailureFacts | None = None,
        observed_container_id: str | None = None,
    ) -> None:
        if (
            observed_container_id is not None
            and re.fullmatch(r"[0-9a-f]{64}", observed_container_id) is None
        ):
            raise ValueError("Unconfirmed container identity must be bounded.")
        self.code = code
        self.diagnostics = diagnostics
        self.observed_container_id = observed_container_id
        super().__init__(code)


class DockerTargetVerifier(Protocol):
    def verify_target(
        self, declaration: DockerTargetDeclaration, *, deadline: float
    ) -> VerifiedDockerTarget: ...


class PreparedInputBinding(_Model):
    """Trusted verifier's live binding, not permission to mount an arbitrary client path."""

    snapshot_id: UUID
    inventory_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    path: Path

    @field_validator("path")
    @classmethod
    def mount_path(cls, value: Path) -> Path:
        absolute_path(value)
        if any(character in str(value) for character in ',"'):
            raise ValueError("Ambiguous mount-path encoding is unsupported.")
        return value


class PreparedInputVerifier(Protocol):
    def verify(self, manifest: ExecutionManifest, *, deadline: float) -> PreparedInputBinding: ...
    def check(
        self, binding: PreparedInputBinding, manifest: ExecutionManifest, *, deadline: float
    ) -> None: ...


class DockerLaunchIntent(_Model):
    """Durable facts only: deliberately excludes transient AuthorityResult.launch_claim."""

    attempt_id: UUID
    record: AuthorityRecord
    target: VerifiedDockerTarget
    inputs: PreparedInputBinding
    nonce: str = Field(pattern=r"^[0-9a-f]{32}$")

    @model_validator(mode="after")
    def bound(self) -> Self:
        profile = self.record.manifest.profile
        if (
            self.record.state != AuthorityState.CONSUMED
            or self.record.claim_state != "claimed"
            or self.target.profile_digest != profile.fingerprint
            or self.target.engine_id != profile.engine_id
            or self.target.endpoint != profile.endpoint
            or self.target.image_id != profile.image_id
            or self.target.platform != profile.platform
            or self.inputs.snapshot_id != self.record.manifest.inputs.snapshot_id
            or self.inputs.inventory_digest != self.record.manifest.inputs.digest
        ):
            raise ValueError("Launch intent is not bound to consumed fresh authority.")
        return self

    @property
    def fingerprint(self) -> str:
        return fingerprint(
            {
                "attempt": str(self.attempt_id),
                "claim": str(self.record.claim_id),
                "job": str(self.record.job_id),
                "owner": str(self.record.owner_instance_id),
                "manifest": self.record.manifest_digest,
                "target": self.target.fingerprint,
                "inputs": self.inputs.model_dump(mode="json"),
                "nonce": self.nonce,
            }
        )


class DockerOwnedContainer(_Model):
    """Observation only. Controller-minted object identity is required for live control.

    Deserializing this model never adopts a container or reconstructs launch authority.
    """

    intent: DockerLaunchIntent
    container_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: str = Field(min_length=20, max_length=64, pattern=r"^[0-9TZ:.+\-]+$")


class DockerLaunchEvent(_Model):
    intent: DockerLaunchIntent
    phase: Literal["create_intent", "created", "start_intent", "exited", "cleaned", "unknown"]
    container_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    created_at: str | None = Field(
        default=None, min_length=20, max_length=64, pattern=r"^[0-9TZ:.+\-]+$"
    )
    # A captured create reply without verified birth/ownership is evidence only.
    observed_container_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    exit_code: StrictInt | None = Field(default=None, ge=0, le=255)

    @model_validator(mode="after")
    def shape(self) -> Self:
        if (self.container_id is None) != (self.created_at is None):
            raise ValueError("Owned identity and birth must be paired.")
        if self.phase == "create_intent" and (
            self.container_id is not None or self.observed_container_id is not None
        ):
            raise ValueError("Create intent cannot assert a container already exists.")
        if self.observed_container_id is not None and (
            self.phase != "unknown" or self.container_id is not None
        ):
            raise ValueError("Partial create replies are uncertainty observations only.")
        if self.phase not in {"create_intent", "unknown"} and self.container_id is None:
            raise ValueError("Observed lifecycle facts require exact container identity.")
        if self.phase not in {"create_intent", "unknown"} and self.created_at is None:
            raise ValueError("Observed lifecycle facts require creation identity.")
        if (self.phase == "exited") != (self.exit_code is not None):
            raise ValueError("Only observed exit carries an exit code.")
        return self


class DockerLaunchJournal(Protocol):
    """Must durably validate transition/claim uniqueness before returning; ACK loss raises.

    Enforce current owner/scope/manifest binding, original expiry at create/start,
    and permanent one-attempt exclusion. No default implementation: lifecycle must
    stay unwired until the SQL owner implements these obligations.
    """

    def record(self, event: DockerLaunchEvent, *, deadline: float) -> None: ...


class DockerOutputSink(Protocol):
    """Trusted bounded, nonblocking memory-only filter. Never persist raw input chunks."""

    retention_limit: int

    def accept(self, stream: Literal["stdout", "stderr"], data: bytes) -> None: ...
    def finish(self, stream: Literal["stdout", "stderr"]) -> None: ...


class DockerJobOutcome(_Model):
    exit_code: StrictInt | None = Field(default=None, ge=0, le=255)
    timed_out: bool = False
    outcome_unknown: bool = False
    cleanup_verified: bool = False


class DockerLifecycleBackend(Protocol):
    """Installation-internal Docker I/O seam; never caller-supplied CLI arguments."""

    identity: DockerCLIIdentity

    def engine_id(self, endpoint: str, *, deadline: float) -> str: ...
    def create(self, intent: DockerLaunchIntent, *, deadline: float) -> str: ...
    def inspect(
        self, endpoint: str, container_id: str, *, deadline: float
    ) -> dict[str, object]: ...
    def attach(
        self, endpoint: str, container_id: str, sink: DockerOutputSink, *, deadline: float
    ) -> int: ...
    def stop(self, endpoint: str, container_id: str, *, deadline: float) -> None: ...
    def kill(self, endpoint: str, container_id: str, *, deadline: float) -> None: ...
    def remove(self, endpoint: str, container_id: str, *, deadline: float) -> None: ...
    def absent(self, endpoint: str, container_id: str, *, deadline: float) -> bool: ...
