"""允许 python -m brain_memory 直接运行。"""
from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
