"""Lazy public CLI composition; only the daemon composes question execution."""

from jeff.bootstrap.runtime import create_project_client, create_runtime_controller
from jeff.interfaces.cli import create_app


def main() -> None:
    """Help/syntax does not select locations or create/connect to installation state."""
    create_app(create_project_client, create_runtime_controller)()
