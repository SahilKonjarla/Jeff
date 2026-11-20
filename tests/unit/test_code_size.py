"""Code-size accounting excludes documentation and multiline literal payloads."""

from collections.abc import Callable

import pytest


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("", 0),
        ("\n# comment\n    # indented comment\n", 0),
        ("value = 1  # inline comment\n", 1),
        ('"module documentation"\n', 0),
        ('("module documentation")\n', 0),
        ('(\n    "module documentation"\n)\n', 0),
        ('"module documentation"; value = 1\n', 1),
        (
            '"""Module documentation.\nMore documentation.\n"""\n'
            'class Example:\n    "class documentation"\n'
            '    def method(self):\n        "method documentation"\n        return 1\n',
            3,
        ),
        ('async def example():\n    "documentation"\n    return 1\n', 2),
        ('text = """\nmultiline payload\n# still payload\n"""\n', 1),
        ('emit(\n    """\n    payload\n    """\n)\n', 2),
        ('text = """\npayload\n""".strip()\n', 2),
        ('text = "# not a comment"\n', 1),
        ("values = (\n    1,\n    2,\n)\n", 4),
        ('text = f"""\npayload {call()}\n"""\n', 1),
        ('text = b"""\nbytes payload\n"""\n', 1),
        ('é = """\npayload\n"""; other = 1\n', 2),
        ('text = (\n    "first"\n    "second"\n)\n', 2),
        ('text = """one line"""\n', 1),
        ('value = 1\n"ordinary non-docstring expression"\n', 2),
        ('"""Documentation\n# not code\n"""\n\n# comment\n' + "pass\n" * 400, 400),
        ("pass\n" * 401, 401),
    ],
)
def test_code_line_count(code_line_count: Callable[[str], int], source: str, expected: int) -> None:
    assert code_line_count(source) == expected
