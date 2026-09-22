from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import requests
from django.conf import settings
from django.utils import timezone

from apps.irrigation.models import GroupedRule, Site, get_curve_settings
from apps.weather.models import WeatherImportLog, WeatherObservation


OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
DEFAULT_TIMEOUT_SECONDS = 5


def _parse_timestamp(value: int | str) -> dt.datetime:
    return dt.datetime.fromtimestamp(int(value), tz=dt.timezone.utc)


def import_weather_range(
    site: Site, start_date: dt.date, end_date: dt.date,
    *, now: dt.datetime | None = None,
) -> int:
    if site.latitude is None or site.longitude is None:
        raise ValueError("Site latitude/longitude required for weather import")

    tz_name = site.timezone or settings.TIME_ZONE
    params = {
        "latitude": site.latitude,
        "longitude": site.longitude,
        "hourly": "temperature_2m,precipitation,relative_humidity_2m",
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "timezone": tz_name,
        "timeformat": "unixtime",
    }

    response = requests.get(
        OPEN_METEO_URL, params=params, timeout=DEFAULT_TIMEOUT_SECONDS
    )
    response.raise_for_status()
    payload = response.json()
    retrieved_at = now or timezone.now()

    hourly = payload.get("hourly", {})
    times = hourly.get("time", [])
    temperatures = hourly.get("temperature_2m", [])
    precipitation = hourly.get("precipitation", [])
    humidity = (
        hourly.get("relative_humidity_2m")
        or hourly.get("relativehumidity_2m") or []
    )

    # Deduplicate by the exact UTC instant stored in Postgres.
    observations_by_timestamp: dict[dt.datetime, WeatherObservation] = {}
    for idx, timestamp in enumerate(times):
        parsed_timestamp = _parse_timestamp(timestamp)
        if parsed_timestamp > retrieved_at:
            continue
        observations_by_timestamp[parsed_timestamp] = WeatherObservation(
            site=site,
            timestamp=parsed_timestamp,
            temperature_c=temperatures[idx] if idx < len(temperatures) else None,
            precipitation_mm=precipitation[idx] if idx < len(precipitation) else None,
            humidity_percent=humidity[idx] if idx < len(humidity) else None,
            retrieved_at=retrieved_at,
        )

    observations = list(observations_by_timestamp.values())

    if observations:
        WeatherObservation.objects.bulk_create(
            observations,
            update_conflicts=True,
            update_fields=[
                "temperature_c",
                "precipitation_mm",
                "humidity_percent",
                "retrieved_at",
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

    log = WeatherImportLog.objects.filter(site=site).order_by("-imported_at").first()
    if log and now - log.imported_at < dt.timedelta(minutes=min_retry_minutes):
        return 0

    latest_success = (
        WeatherImportLog.objects.filter(site=site, last_success_at__isnull=False)
        .order_by("-last_success_at")
        .values_list("last_success_at", flat=True).first()
    )
    if latest_success and now - latest_success < dt.timedelta(hours=max_age_hours):
        return 0

    coverage_days = 1
    if GroupedRule.objects.filter(
        schedule__site=site, enabled=True, mode="SMART"
    ).exists():
        coverage_days = get_curve_settings(site).coverage_days
    # Initial backfill populates the archive. Routine refreshes update only the
    # temperature/Smart window; gaps do not trigger extra imports.
    days = max(1, coverage_days)
    if latest_success is None:
        days = max(days, lookback_days)
    start_date = local_now.date() - dt.timedelta(days=days)
    end_date = local_now.date()

    status = WeatherImportLog.STATUS_SUCCESS
    error_message = ""
    count = 0
    try:
        count = import_weather_range(site, start_date, end_date, now=now)
    except Exception as exc:  # noqa: BLE001 - keep failures non-fatal
        status = WeatherImportLog.STATUS_FAILED
        error_message = str(exc)

    log, _ = WeatherImportLog.objects.update_or_create(
        site=site,
        date=local_now.date(),
        defaults={"status": status, "error_message": error_message},
    )
    timestamps = {"imported_at": now}
    if status == WeatherImportLog.STATUS_SUCCESS:
        timestamps["last_success_at"] = now
    WeatherImportLog.objects.filter(id=log.id).update(**timestamps)
    return count
