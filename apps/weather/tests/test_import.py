from __future__ import annotations

import datetime as dt
from unittest import mock

from django.test import TestCase

from apps.irrigation.models import Site
from apps.weather.models import WeatherObservation
from apps.weather.services import ensure_recent_weather, import_yesterday_weather


class WeatherImportTests(TestCase):
    def test_import_yesterday_weather(self) -> None:
        site = Site.objects.create(
            name="Home", latitude=52.5, longitude=13.4, timezone="UTC"
        )
        payload = {
            "hourly": {
                "time": ["2024-01-01T00:00", "2024-01-01T01:00"],
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

    def test_ensure_recent_weather_throttles_when_recent(self) -> None:
        site = Site.objects.create(
            name="Home", latitude=52.5, longitude=13.4, timezone="UTC"
        )
        payload = {
            "hourly": {
                "time": ["2024-01-02T10:00", "2024-01-02T11:00"],
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
