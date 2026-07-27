"""Tests for the configuration layer."""

from __future__ import annotations

import pytest

from quant_momentum.config import Settings


def test_defaults() -> None:
    settings = Settings()
    assert settings.api_port == 8020
    assert settings.api_listen_address == "0.0.0.0"
    assert settings.momentum_rule == "ALL"
    assert settings.momentum_adjustment_type == "unadjusted"
    assert settings.momentum_direction_mode == "long_only"
    assert settings.momentum_submit_enabled is True
    assert settings.momentum_score_scale == 30.0
    assert settings.daily_change_retention_days == 90
    assert settings.lookbacks == [5, 15, 30]
    assert settings.thresholds == {5: 0.0, 15: 0.0, 30: 0.0}
    assert settings.quant_redis_url is None


def test_rule_is_normalized_uppercase() -> None:
    assert Settings(momentum_rule="any").momentum_rule == "ANY"
    assert Settings(momentum_rule="majority").momentum_rule == "MAJORITY"


@pytest.mark.parametrize(
    "field,value",
    [
        ("momentum_rule", "sometimes"),
        ("momentum_adjustment_type", "dividend_adjusted"),
        ("momentum_direction_mode", "sideways"),
        ("daily_change_retention_days", 0),
    ],
)
def test_invalid_values_rejected(field: str, value: object) -> None:
    with pytest.raises(Exception):
        Settings(**{field: value})


def test_custom_lookbacks_parsed() -> None:
    assert Settings(momentum_lookbacks="10, 20 ,40").lookbacks == [10, 20, 40]
