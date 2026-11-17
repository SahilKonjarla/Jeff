"""Bounded failure counts and allowlisted template diagnostics, never raw output."""

import re

from jeff.application.execution import DockerFailureFacts


def _failure_facts(
    code: int, stdout_bytes: int, stderr_bytes: int, prefix: bytes
) -> DockerFailureFacts:
    values: dict[str, object] = {
        "returncode": code,
        "stdout_bytes": stdout_bytes,
        "stderr_bytes": stderr_bytes,
        "category": "stderr_on_success" if code == 0 else "other_cli_failure",
    }
    # Match only fixed template syntax and known schema field names. No raw snippets escape.
    for field in (
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
    ):
        name = re.escape(field.encode("ascii"))
        if re.search(rb"can't evaluate field " + name + rb"(?:\s|$)", prefix) or re.search(
            rb"map has no entry for key [\"']" + name + rb"[\"']", prefix
        ):
            values.update(category="template_field_unavailable", field_name=field)
            break
    return DockerFailureFacts.model_validate(values)
