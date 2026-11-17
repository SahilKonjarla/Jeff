"""Keep the private single-use ``python -m jeff.integrations.git.worker`` entry point."""

from . import main

raise SystemExit(main())
