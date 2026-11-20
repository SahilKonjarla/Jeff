"""Bind typed leaf implementations without changing original receiver annotations."""

from collections.abc import Callable


def _method[Function: Callable[..., object]](function: Function) -> Function:
    """Keep the same function/descriptor; erase only its helper-only self annotation.

    The annotation lets strict type checking see the real single store owner.
    Removing it restores the original introspection contract, without wrappers,
    extra state, altered defaults or runtime resolution of a circular class name.
    """
    function.__annotations__.pop("self")
    return function
