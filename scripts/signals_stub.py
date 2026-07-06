"""Minimal quant_signals stub for local end-to-end smoke testing (spec §14, slice 7).

Accepts ``POST /signals`` and returns ``accepted`` (or ``duplicate`` for a
repeated ``source + idempotency_key`` within the process). Not for production.

Run: ``python3 scripts/signals_stub.py`` (listens on 8016 by default), then point
``QUANT_SIGNALS_BASE_URL`` at it and run ``momentum run``.
"""

from __future__ import annotations

import os

from bottle import Bottle, request, response

app = Bottle()
_seen: set[tuple[str, str]] = set()


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "signals-stub"}


@app.post("/signals")
def signals() -> dict:
    payload = request.json or {}
    key = (payload.get("source", ""), payload.get("idempotency_key", ""))
    if not payload.get("ticker") or not payload.get("idempotency_key"):
        response.status = 422
        return {"status": "unresolved", "error": "missing ticker/idempotency_key"}
    status = "duplicate" if key in _seen else "accepted"
    _seen.add(key)
    return {"status": status, "signal_cache_id": f"stub-{abs(hash(key))}"}


if __name__ == "__main__":
    from waitress import serve

    serve(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8016")), threads=4)
