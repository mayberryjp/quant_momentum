"""quant_momentum — daily 5/15/30-day close-to-close momentum service.

Reads daily bars produced by ``quant_daily_bars``, computes momentum over
5/15/30 trading-day lookbacks (plus rolling 30-day daily-change statistics),
persists the results to Postgres, and submits flagged tickers to the
``quant_signals`` watchlist.
"""

__version__ = "0.1.0"
