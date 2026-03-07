from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .exceptions import InvalidTimezoneError


@dataclass(frozen=True)
class QuivConfig:
    """Immutable scheduler configuration.

    Attributes:
        pool_size (int): Maximum number of worker threads.
        history_retention_seconds (int):
            How long finished job history is retained.
        timezone (str, tzinfo):
            Timezone used for display-facing datetime formatting.
    """

    pool_size: int = 10
    history_retention_seconds: int = 86400
    timezone: str | tzinfo = "UTC"


def resolve_timezone(value: str | tzinfo) -> tzinfo:
    """Resolve a timezone input into a concrete ``tzinfo`` instance.

    Args:
        value (str, tzinfo):
            Either an IANA timezone string or a ``tzinfo`` object.

    Returns:
        tzinfo: A timezone object usable for datetime conversions.

    Raises:
        InvalidTimezoneError: If the input cannot be resolved.
    """

    if isinstance(value, str):
        normalized = value.strip().upper()
        if normalized in {"UTC", "Z", "GMT"}:
            return timezone.utc
        try:
            return ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise InvalidTimezoneError(
                f"Invalid timezone '{value}'. Use a valid IANA timezone string"
                " like 'UTC' or 'America/New_York'."
            ) from exc
    if isinstance(value, tzinfo):
        return value
    raise InvalidTimezoneError(
        f"Invalid timezone type '{type(value).__name__}'. Expected str or"
        " tzinfo."
    )
