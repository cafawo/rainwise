from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import requests
from django.conf import settings

from apps.irrigation.models import Site
from apps.weather.models import WeatherObservation


OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
DEFAULT_TIMEOUT_SECONDS = 5


def _parse_timestamp(value: str, tz: ZoneInfo) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed


def import_yesterday_weather(site: Site, target_date: dt.date | None = None) -> int:
    if site.latitude is None or site.longitude is None:
        raise ValueError("Site latitude/longitude required for weather import")

    tz_name = site.timezone or settings.TIME_ZONE
    tz = ZoneInfo(tz_name)

    if target_date is None:
        local_today = dt.datetime.now(tz=tz).date()
        target_date = local_today - dt.timedelta(days=1)

    params = {
        "latitude": site.latitude,
        "longitude": site.longitude,
        "hourly": "temperature_2m,precipitation,relative_humidity_2m",
        "start_date": target_date.isoformat(),
        "end_date": target_date.isoformat(),
        "timezone": tz_name,
    }

    response = requests.get(OPEN_METEO_URL, params=params, timeout=DEFAULT_TIMEOUT_SECONDS)
    response.raise_for_status()
    payload = response.json()

    hourly = payload.get("hourly", {})
    times = hourly.get("time", [])
    temperatures = hourly.get("temperature_2m", [])
    precipitation = hourly.get("precipitation", [])
    humidity = hourly.get("relative_humidity_2m") or hourly.get("relativehumidity_2m") or []

    observations: list[WeatherObservation] = []
    for idx, timestamp in enumerate(times):
        observations.append(
            WeatherObservation(
                site=site,
                timestamp=_parse_timestamp(timestamp, tz),
                temperature_c=temperatures[idx] if idx < len(temperatures) else None,
                precipitation_mm=precipitation[idx] if idx < len(precipitation) else None,
                humidity_percent=humidity[idx] if idx < len(humidity) else None,
            )
        )

    if observations:
        WeatherObservation.objects.bulk_create(
            observations,
            update_conflicts=True,
            update_fields=[
                "temperature_c",
                "precipitation_mm",
                "humidity_percent",
            ],
            unique_fields=["site", "timestamp"],
        )

    return len(observations)
