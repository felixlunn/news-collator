"""Allows `python -m news_collator …`."""

import sys

from .cli import main

sys.exit(main())
