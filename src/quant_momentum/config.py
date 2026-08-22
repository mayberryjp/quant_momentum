"""Application configuration loaded from environment variables (spec §6).

Uses ``pydantic-settings`` so every knob is documented, typed, and validated
in one place. Environment variable names are the canonical aliases; field
names may also be used programmatically (e.g. in tests).
"""

from __future__ import annotations

from datetime import time
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ADJUSTMENT_TYPES = ("unadjusted", "split_adjusted")
MOMENTUM_RULES = ("ALL", "ANY", "MAJORITY")
DIRECTION_MODES = ("long_only", "long_short")


def parse_run_at(value: str) -> time:
    """Parse a ``HH:MM`` or ``HH:MM:SS`` wall-clock time (24-hour)."""
    parts = value.strip().split(":")
    if len(parts) not in (2, 3) or not all(part.isdigit() for part in parts):
        raise ValueError(f"Run time must be HH:MM or HH:MM:SS, got {value!r}")
    hour, minute, *rest = (int(part) for part in parts)
    second = rest[0] if rest else 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
        raise ValueError(f"Run time out of range: {value!r}")
    return time(hour, minute, second)


class Settings(BaseSettings):
    """Runtime configuration for the momentum service."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    # -- Database ------------------------------------------------------
    database_url: str = Field(
        default="postgresql+psycopg://quant:quant_dev_password@postgres:5432/quant",
        alias="DATABASE_URL",
    )

    # -- Read API ------------------------------------------------------
    api_port: int = Field(default=8020, alias="API_PORT")
    api_listen_address: str = Field(default="0.0.0.0", alias="API_LISTEN_ADDRESS")

    # -- Momentum compute ---------------------------------------------
    momentum_interval: int = Field(default=86400, alias="MOMENTUM_INTERVAL")
    momentum_run_at: str = Field(default="", alias="MOMENTUM_RUN_AT")
    momentum_timezone: str = Field(default="UTC", alias="MOMENTUM_TIMEZONE")
    momentum_adjustment_type: str = Field(default="unadjusted", alias="MOMENTUM_ADJUSTMENT_TYPE")
    momentum_lookbacks: str = Field(default="5,15,30", alias="MOMENTUM_LOOKBACKS")
    momentum_threshold_5d: float = Field(default=0.0, alias="MOMENTUM_THRESHOLD_5D")
    momentum_threshold_15d: float = Field(default=0.0, alias="MOMENTUM_THRESHOLD_15D")
    momentum_threshold_30d: float = Field(default=0.0, alias="MOMENTUM_THRESHOLD_30D")
    momentum_threshold_5_15d: float = Field(default=0.0, alias="MOMENTUM_THRESHOLD_5_15D")
    momentum_threshold_15_30d: float = Field(default=0.0, alias="MOMENTUM_THRESHOLD_15_30D")
    momentum_rule: str = Field(default="ALL", alias="MOMENTUM_RULE")
    momentum_min_history: int = Field(default=31, alias="MOMENTUM_MIN_HISTORY")
    momentum_direction_mode: str = Field(default="long_only", alias="MOMENTUM_DIRECTION_MODE")
    daily_change_retention_days: int = Field(default=90, alias="DAILY_CHANGE_RETENTION_DAYS")

    # -- Watchlist submission -----------------------------------------
    momentum_submit_enabled: bool = Field(default=True, alias="MOMENTUM_SUBMIT_ENABLED")
    momentum_source: str = Field(default="momentum-v1", alias="MOMENTUM_SOURCE")
    momentum_horizon: str = Field(default="30d", alias="MOMENTUM_HORIZON")
    momentum_score_scale: float = Field(default=30.0, alias="MOMENTUM_SCORE_SCALE")

    # -- quant_signals client -----------------------------------------
    quant_signals_base_url: str = Field(default="http://quant_signals:8016", alias="QUANT_SIGNALS_BASE_URL")
    quant_signals_timeout_seconds: float = Field(default=10.0, alias="QUANT_SIGNALS_TIMEOUT_SECONDS")
    quant_signals_retry_count: int = Field(default=3, alias="QUANT_SIGNALS_RETRY_COUNT")
    quant_signals_backoff_seconds: float = Field(default=0.5, alias="QUANT_SIGNALS_BACKOFF_SECONDS")

    # -- Optional Redis (run lock / heartbeat only) -------------------
    quant_redis_url: str | None = Field(default=None, alias="QUANT_REDIS_URL")

    @field_validator("momentum_run_at")
    @classmethod
    def _validate_run_at(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return ""
        parse_run_at(value)
        return value

    @field_validator("momentum_timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        value = value.strip() or "UTC"
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"MOMENTUM_TIMEZONE is not a valid IANA timezone: {value!r}") from exc
        return value

    @field_validator("momentum_adjustment_type")
    @classmethod
    def _validate_adjustment_type(cls, value: str) -> str:
        if value not in ADJUSTMENT_TYPES:
            raise ValueError(f"MOMENTUM_ADJUSTMENT_TYPE must be one of {ADJUSTMENT_TYPES}")
        return value

    @field_validator("momentum_rule")
    @classmethod
    def _validate_rule(cls, value: str) -> str:
        upper = value.upper()
        if upper not in MOMENTUM_RULES:
            raise ValueError(f"MOMENTUM_RULE must be one of {MOMENTUM_RULES}")
        return upper

    @field_validator("momentum_direction_mode")
    @classmethod
    def _validate_direction_mode(cls, value: str) -> str:
        if value not in DIRECTION_MODES:
            raise ValueError(f"MOMENTUM_DIRECTION_MODE must be one of {DIRECTION_MODES}")
        return value

    @field_validator("daily_change_retention_days")
    @classmethod
    def _validate_retention_days(cls, value: int) -> int:
        if value < 1:
            raise ValueError("DAILY_CHANGE_RETENTION_DAYS must be >= 1")
        return value

    @property
    def run_at(self) -> time | None:
        """Parsed ``MOMENTUM_RUN_AT`` wall-clock time, or ``None`` when unset."""
        return parse_run_at(self.momentum_run_at) if self.momentum_run_at else None

    @property
    def timezone(self) -> ZoneInfo:
        """Parsed ``MOMENTUM_TIMEZONE``."""
        return ZoneInfo(self.momentum_timezone)

    @property
    def lookbacks(self) -> list[int]:
        """Parsed ``MOMENTUM_LOOKBACKS`` as ordered ints (e.g. ``[5, 15, 30]``)."""
        return [int(part.strip()) for part in self.momentum_lookbacks.split(",") if part.strip()]

    @property
    def thresholds(self) -> dict[int, float]:
        """Per-interval thresholds keyed by lookback (percentage points)."""
        return {
            5: self.momentum_threshold_5d,
            15: self.momentum_threshold_15d,
            30: self.momentum_threshold_30d,
        }

    @property
    def segment_thresholds(self) -> dict[tuple[int, int], float]:
        """Per-segment thresholds keyed by (near, far) offset (percentage points)."""
        return {
            (5, 15): self.momentum_threshold_5_15d,
            (15, 30): self.momentum_threshold_15_30d,
        }


@lru_cache
def get_settings() -> Settings:
    """Return a cached :class:`Settings` instance."""
    return Settings()
