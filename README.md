# quant_momentum

Daily batch service that computes **5 / 15 / 30 trading-day close-to-close momentum**
(plus rolling 30-day daily-change statistics) for every active symbol, persists the
results to Postgres for reuse (regime / co-movement research), and submits flagged
tickers to the [`quant_signals`](https://github.com/mayberryjp/quant_signals) watchlist.

It reads daily bars produced by [`quant_daily_bars`](https://github.com/mayberryjp/quant_daily_bars)
from the shared Postgres `market_data.daily_bars` table and owns a dedicated `momentum` schema.

> Built in vertical slices — see the [spec](https://github.com/mayberryjp/quant_momentum/issues/1).
> This is **slice 0** (project scaffold & tooling).

## Momentum definition

For close $C_t$ on the as-of date and $C_{t-N}$ $N$ trading days earlier:

$$M_N = \left(\frac{C_t}{C_{t-N}} - 1\right) \times 100$$

Values are stored in **percentage points** (`3.14` = `+3.14%`) for $N \in \{5, 15, 30\}$.
Per-interval binary flags use configurable thresholds; a combined flag is derived via
`MOMENTUM_RULE` (`ALL` | `ANY` | `MAJORITY`). Each row also stores rolling 30-day
average / median / min / max daily % change and the floor / ceiling close price.

## Layout

```
src/quant_momentum/      # package (config, logging, CLI, api/)
tests/                   # pytest suite (no live DB/network needed)
alembic/                 # migrations (added in slice 1)
Dockerfile               # python:3.12-slim image
supervisord.conf         # db-migrate + momentum-compute + api programs
docker-compose.yml       # wires into the shared quant stack
```

## Development

```bash
python3 -m pip install -e ".[dev]"   # install with dev extras
pytest                                # run tests
python3 -m quant_momentum.cli --help  # explore the CLI
```

## Configuration

All configuration is via environment variables in [`docker-compose.yml`](docker-compose.yml).
Key knobs: `DATABASE_URL`, `API_PORT` (default `8020`), `MOMENTUM_RULE`,
`MOMENTUM_ADJUSTMENT_TYPE`, `MOMENTUM_THRESHOLD_{5,15,30}D`, `QUANT_SIGNALS_BASE_URL`,
and optional `QUANT_REDIS_URL` (run lock / heartbeat only).

## CLI

```bash
python3 -m quant_momentum.cli db upgrade | verify | downgrade-base
python3 -m quant_momentum.cli momentum run [--as-of YYYY-MM-DD] [--tickers AAPL,MSFT] \
    [--adjustment-type unadjusted|split_adjusted] [--rule ALL|ANY|MAJORITY] \
    [--no-submit] [--dry-run] [--schedule SECONDS] [--at HH:MM] [--timezone IANA_TZ]
python3 -m quant_momentum.cli momentum backfill --from-date YYYY-MM-DD --to-date YYYY-MM-DD
python3 -m quant_momentum.cli run-summary --latest
```

Set `MOMENTUM_RUN_AT` (e.g. `21:30`) and `MOMENTUM_TIMEZONE` (e.g. `America/New_York`)
to run once per day at a fixed wall-clock time, with DST handled by the timezone
database. When set it takes precedence over the drifting `MOMENTUM_INTERVAL`
(`--schedule`) loop; `--at` / `--timezone` override the environment.

## Docker

```bash
docker build -t quant_momentum:latest .
docker compose up
```

## Operations & runbook

Under `supervisord` the container runs three programs (see [supervisord.conf](supervisord.conf)):

1. `db-migrate` (priority 10) — `alembic upgrade head`, runs once.
2. `momentum-compute` (priority 20) — `momentum run --schedule $MOMENTUM_INTERVAL`,
   or a daily run at `$MOMENTUM_RUN_AT` in `$MOMENTUM_TIMEZONE` when that is set.
3. `quant-momentum-api` (priority 20) — the read API on `API_PORT` (8020).

`docker compose up` brings up migrate → compute → API against the shared `quant`
stack. The API `/health` endpoint backs the compose healthcheck; `/ready` reports
DB connectivity, schema version, and the latest run.

**Scheduling & the run lock.** Scheduled mode recomputes the as-of date each cycle.
Set `QUANT_REDIS_URL` to enable an optional cross-instance run lock so overlapping
runs are skipped; if unset, the lock is a no-op (single-instance default).

**Local end-to-end smoke.**

```bash
python3 scripts/signals_stub.py &                       # fake quant_signals on :8016
QUANT_SIGNALS_BASE_URL=http://localhost:8016 \
  python3 -m quant_momentum.cli momentum run --as-of 2026-07-06
python3 -m quant_momentum.cli run-summary --latest      # inspect the run
```

**Common tasks.**

| Task | Command |
|---|---|
| Apply migrations | `python3 -m quant_momentum.cli db upgrade` |
| Verify schema at head | `python3 -m quant_momentum.cli db verify` |
| One-shot compute (no submit) | `python3 -m quant_momentum.cli momentum run --no-submit` |
| Latest run summary | `python3 -m quant_momentum.cli run-summary --latest` |
| Readiness probe | `curl localhost:8020/ready` |

