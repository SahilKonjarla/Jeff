"""Fixed service health probe for an explicitly supplied daemon relay."""

import re
import time

from jeff.application.compose_lifecycle import ServiceAttempt, ServiceResources
from jeff.application.execution import DockerBackendFault

from .metadata import _json
from .transport import _DockerCLI

_ENGINE = "{{json .ID}}"
_CONTAINER = (
    '[{{json .Id}},{{json .Created}},{{json .Image}},{{json .State.Running}},'
    '{{json .HostConfig.PublishAllPorts}},{{json .HostConfig.PortBindings}},'
    '{{json .NetworkSettings.Ports}},'
    '[{{range .NetworkSettings.Networks}}{{json .NetworkID}},{{end}}null]]'
)
_NETWORK = (
    '[{{json .Id}},{{json .Created}},{{json .Internal}},{{json .Driver}},'
    '[{{range $id, $entry := .Containers}}{{json $id}},{{end}}null]]'
)
# Fixed code, destination and path. Do not relay status lines, headers or response bodies.
_PROBE = (
    'import contextlib,http.client,signal,sys\n'
    'signal.alarm(1)\n'
    'try:\n'
    '    with contextlib.closing('
    'http.client.HTTPConnection("127.0.0.1",8000,timeout=0.75)) as connection:\n'
    '        connection.request("GET","/health")\n'
    '        with connection.getresponse() as response:\n'
    '            sys.stdout.write("healthy" if response.status == 200 else "unhealthy")\n'
    'except (OSError,http.client.HTTPException):\n'
    '    sys.stdout.write("unhealthy")\n'
)


class DockerServiceHealthProbe(_DockerCLI):
    """A relay-callable read of health; not a general container exec interface."""

    def __call__(
        self, attempt: ServiceAttempt, resources: ServiceResources, deadline: float
    ) -> bool:
        manifest = attempt.relay_start
        if (
            manifest is None
            or not attempt.matches_review()
            or not re.fullmatch(r"[0-9a-f]{64}", resources.container.id)
            or not re.fullmatch(r"[0-9a-f]{64}", resources.network.id)
            or time.monotonic() >= deadline
        ):
            return False
        endpoint = manifest.target.endpoint
        try:
            socket_identity = self._socket(endpoint)

            def query(*args: str) -> bytes:
                if time.monotonic() >= deadline or self._socket(endpoint) != socket_identity:
                    raise DockerBackendFault("docker_target_mismatch")
                reply = self._control(args, endpoint=endpoint, deadline=deadline)
                if self._socket(endpoint) != socket_identity:
                    raise DockerBackendFault("docker_target_mismatch")
                return reply

            if _json(query("info", "--format", _ENGINE)) != manifest.target.engine_id:
                return False
            container = resources.container
            network = resources.network
            observed = _json(query("container", "inspect", "--format", _CONTAINER, "--", container.id))
            if not isinstance(observed, list) or len(observed) != 8:
                return False
            if (
                observed[:3] != [container.id, container.created_at, manifest.image_id]
                or observed[3] is not True
                or observed[4] is not False
                or observed[5] not in (None, {})
                or not _no_bindings(observed[6])
                or observed[7] != [network.id, None]
            ):
                return False
            result = _json(query("network", "inspect", "--format", _NETWORK, "--", network.id))
            if (
                not isinstance(result, list) or len(result) != 5
                or result[:2] != [network.id, network.created_at]
                or result[2] is not True or result[3:] != ["bridge", [container.id, None]]
            ):
                return False
            return query(
                "container", "exec", "--user", "65534:65534", "--", container.id,
                "/usr/local/bin/python", "-I", "-B", "-c", _PROBE,
            ) == b"healthy"
        except (DockerBackendFault, ValueError, RecursionError):
            return False


def _no_bindings(value: object) -> bool:
    return value is None or (
        isinstance(value, dict) and all(bindings in (None, []) for bindings in value.values())
    )
