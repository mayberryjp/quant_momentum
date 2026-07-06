"""Centralized logging configuration (spec §11).

Emits ``%(asctime)s %(levelname)s %(name)s: %(message)s`` to stderr. Call
:func:`configure_logging` once at process start (CLI / API entry points).
"""

from __future__ import annotations

import logging
import sys

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure_logging(level: int = logging.INFO) -> None:
    """Configure root logging to stderr with the shared format."""
    logging.basicConfig(level=level, format=LOG_FORMAT, stream=sys.stderr, force=True)
