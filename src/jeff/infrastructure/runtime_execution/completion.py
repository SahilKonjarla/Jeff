"""One-shot completion recording; lost acknowledgement never restores authority."""

from typing import TYPE_CHECKING

from jeff.application.execution import (
    DockerBackendFault,
    DockerCompletion,
    DockerCompletionStore,
    DockerExecutionResult,
    DockerOutputCapture,
    DockerOwnedContainer,
)

if TYPE_CHECKING:
    from .controller import DockerJobController


def complete(
    self: "DockerJobController",
    handle: DockerOwnedContainer,
    capture: DockerOutputCapture,
    store: DockerCompletionStore,
    *,
    deadline: float,
) -> DockerExecutionResult:
    """One-shot finalization of this controller's actual run, never a DTO setter.

    Lost acknowledgement fences this caller; only facts inspection may discover
    a committed finish. In particular, never write unknown over that finish.
    """
    with self._operation():
        session = self._session(handle)
        if session.sink is not capture or session.outcome is None or session.completion_attempted:
            raise DockerBackendFault("docker_authority_required")
        session.completion_attempted = True
        try:
            self._deadline(deadline)
            try:
                output = capture.seal()
            except DockerBackendFault:
                output = capture.seal(abort=True)
            request = DockerCompletion(owned=handle, outcome=session.outcome, output=output)
            result = store.complete_execution(request, deadline=deadline)
            self._deadline(deadline)
            if result.completion_digest != request.fingerprint or result.disposition != (
                "completed" if request.confirmed else "quarantined"
            ):
                raise DockerBackendFault("docker_journal_unconfirmed")
            if result.disposition == "quarantined":
                self._quarantined = True
            return result
        except Exception:
            self._quarantined = True
            try:
                capture.seal(abort=True)
            except Exception:
                pass
            raise DockerBackendFault("docker_journal_unconfirmed") from None
        finally:
            session.sink = None  # Do not retain1MiB sealed captures for every historical handle.
