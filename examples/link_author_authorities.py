#!/usr/bin/env python3
"""Thin runner for link_author_authorities (package lives alongside this file)."""

import asyncio
import sys
from pathlib import Path

_pkg = Path(__file__).resolve().parent / "link_author_authorities"
if str(_pkg) not in sys.path:
    sys.path.insert(0, str(_pkg))

from main import main

if __name__ == "__main__":
    asyncio.run(main())
