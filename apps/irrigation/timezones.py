from __future__ import annotations

from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones


@lru_cache(maxsize=1)
def site_timezone_choices() -> list[tuple[str, str]]:
    return [(name, name) for name in sorted(available_timezones())]


def is_valid_timezone_name(value: str) -> bool:
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError:
        return False
    return True
