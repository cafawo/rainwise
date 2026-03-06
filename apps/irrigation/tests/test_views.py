from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.irrigation.models import (
    IrrigationRun,
    RelayDevice,
    Schedule,
    ScheduleRule,
    Site,
    Valve,
)
from apps.weather.models import WeatherObservation


class LogsViewTests(TestCase):
    def setUp(self) -> None:
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="tester",
            password="password",
        )

        site = Site.objects.create(name="Test Site")
        relay = RelayDevice.objects.create(
            site=site,
            name="Relay",
            host="127.0.0.1",
        )
        self.valve = Valve.objects.create(
            relay_device=relay,
            channel=1,
            name="Valve 1",
        )

    def test_logs_requires_login(self) -> None:
        response = self.client.get(reverse("logs"))
        self.assertEqual(response.status_code, 302)

    def test_logs_renders_recent_runs(self) -> None:
        start = timezone.now()
        stop = start + dt.timedelta(minutes=5)
        IrrigationRun.objects.create(
            valve=self.valve,
            trigger=IrrigationRun.TRIGGER_MANUAL,
            requested_start_at=start,
            actual_start_at=start,
            actual_stop_at=stop,
            optimal_duration_seconds=300,
            max_duration_seconds=600,
            status=IrrigationRun.STATUS_FINISHED,
            stop_reason=IrrigationRun.STOP_COMPLETED,
        )

        self.client.login(username="tester", password="password")
        response = self.client.get(reverse("logs"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Logs")


class ScheduleNewViewTests(TestCase):
    def setUp(self) -> None:
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="tester",
            password="password",
        )

        self.site = Site.objects.create(name="Test Site")
        relay = RelayDevice.objects.create(
            site=self.site,
            name="Relay",
            host="127.0.0.1",
        )
        self.valve = Valve.objects.create(
            relay_device=relay,
            channel=1,
            name="Valve 1",
        )
        self.schedule = Schedule.objects.create(site=self.site, name="Default")
        self.site.active_schedule = self.schedule
        self.site.save(update_fields=["active_schedule"])

        self.rule = ScheduleRule.objects.create(
            schedule=self.schedule,
            valve=self.valve,
            enabled=True,
            days_of_week_mask=1,
            start_time=dt.time(6, 30),
            mode=ScheduleRule.MODE_FIXED,
            max_duration_seconds=600,
        )

    def test_new_schedule_requires_login(self) -> None:
        response = self.client.get(reverse("schedule_new"))
        self.assertEqual(response.status_code, 302)

    def test_new_schedule_can_copy_rules(self) -> None:
        self.client.login(username="tester", password="password")
        response = self.client.post(
            reverse("schedule_new"),
            {"name": "Summer", "copy_current": "on"},
        )
        self.assertEqual(response.status_code, 302)

        new_schedule = Schedule.objects.get(name="Summer")
        self.site.refresh_from_db()
        self.assertEqual(self.site.active_schedule_id, new_schedule.id)
        self.assertEqual(
            ScheduleRule.objects.filter(schedule=new_schedule).count(),
            ScheduleRule.objects.filter(schedule=self.schedule).count(),
        )

    def test_copy_rule_creates_new_rule(self) -> None:
        self.client.login(username="tester", password="password")
        response = self.client.post(
            reverse("schedule_copy", args=[self.rule.id]),
            {
                "valve": self.valve.id,
                "enabled": "on",
                "days_of_week": ["0"],
                "start_time": "06:30",
                "mode": ScheduleRule.MODE_FIXED,
                "max_duration_seconds": "600",
                "note": "Copy",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            ScheduleRule.objects.filter(schedule=self.schedule).count(), 2
        )


class ChartDataTests(TestCase):
    def setUp(self) -> None:
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="tester",
            password="password",
        )

        self.site = Site.objects.create(name="Test Site", timezone="Europe/Berlin")
        relay = RelayDevice.objects.create(
            site=self.site,
            name="Relay",
            host="127.0.0.1",
        )
        self.valve_1 = Valve.objects.create(
            relay_device=relay,
            channel=1,
            name="Valve 1",
        )
        self.valve_2 = Valve.objects.create(
            relay_device=relay,
            channel=2,
            name="Valve 2",
        )

    def test_chart_shows_weather_without_runs(self) -> None:
        now = timezone.now()
        WeatherObservation.objects.create(
            site=self.site,
            timestamp=now,
            temperature_c=12.5,
            precipitation_mm=0.2,
        )
        WeatherObservation.objects.create(
            site=self.site,
            timestamp=now - dt.timedelta(hours=1),
            temperature_c=11.9,
            precipitation_mm=0.0,
        )

        self.client.login(username="tester", password="password")
        response = self.client.get(reverse("chart_data"))
        self.assertEqual(response.status_code, 200)

        payload = response.json()
        self.assertTrue(payload["labels"])

        tz = ZoneInfo(self.site.timezone)
        expected_day = timezone.localtime(now, tz).date().isoformat()
        self.assertEqual(payload["labels"][-1], expected_day)

        datasets = payload["datasets"]
        self.assertEqual(len(datasets), 4)
        axis_ids = {dataset["label"]: dataset.get("yAxisID") for dataset in datasets}
        self.assertEqual(axis_ids.get("Precip (mm)"), "y_precip")
        self.assertEqual(axis_ids.get("Temp (°C)"), "y_temp")

    def test_chart_groups_valves(self) -> None:
        tz = timezone.get_current_timezone()
        start = timezone.make_aware(dt.datetime(2026, 3, 6, 8, 0, 0), tz)

        IrrigationRun.objects.create(
            valve=self.valve_1,
            trigger=IrrigationRun.TRIGGER_MANUAL,
            requested_start_at=start,
            actual_start_at=start,
            actual_stop_at=start + dt.timedelta(minutes=10),
            optimal_duration_seconds=600,
            max_duration_seconds=600,
            status=IrrigationRun.STATUS_FINISHED,
            stop_reason=IrrigationRun.STOP_COMPLETED,
        )
        IrrigationRun.objects.create(
            valve=self.valve_2,
            trigger=IrrigationRun.TRIGGER_MANUAL,
            requested_start_at=start,
            actual_start_at=start,
            actual_stop_at=start + dt.timedelta(minutes=5),
            optimal_duration_seconds=300,
            max_duration_seconds=600,
            status=IrrigationRun.STATUS_FINISHED,
            stop_reason=IrrigationRun.STOP_COMPLETED,
        )

        self.client.login(username="tester", password="password")
        response = self.client.get(reverse("chart_data"))
        self.assertEqual(response.status_code, 200)

        payload = response.json()
        bar_datasets = [ds for ds in payload["datasets"] if ds.get("yAxisID") == "y"]
        self.assertEqual(len(bar_datasets), 2)

        bar_by_label = {ds["label"]: ds["data"][0] for ds in bar_datasets}
        self.assertEqual(bar_by_label.get("Valve 1 (min)"), 10.0)
        self.assertEqual(bar_by_label.get("Valve 2 (min)"), 5.0)
