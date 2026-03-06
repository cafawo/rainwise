from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import requests
from django.conf import settings
from django.utils import timezone

from apps.irrigation.models import Site
from apps.weather.models import WeatherImportLog, WeatherObservation


OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
DEFAULT_TIMEOUT_SECONDS = 5


def _parse_timestamp(value: str, tz: ZoneInfo) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed


def import_weather_range(
    site: Site, start_date: dt.date, end_date: dt.date
) -> int:
    if site.latitude is None or site.longitude is None:
        raise ValueError("Site latitude/longitude required for weather import")

    tz_name = site.timezone or settings.TIME_ZONE
    tz = ZoneInfo(tz_name)

    params = {
        "latitude": site.latitude,
        "longitude": site.longitude,
        "hourly": "temperature_2m,precipitation,relative_humidity_2m",
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
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


def import_yesterday_weather(site: Site, target_date: dt.date | None = None) -> int:
    tz_name = site.timezone or settings.TIME_ZONE
    tz = ZoneInfo(tz_name)

    if target_date is None:
        local_today = dt.datetime.now(tz=tz).date()
        target_date = local_today - dt.timedelta(days=1)

    return import_weather_range(site, target_date, target_date)


def ensure_recent_weather(
    site: Site,
    now: dt.datetime | None = None,
    max_age_hours: int = 6,
    lookback_days: int = 2,
    min_retry_minutes: int = 60,
) -> int:
    if site.latitude is None or site.longitude is None:
        return 0

    if now is None:
        now = timezone.now()

    tz = ZoneInfo(site.timezone or settings.TIME_ZONE)
    local_now = timezone.localtime(now, tz)

    last_obs = (
        WeatherObservation.objects.filter(site=site)
        .order_by("-timestamp")
        .first()
    )
    if last_obs:
        last_time = timezone.localtime(last_obs.timestamp, tz)
        if local_now - last_time < dt.timedelta(hours=max_age_hours):
            return 0

    log = WeatherImportLog.objects.filter(site=site, date=local_now.date()).first()
    if log and local_now - log.imported_at < dt.timedelta(minutes=min_retry_minutes):
        return 0

    if last_obs:
        start_date = (last_time - dt.timedelta(hours=1)).date()
    else:
        start_date = local_now.date() - dt.timedelta(days=lookback_days)

    end_date = local_now.date()
    if start_date > end_date:
        start_date = end_date

    status = WeatherImportLog.STATUS_SUCCESS
    error_message = ""
    count = 0
    try:
        count = import_weather_range(site, start_date, end_date)
    except Exception as exc:  # noqa: BLE001 - keep failures non-fatal
        status = WeatherImportLog.STATUS_FAILED
        error_message = str(exc)

    log, _ = WeatherImportLog.objects.update_or_create(
        site=site,
        date=local_now.date(),
        defaults={"status": status, "error_message": error_message},
    )
    WeatherImportLog.objects.filter(id=log.id).update(imported_at=timezone.now())
    return count
