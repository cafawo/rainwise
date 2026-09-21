from __future__ import annotations

import datetime as dt
from unittest import mock

from django.test import TestCase

from apps.irrigation.models import CurveSettings, GroupedRule, Schedule, Site
from apps.weather.models import WeatherImportLog, WeatherObservation
from apps.weather.services import (
    ensure_recent_weather,
    import_weather_range,
    import_yesterday_weather,
)


def _utc_timestamp(
    year: int, month: int, day: int, hour: int, minute: int = 0
) -> int:
    return int(
        dt.datetime(
            year, month, day, hour, minute, tzinfo=dt.timezone.utc
        ).timestamp()
    )


class WeatherImportTests(TestCase):
    def test_future_hours_are_not_imported_and_legacy_provenance_is_repaired(self):
        site = Site.objects.create(name="Home", latitude=52.5, longitude=13.4, timezone="UTC")
        now = dt.datetime(2026, 7, 8, 6, 30, tzinfo=dt.timezone.utc)
        timestamp = now.replace(minute=0)
        WeatherObservation.objects.create(site=site, timestamp=timestamp, temperature_c=40)
        response = mock.Mock()
        response.json.return_value = {"hourly": {
            "time": [int(timestamp.timestamp()), int((timestamp + dt.timedelta(hours=1)).timestamp())],
            "temperature_2m": [20, 21], "precipitation": [0.2, 9],
        }}
        with mock.patch("apps.weather.services.requests.get", return_value=response):
            count = import_weather_range(site, now.date(), now.date(), now=now)
        self.assertEqual(count, 1)
        row = WeatherObservation.objects.get(site=site)
        self.assertEqual(row.temperature_c, 20)
        self.assertEqual(row.retrieved_at, now)

    def test_full_seven_day_backfill_despite_newer_rows_and_retry_throttle(self):
        site = Site.objects.create(name="Home", latitude=52.5, longitude=13.4, timezone="UTC")
        settings = CurveSettings.objects.create(site=site, coverage_days=7, fallback_temperature_c=20)
        schedule = Schedule.objects.create(site=site, name="Summer")
        GroupedRule.objects.create(schedule=schedule, mode="SMART", start_time="06:30")
        now = dt.datetime(2026, 7, 8, 6, 30, tzinfo=dt.timezone.utc)
        WeatherObservation.objects.create(
            site=site, timestamp=now.replace(minute=0), retrieved_at=now,
            temperature_c=20, precipitation_mm=0,
        )
        with mock.patch("apps.weather.services.import_weather_range", return_value=168) as imported:
            ensure_recent_weather(site, now=now, lookback_days=2)
            imported.assert_called_once_with(site, dt.date(2026, 7, 1), dt.date(2026, 7, 8), now=now)
            ensure_recent_weather(site, now=now + dt.timedelta(minutes=10), lookback_days=2)
            self.assertEqual(imported.call_count, 1)
        settings.coverage_days = 2
        settings.save()
        later = now + dt.timedelta(hours=1)
        with mock.patch("apps.weather.services.import_weather_range", return_value=48) as imported:
            ensure_recent_weather(site, now=later, lookback_days=2)
            self.assertEqual(imported.call_args.args[1], dt.date(2026, 7, 6))
        settings.coverage_days = 7
        settings.save()
        later += dt.timedelta(hours=1)
        with mock.patch("apps.weather.services.import_weather_range", return_value=168) as imported:
            ensure_recent_weather(site, now=later, lookback_days=2)
            self.assertEqual(imported.call_args.args[1], dt.date(2026, 7, 1))

    def test_success_time_and_interior_gap_control_refresh_not_future_observation(self):
        site = Site.objects.create(name="Home", latitude=52.5, longitude=13.4, timezone="UTC")
        now = dt.datetime(2026, 7, 8, 6, 30, tzinfo=dt.timezone.utc)
        start = dt.datetime(2026, 7, 7, tzinfo=dt.timezone.utc)
        rows = []
        instant = start
        while instant <= now:
            rows.append(WeatherObservation(site=site, timestamp=instant, retrieved_at=now, temperature_c=20, precipitation_mm=0))
            instant += dt.timedelta(hours=1)
        WeatherObservation.objects.bulk_create(rows)
        log = WeatherImportLog.objects.create(site=site, date=now.date(), status="SUCCESS", last_success_at=now - dt.timedelta(hours=2))
        WeatherImportLog.objects.filter(pk=log.pk).update(imported_at=now - dt.timedelta(hours=2))
        with mock.patch("apps.weather.services.import_weather_range", return_value=30) as imported:
            ensure_recent_weather(site, now=now, lookback_days=1)
            imported.assert_not_called()
            ensure_recent_weather(site, now=now + dt.timedelta(hours=1), lookback_days=1)
            imported.assert_not_called()
            WeatherObservation.objects.filter(timestamp=start + dt.timedelta(hours=4)).update(retrieved_at=None)
            ensure_recent_weather(site, now=now, lookback_days=1)
            imported.assert_called_once()
        WeatherObservation.objects.create(site=site, timestamp=now + dt.timedelta(days=1), temperature_c=50)
        later = now + dt.timedelta(hours=6)
        with mock.patch("apps.weather.services.import_weather_range", side_effect=RuntimeError("Offline")) as imported:
            ensure_recent_weather(site, now=later, lookback_days=1)
            ensure_recent_weather(site, now=later + dt.timedelta(minutes=30), lookback_days=1)
            self.assertEqual(imported.call_count, 1)
        log.refresh_from_db()
        self.assertEqual(log.status, "FAILED")
        self.assertEqual(log.last_success_at, now)

    def test_import_yesterday_weather(self) -> None:
        site = Site.objects.create(
            name="Home", latitude=52.5, longitude=13.4, timezone="UTC"
        )
        payload = {
            "hourly": {
                "time": [
                    _utc_timestamp(2024, 1, 1, 0),
                    _utc_timestamp(2024, 1, 1, 1),
                ],
                "temperature_2m": [1.0, 2.0],
                "precipitation": [0.1, 0.0],
                "relative_humidity_2m": [80, 81],
            }
        }

        response = mock.Mock()
        response.json.return_value = payload
        response.raise_for_status.return_value = None

        with mock.patch("apps.weather.services.requests.get", return_value=response):
            count = import_yesterday_weather(site, target_date=dt.date(2024, 1, 1))

        self.assertEqual(count, 2)
        self.assertEqual(WeatherObservation.objects.count(), 2)

    def test_import_weather_range_uses_unixtime_on_dst_boundary(self) -> None:
        site = Site.objects.create(
            name="Home", latitude=52.5, longitude=13.4, timezone="Europe/Berlin"
        )
        payload = {
            "hourly": {
                "time": [
                    _utc_timestamp(2026, 3, 28, 22),
                    _utc_timestamp(2026, 3, 28, 23),
                    _utc_timestamp(2026, 3, 29, 0),
                    _utc_timestamp(2026, 3, 29, 1),
                ],
                "temperature_2m": [5.0, 4.9, 4.7, 4.6],
                "precipitation": [0.0, 0.0, 0.1, 0.2],
                "relative_humidity_2m": [80, 81, 82, 83],
            }
        }

        response = mock.Mock()
        response.json.return_value = payload
        response.raise_for_status.return_value = None

        with mock.patch(
            "apps.weather.services.requests.get", return_value=response
        ) as mocked_get:
            count = import_weather_range(
                site,
                start_date=dt.date(2026, 3, 29),
                end_date=dt.date(2026, 3, 29),
            )

        self.assertEqual(count, 4)
        self.assertEqual(WeatherObservation.objects.count(), 4)
        timestamps = list(
            WeatherObservation.objects.filter(site=site)
            .order_by("timestamp")
            .values_list("timestamp", flat=True)
        )
        self.assertEqual(
            timestamps,
            [
                dt.datetime(2026, 3, 28, 22, 0, tzinfo=dt.timezone.utc),
                dt.datetime(2026, 3, 28, 23, 0, tzinfo=dt.timezone.utc),
                dt.datetime(2026, 3, 29, 0, 0, tzinfo=dt.timezone.utc),
                dt.datetime(2026, 3, 29, 1, 0, tzinfo=dt.timezone.utc),
            ],
        )
        self.assertEqual(mocked_get.call_args.kwargs["params"]["timeformat"], "unixtime")

    def test_ensure_recent_weather_throttles_when_recent(self) -> None:
        site = Site.objects.create(
            name="Home", latitude=52.5, longitude=13.4, timezone="UTC"
        )
        payload = {
            "hourly": {
                "time": [
                    _utc_timestamp(2024, 1, 2, 10),
                    _utc_timestamp(2024, 1, 2, 11),
                ],
                "temperature_2m": [1.0, 2.0],
                "precipitation": [0.1, 0.0],
                "relative_humidity_2m": [80, 81],
            }
        }

        response = mock.Mock()
        response.json.return_value = payload
        response.raise_for_status.return_value = None

        now = dt.datetime(2024, 1, 2, 12, 0, tzinfo=dt.timezone.utc)
        with mock.patch("apps.weather.services.requests.get", return_value=response) as mocked:
            count = ensure_recent_weather(
                site,
                now=now,
                max_age_hours=6,
                lookback_days=2,
                min_retry_minutes=60,
            )
            self.assertEqual(count, 2)
            count_again = ensure_recent_weather(
                site,
                now=now,
                max_age_hours=6,
                lookback_days=2,
                min_retry_minutes=60,
            )
            self.assertEqual(count_again, 0)
            self.assertEqual(mocked.call_count, 1)
