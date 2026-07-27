# Spec: `quant_momentum` — daily 5/15/30‑day momentum service

## 1. Summary

`quant_momentum` is a daily batch service that:

1. Reads daily closing prices produced by [`quant_daily_bars`](https://github.com/mayberryjp/quant_daily_bars) from the shared Postgres `market_data.daily_bars` table.
2. Computes close‑to‑close percentage momentum over **5, 15, and 30 trading‑day** lookbacks for every ticker, every day.
3. Persists each ticker's per‑day momentum (values + binary indicators) in Postgres so the data can be reused for purposes beyond momentum — e.g. detecting tickers that move together to identify **regime change**.
4. Pushes tickers that are currently indicating momentum onto the watchlist via the [`quant_signals`](https://github.com/mayberryjp/quant_signals) `POST /signals` API.

The service is a Python project, Dockerized, and run under `supervisord`, mirroring the conventions already established in `quant_daily_bars` and `quant_signals`.

## 2. Goals / Non‑Goals

**Goals**
- Compute 5/15/30‑day momentum as **% movement from day‑to‑day closing** for all active symbols daily.
- Store per‑ticker, per‑day momentum values plus a **binary momentum indicator per interval** and a **single combined binary indicator across the 3 intervals**.
- Make the stored momentum reusable (regime / co‑movement research) via a queryable table and a read API.
- Submit momentum tickers to the `quant_signals` watchlist idempotently.

**Non‑Goals (v1)**
- Ingesting bars (owned by `quant_daily_bars`) or resolving symbols (owned by `quant_symbols`).
- Portfolio construction, sizing, backtesting, or PnL.
- Intraday momentum. This is a daily, close‑based service.
- Dividend‑adjusted return series (not stored upstream by design).

## 3. Context & dependencies

```mermaid
flowchart LR
    subgraph PG["shared Postgres db: quant"]
      SM[(symbol_master.symbols)]
      DB[(market_data.daily_bars)]
      MO[(momentum.daily_momentum / momentum_runs)]
    end
    QDB[quant_daily_bars ingest] -->|writes| DB
    QM[quant_momentum compute] -->|reads closes| DB
    QM -->|reads active symbols| SM
    QM -->|writes momentum| MO
    QM -->|POST /signals| QS[quant_signals watchlist]
    API[quant_momentum read API] -->|reads| MO
```

- **Upstream — `quant_daily_bars`**: owns Postgres schema `market_data`. Relevant table `market_data.daily_bars` columns: `symbol_id` (int, logical FK → `symbol_master.symbols.id`), `ticker` (text, denormalized), `bar_date` (date), `adjustment_type` (`unadjusted` default | `split_adjusted`), `open/high/low/close` `NUMERIC(18,6)`, `volume`, `vwap`, `transactions`, `fetched_at`. Unique on `(symbol_id, bar_date, adjustment_type)`; indexed on `(ticker, bar_date)` and `(bar_date)`.
- **Symbols — `quant_symbols`**: `symbol_master.symbols(id, canonical_ticker, active)`. `symbol_id` is the stable identity; `ticker` can change on corporate actions.
- **Downstream — `quant_signals`**: Redis‑backed watchlist. `POST /signals` accepts `{source, idempotency_key, ticker, reason}` (required) plus `{market, locale, signal_type, direction, score, confidence, horizon, tags[], metadata{}}` (optional). Idempotency = `source + idempotency_key` unique for 24h; recommended key format `<source>:<date>:<ticker>`. Responses: `accepted` | `duplicate` | `unresolved`.
- **Shared DB**: single Postgres database `quant`, user `quant`; each service owns its own schema. `quant_momentum` owns a new `momentum` schema and only reads `market_data` / `symbol_master`.

## 4. Momentum definition

Let $C_t$ be the close on the as‑of trading date $t$ (the most recent available bar for a symbol in the chosen `adjustment_type` series), and $C_{t-N}$ the close $N$ **trading days** earlier (i.e. $N$ bar rows before the as‑of bar). The $N$‑day momentum is the period return expressed in **percentage points**:

$$M_N = \left(\frac{C_t}{C_{t-N}} - 1\right)\times 100$$

- Intervals: $N \in \{5, 15, 30\}$.
- "Trading days" = row offset in the ordered bar series, which naturally skips weekends/holidays. Gap handling is an open question (§13).
- Per‑interval binary indicator, with configurable threshold $\theta_N$ (percentage points, default `0`):

$$B_N = \begin{cases} 1 & M_N \ge \theta_N \\ 0 & \text{otherwise} \end{cases}$$

- Combined binary indicator $B$ (single column, "momentum across the 3 types"), governed by `MOMENTUM_RULE`:
  - `ALL` (default): $B = B_5 \wedge B_{15} \wedge B_{30}$
  - `ANY`: $B = B_5 \vee B_{15} \vee B_{30}$
  - `MAJORITY`: $B = 1$ iff at least 2 of 3 are set.

If a symbol has insufficient history for an interval ($< N+1$ bars), that interval's value is `NULL` and its flag is `false`.

## 5. Data model — new `momentum` schema

Alembic uses a **dedicated version table** `momentum.alembic_version_momentum` (mirrors `quant_daily_bars`) so it never collides with `quant_symbols`' `public.alembic_version`. All numeric momentum values are stored in **percentage points** (`3.14` = `+3.14%`).

### 5.1 `momentum.daily_momentum`

| Column | Type | Notes |
|---|---|---|
| `id` | BIGSERIAL PK | |
| `symbol_id` | INT NOT NULL | logical FK → `symbol_master.symbols.id` |
| `ticker` | TEXT NOT NULL | denormalized for query convenience |
| `bar_date` | DATE NOT NULL | as‑of date = latest close used |
| `adjustment_type` | TEXT NOT NULL DEFAULT `'unadjusted'` | which series was used |
| `close` | NUMERIC(18,6) NOT NULL | close on `bar_date` |
| `close_5d_ago` / `close_15d_ago` / `close_30d_ago` | NUMERIC(18,6) NULL | reference closes (reproducibility) |
| `momentum_5d` / `momentum_15d` / `momentum_30d` | NUMERIC(18,6) NULL | percent; NULL if insufficient history |
| `is_momentum_5d` / `is_momentum_15d` / `is_momentum_30d` | BOOLEAN NOT NULL DEFAULT false | per‑interval binary indicator |
| `is_momentum` | BOOLEAN NOT NULL DEFAULT false | **combined** binary indicator |
| `momentum_rule` | TEXT NOT NULL | `ALL` \| `ANY` \| `MAJORITY` applied |
| `threshold_5d` / `threshold_15d` / `threshold_30d` | NUMERIC(9,6) NOT NULL | thresholds applied (reproducibility) |
| `bars_available` | INT NOT NULL | trailing bars used |
| `run_id` | BIGINT NULL | FK → `momentum.momentum_runs.id` |
| `computed_at` | TIMESTAMPTZ NOT NULL | |
| `created_at` / `updated_at` | TIMESTAMPTZ NOT NULL DEFAULT now() | |

- **Unique**: `(symbol_id, bar_date, adjustment_type)` → idempotent upserts.
- **Indexes**: `(ticker, bar_date)`, `(bar_date)`, `(bar_date, is_momentum)` (fast "flagged today" queries), `(is_momentum, bar_date)`.

### 5.2 `momentum.momentum_runs`

Per‑run tracking (mirrors `market_data.vendor_bar_runs`).

| Column | Type |
|---|---|
| `id` | BIGSERIAL PK |
| `run_date` | DATE NOT NULL |
| `as_of_bar_date` | DATE NOT NULL |
| `adjustment_type` | TEXT NOT NULL |
| `momentum_rule` | TEXT NOT NULL |
| `threshold_5d` / `threshold_15d` / `threshold_30d` | NUMERIC(9,6) |
| `status` | TEXT NOT NULL DEFAULT `'running'` (`running`\|`completed`\|`failed`) |
| `symbols_requested` / `symbols_computed` / `symbols_skipped` / `symbols_failed` | INT DEFAULT 0 |
| `momentum_flagged` | INT DEFAULT 0 |
| `signals_submitted` / `signals_accepted` / `signals_duplicate` / `signals_unresolved` / `signals_failed` | INT DEFAULT 0 |
| `error_message` | TEXT |
| `duration_seconds` | FLOAT |
| `started_at` / `finished_at` | TIMESTAMPTZ |

### 5.3 `momentum.signal_submissions` (audit)

One row per `POST /signals` attempt for traceability.

| Column | Type |
|---|---|
| `id` | BIGSERIAL PK |
| `run_id` | BIGINT FK → `momentum.momentum_runs.id` |
| `symbol_id` | INT |
| `ticker` | TEXT NOT NULL |
| `bar_date` | DATE NOT NULL |
| `idempotency_key` | TEXT NOT NULL |
| `source` | TEXT NOT NULL |
| `direction` | TEXT |
| `score` | NUMERIC(9,6) |
| `status` | TEXT NOT NULL (`accepted`\|`duplicate`\|`unresolved`\|`failed`) |
| `signal_cache_id` | TEXT NULL |
| `http_status` | INT NULL |
| `error` | TEXT NULL |
| `submitted_at` | TIMESTAMPTZ DEFAULT now() |

> **Redis vs Postgres:** persistent momentum history lives in **Postgres** (needed for regime/co‑movement reuse). Redis is **optional** and only considered for a run lock / liveness heartbeat (§13, open question).

## 6. Configuration (env vars)

| Var | Default | Description |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg://quant:quant_dev_password@postgres:5432/quant` | Shared Postgres |
| `API_PORT` | `8020` | Read API port (avoid `8016` used by signals) |
| `API_LISTEN_ADDRESS` | `0.0.0.0` | |
| `MOMENTUM_INTERVAL` | `86400` | Scheduled compute interval (seconds) |
| `MOMENTUM_ADJUSTMENT_TYPE` | `unadjusted` | `unadjusted` \| `split_adjusted` |
| `MOMENTUM_LOOKBACKS` | `5,15,30` | Fixed for v1; configurable hook |
| `MOMENTUM_THRESHOLD_5D` / `_15D` / `_30D` | `0.0` | Per‑interval threshold (percentage points) |
| `MOMENTUM_RULE` | `ALL` | `ALL` \| `ANY` \| `MAJORITY` |
| `MOMENTUM_MIN_HISTORY` | `31` | Min trailing bars to attempt longest interval |
| `MOMENTUM_DIRECTION_MODE` | `long_only` | `long_only` \| `long_short` |
| `MOMENTUM_SUBMIT_ENABLED` | `true` | Toggle watchlist submission |
| `MOMENTUM_SOURCE` | `momentum-v1` | Signals `source` name |
| `MOMENTUM_HORIZON` | `30d` | Signals `horizon` |
| `MOMENTUM_SCORE_SCALE` | `30.0` | Percent mapping to `score=1.0` |
| `QUANT_SIGNALS_BASE_URL` | `http://quant_signals:8016` | Signals service base URL |
| `QUANT_SIGNALS_TIMEOUT_SECONDS` | `10` | |
| `QUANT_SIGNALS_RETRY_COUNT` | `3` | |
| `QUANT_SIGNALS_BACKOFF_SECONDS` | `0.5` | Exponential backoff base |
| `QUANT_REDIS_URL` | *(unset)* | Optional; run lock / heartbeat only |

## 7. Processing pipeline (daily run)

1. **As‑of date**: `--as-of` if given, else `MAX(bar_date)` in `market_data.daily_bars` for the configured `adjustment_type` (fallback: yesterday).
2. **Ordering guard**: verify `MAX(bar_date) >= as_of` so momentum runs *after* the day's `quant_daily_bars` incremental ingest. If bars for `as_of` are missing, log and either wait (scheduled mode) or exit non‑zero (one‑shot).
3. **Targets**: active symbols from `symbol_master.symbols WHERE active = true` (or `--tickers` subset) that have bars.
4. **Bulk read** trailing closes with a single window query, then select rows at offsets `rn ∈ {1, 6, 16, 31}` (offset 0 / 5 / 15 / 30):

   ```sql
   SELECT symbol_id, ticker, bar_date, close,
          ROW_NUMBER() OVER (PARTITION BY symbol_id ORDER BY bar_date DESC) AS rn
   FROM market_data.daily_bars
   WHERE adjustment_type = :adj
     AND bar_date <= :as_of
     AND symbol_id = ANY(:symbol_ids)
   -- keep rn <= max_lookback + 1
   ```

5. **Compute** `M_5/M_15/M_30`, per‑interval flags `B_5/B_15/B_30`, and combined `B` (§4). Record `bars_available`.
6. **Upsert** into `momentum.daily_momentum` with `ON CONFLICT (symbol_id, bar_date, adjustment_type) DO UPDATE`.
7. **Per‑symbol error isolation**: a failure for one symbol increments `symbols_failed` and continues (mirrors `quant_daily_bars`).
8. **Submission phase** (§8) for flagged symbols.
9. **Finalize** the `momentum_runs` row (status, counts, duration).

## 8. Watchlist integration (`quant_signals` producer)

For each symbol with `is_momentum = true` on the as‑of date (subject to `MOMENTUM_DIRECTION_MODE` and `MOMENTUM_SUBMIT_ENABLED`), submit:

```json
{
  "source": "momentum-v1",
  "idempotency_key": "momentum-v1:2026-07-05:AAPL",
  "ticker": "AAPL",
  "market": "stocks",
  "locale": "us",
  "signal_type": "watchlist_candidate",
  "direction": "long",
  "score": 0.62,
  "horizon": "30d",
  "reason": "Momentum across 5/15/30d: +2.9%/+7.1%/+18.6% (rule=ALL, thresholds 0/0/0)",
  "tags": ["momentum", "5d", "15d", "30d"],
  "metadata": {
    "strategy_version": "momentum-v1",
    "adjustment_type": "unadjusted",
    "bar_date": "2026-07-05",
    "close": 231.44,
    "momentum_5d": 2.9, "momentum_15d": 7.1, "momentum_30d": 18.6,
    "is_momentum_5d": true, "is_momentum_15d": true, "is_momentum_30d": true,
    "thresholds": {"5d": 0, "15d": 0, "30d": 0}, "rule": "ALL"
  }
}
```

- **Idempotency key**: `"{source}:{bar_date}:{TICKER}"` → re‑runs for the same day are safely deduplicated (`duplicate`).
- **Direction**: `long` for positive momentum. In `long_short` mode, `short` when all intervals are below the negative thresholds.
- **Score**: `clamp(mean(M_5, M_15, M_30) / MOMENTUM_SCORE_SCALE, 0, 1)`.
- **HTTP client**: timeout + exponential backoff retry on 5xx/network; classify `accepted` / `duplicate` / `unresolved`; anything else → `failed`. Record every attempt in `signal_submissions` and update run counters.

## 9. Read API (Bottle + waitress)

Mirrors `quant_daily_bars`' API style (dependency‑injectable handlers, param validation, DB URL redaction in errors).

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Liveness `{status, service}` |
| GET | `/ready` | DB readiness + schema version + latest run |
| GET | `/momentum` | List rows; filters `ticker, symbol_id, from_date, to_date, is_momentum, adjustment_type`, `limit<=500`, `offset` |
| GET | `/momentum/by-ticker/<ticker>` | Recent momentum series for a ticker |
| GET | `/momentum/latest` | Latest as‑of‑date rows (default `is_momentum=true`) |
| GET | `/momentum/date-range` | Min/max `bar_date` + counts |
| GET | `/runs` · `/runs/<id>` · `/runs/latest` | Run tracking |
| GET | `/stats` | Operational counters / last‑run summary |

## 10. CLI

```bash
python3 -m quant_momentum.cli db upgrade | verify | downgrade-base

python3 -m quant_momentum.cli momentum run \
    [--as-of YYYY-MM-DD] [--tickers AAPL,MSFT] \
    [--adjustment-type unadjusted|split_adjusted] \
    [--rule ALL|ANY|MAJORITY] [--no-submit] [--dry-run] \
    [--schedule SECONDS]         # continuous mode; one‑shot if omitted

python3 -m quant_momentum.cli momentum backfill \
    --from-date YYYY-MM-DD --to-date YYYY-MM-DD \
    [--tickers ...] [--adjustment-type ...]   # historical, no submission

python3 -m quant_momentum.cli run-summary --latest
```

Scheduled mode recomputes the as‑of date each cycle (mirrors `quant_daily_bars` `--schedule`).

## 11. Coding standards & conventions (match existing repos)

- **Python 3.12**, `src/` layout (`src/quant_momentum/…`, `where = ["src"]`), package `quant_momentum`.
- **Web**: `bottle>=0.13` served by `waitress>=3.0` (`threads=20`); `python3 -m quant_momentum.api.app`. Not FastAPI/uvicorn.
- **DB**: SQLAlchemy 2.0 core with `text()` parameterized SQL, `create_engine(url, pool_pre_ping=True)`, `psycopg[binary]>=3.1`.
- **Migrations**: Alembic with dedicated version table `momentum.alembic_version_momentum`.
- **Settings**: `pydantic>=2.7` + `pydantic-settings>=2.3`.
- **HTTP client**: `requests` (or `httpx`) for the signals producer.
- **CLI**: `argparse`, `python3 -m quant_momentum.cli <group> <cmd>`, `--schedule SECONDS`.
- **Logging**: stdlib `logging.basicConfig`, format `%(asctime)s %(levelname)s %(name)s: %(message)s`, to stderr, `PYTHONUNBUFFERED=1`.
- **Idempotency**: `INSERT … ON CONFLICT DO UPDATE`; per‑symbol error isolation.
- **Security**: parameterized SQL only; redact DB URL in error responses; never log secrets; `.env` git‑ignored; DB role scoped to read `market_data`/`symbol_master` + write `momentum`.
- **Tests**: `pytest` + `webtest` + injected `TestClient`; mock the signals HTTP API; no live DB/network needed in CI.

## 12. Containerization & supervisord

- **Dockerfile** (`python:3.12-slim`) mirrors `quant_daily_bars`: install `bash ca-certificates git vim procps`, `ARG CACHE_BUST=1`, `git clone` repo, `pip install -e ".[dev]"` + `supervisor`.
- **`entrypoint.sh`**: `alembic upgrade head` then `exec supervisord -c /app/supervisord.conf`.
- **`supervisord.conf`** programs:
  - `[program:db-migrate]` → `alembic upgrade head`, `priority=10`, `autostart=true`, `autorestart=false`, `startsecs=0`, `exitcodes=0`.
  - `[program:momentum-compute]` → `bash -c "sleep 5 && exec python3 -m quant_momentum.cli momentum run --schedule %(ENV_MOMENTUM_INTERVAL)s"`, `priority=20`, `autorestart=true`, `startretries=999`, `depends_on=db-migrate`.
  - `[program:quant-momentum-api]` → `bash -c "sleep 5 && exec python3 -m quant_momentum.api.app"`, `priority=20`, `autorestart=true`, `startretries=999`, `depends_on=db-migrate`.
  - Logs to `/dev/stdout` / `/dev/stderr` (`maxbytes=0`), `environment=PYTHONUNBUFFERED="1"`.
- **`docker-compose.yml`**: `image: quant_momentum:latest`, env `DATABASE_URL`, `QUANT_SIGNALS_BASE_URL`, `API_PORT=8020`, `MOMENTUM_INTERVAL`; `ports: "8020:8020"`; connects to shared `postgres`, `quant_signals`, and optional `redis`.

## 13. Open questions / decisions to confirm

1. **`adjustment_type` default** — `unadjusted` is always present upstream, but split events distort raw momentum. Prefer `split_adjusted` when available (requires it to be backfilled)? Proposed default: `unadjusted`, configurable.
2. **Units** — store momentum as **percentage points** (proposed) vs. fractions.
3. **Combined rule default** — `ALL` (high precision, proposed) vs. `MAJORITY`.
4. **Threshold defaults** — `0` (any positive move, proposed) vs. minimum moves (e.g. `2/5/10`).
5. **Direction** — `long_only` v1 (proposed) vs. `long_short`.
6. **Trading‑day offset vs. calendar** — use row offset (proposed); how strictly to handle mid‑series gaps / missing bars?
7. **Score normalization** — `MOMENTUM_SCORE_SCALE=30` default; confirm.
8. **Redis** — needed for a run lock / heartbeat, or is Postgres‑only sufficient for v1?
9. **Extra storage for regime research** — is period return + reference closes enough, or should we also persist the full daily return series?
10. **API port** — confirm `8020` (signals uses `8016`).

## 14. Delivery slices

Each slice is an independently shippable, testable vertical increment.

### Slice 0 — Project scaffold & tooling
Repo layout (`src/quant_momentum`), `pyproject.toml` (bottle, waitress, SQLAlchemy, psycopg[binary], alembic, requests, pydantic‑settings; dev: pytest, webtest), `.env.example`, `.gitignore`, `README.md`, `Dockerfile`, `entrypoint.sh`, `supervisord.conf`, `docker-compose.yml`, GitHub Actions workflow, settings + logging modules, CLI skeleton (`--help`).
**Acceptance**: `pip install -e ".[dev]"` succeeds; `pytest` green; `docker build` works; `python3 -m quant_momentum.cli --help` runs.

### Slice 1 — Database schema & migrations
Alembic env with dedicated version table; migration `0001` creates `momentum` schema + `daily_momentum`, `momentum_runs`, `signal_submissions` with constraints/indexes; `db upgrade|verify|downgrade-base`.
**Acceptance**: migrations apply idempotently; `db verify` passes; downgrade leaves shared schema intact.

### Slice 2 — Bars reader (upstream data access)
Module to resolve active symbols from `symbol_master.symbols` and bulk‑read trailing closes from `market_data.daily_bars` (windowed query) for a chosen `adjustment_type`.
**Acceptance**: unit tests with fixtures return correct trailing closes and handle missing/short history.

### Slice 3 — Momentum computation engine (pure logic)
Pure functions: ordered closes + lookbacks + thresholds + rule → momentum values, per‑interval flags, combined flag; percentage‑point semantics; insufficient‑history → NULL.
**Acceptance**: thorough unit tests (positive/negative/zero, exact vs insufficient bars, threshold boundaries, each rule). No DB.

### Slice 4 — Persist momentum + run tracking
Upsert into `daily_momentum`; create/finalize `momentum_runs`; per‑symbol error isolation; wire `momentum run` (submission off).
**Acceptance**: run populates rows idempotently; re‑run overwrites; run summary recorded; failures counted.

### Slice 5 — Signals watchlist producer
HTTP client to `POST /signals` with idempotency, retry/backoff, timeout, response classification; payload builder; `signal_submissions` audit + run counters; wire into `momentum run` (`MOMENTUM_SUBMIT_ENABLED`, `--no-submit`).
**Acceptance**: mocked‑API tests cover accepted/duplicate/unresolved/5xx‑retry; only flagged tickers submitted; idempotency key format verified.

### Slice 6 — Read API (Bottle + waitress)
Endpoints in §9 with param validation, error handling, DB URL redaction, injectable `TestClient`.
**Acceptance**: webtest tests cover 200/404/422 paths and filters.

### Slice 7 — Scheduling, containerization & ops
`supervisord` programs, `entrypoint.sh`, finalized `Dockerfile`, `docker-compose.yml` wired to shared Postgres/Redis + signals base URL; runbook docs; health/readiness.
**Acceptance**: `docker compose up` runs migrate → compute → api; health endpoints OK; scheduled run executes; end‑to‑end smoke posts to a signals stub.

### Slice 8 — Backfill & historical momentum (stretch)
`momentum backfill --from-date --to-date` computes momentum across a historical range for regime/co‑movement research (no submission), iterating the trading calendar.
**Acceptance**: backfill populates historical rows idempotently.
