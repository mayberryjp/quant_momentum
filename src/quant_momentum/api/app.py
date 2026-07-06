"""Bottle API application for quant_momentum (served by waitress).

Scaffold slice: exposes ``/health`` only via a dependency-injectable
:func:`create_app` factory. Readiness and momentum/read endpoints are added in
later slices. Run with ``python3 -m quant_momentum.api.app``.
"""

from __future__ import annotations

import logging
import os
import sys

from bottle import Bottle

from quant_momentum.logging_config import configure_logging

SERVICE_NAME = "quant-momentum-api"

log = logging.getLogger(SERVICE_NAME)


def create_app() -> Bottle:
    """Construct the Bottle application.

    Handlers are wired here so tests can drive the app with an injected
    ``webtest.TestApp`` client without a live server.
    """
    api = Bottle()

    @api.get("/health")
    def health() -> dict:
        return {"status": "ok", "service": SERVICE_NAME}

    return api


configure_logging()

print(
    f"[{SERVICE_NAME}] module={__file__} python={sys.executable} "
    f"version={sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
    file=sys.stderr,
    flush=True,
)

app = create_app()


if __name__ == "__main__":
    from waitress import serve

    host = os.environ.get("API_LISTEN_ADDRESS", "0.0.0.0")
    port = int(os.environ.get("API_PORT", "8020"))
    log.info("Starting %s on %s:%d ...", SERVICE_NAME, host, port)
    serve(app, host=host, port=port, threads=20)
