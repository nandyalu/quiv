from __future__ import annotations

from datetime import timezone

import pytest

from quiv.config import QuivConfig, resolve_timezone
from quiv.exceptions import InvalidTimezoneError


def test_resolve_timezone_from_iana_string() -> None:
    tz = resolve_timezone("UTC")
    assert str(tz) == "UTC"


def test_resolve_timezone_from_tzinfo_instance() -> None:
    tz = resolve_timezone(timezone.utc)
    assert tz is timezone.utc


def test_resolve_timezone_invalid_string_raises() -> None:
    with pytest.raises(InvalidTimezoneError):
        resolve_timezone("Not/A-Real-TZ")


def test_config_process_pool_defaults() -> None:
    config = QuivConfig()
    assert config.process_pool_size == 0
    assert config.process_kill_grace == 5.0
    assert QuivConfig(process_pool_size=3).process_pool_size == 3
