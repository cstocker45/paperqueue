"""`python -m paperqueue` (used by the launchd service)."""

import sys

from .cli import main

sys.exit(main())
