"""``python3 -m quant_momentum.cli`` entry point.

Thin wrapper that re-exports the implementation from
:mod:`quant_momentum._cli_impl` (mirrors the ``quant_daily_bars`` convention).
"""

from __future__ import annotations

import sys

from quant_momentum._cli_impl import *  # noqa: F401,F403
from quant_momentum._cli_impl import main

if __name__ == "__main__":
    sys.exit(main())
