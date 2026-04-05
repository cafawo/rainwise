from __future__ import annotations

import datetime as dt
from unittest import mock

from django.test import TestCase

from apps.irrigation.models import Site
from apps.weather.models import WeatherObservation
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
