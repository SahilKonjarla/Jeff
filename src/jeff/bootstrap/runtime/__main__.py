"""Preserve the private ``python -m jeff.bootstrap.runtime`` child entry point."""

from . import main

raise SystemExit(main())
