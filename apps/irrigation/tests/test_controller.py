from __future__ import annotations

import datetime as dt
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from apps.irrigation.management.commands.controller import Command
from apps.irrigation.models import (
    IrrigationRun,
    RelayDevice,
    Schedule,
    ScheduleRule,
    Site,
    Valve,
)


class ControllerScheduleTests(TestCase):
    def setUp(self) -> None:
        self.site = Site.objects.create(name="Home", timezone="UTC")
        self.device = RelayDevice.objects.create(
            site=self.site, name="Relay", host="127.0.0.1"
        )
        self.schedule = Schedule.objects.create(site=self.site, name="Default")
        self.site.active_schedule = self.schedule
        self.site.save(update_fields=["active_schedule"])
        self.valve = Valve.objects.create(
            relay_device=self.device,
            channel=1,
            name="Front",
            default_max_duration_seconds=600,
        )

    def test_start_due_runs_is_idempotent(self) -> None:
        now = timezone.now().astimezone(dt.timezone.utc)
        start_time = now.time().replace(second=0, microsecond=0)

        ScheduleRule.objects.create(
            schedule=self.schedule,
            valve=self.valve,
            enabled=True,
            days_of_week_mask=1 << now.weekday(),
            start_time=start_time,
            mode=ScheduleRule.MODE_FIXED,
            max_duration_seconds=600,
        )

        command = Command()
        with mock.patch("apps.irrigation.services.open_valve"):
            command._start_due_runs(now)
            self.assertEqual(IrrigationRun.objects.count(), 1)
            command._start_due_runs(now)
            self.assertEqual(IrrigationRun.objects.count(), 1)

        run = IrrigationRun.objects.first()
        assert run is not None
        self.assertEqual(run.trigger, IrrigationRun.TRIGGER_SCHEDULED)
        self.assertEqual(run.status, IrrigationRun.STATUS_RUNNING)

    def test_fixed_run_stops_as_completed(self) -> None:
        now = timezone.now().astimezone(dt.timezone.utc)
        run = IrrigationRun.objects.create(
            valve=self.valve,
            trigger=IrrigationRun.TRIGGER_MANUAL,
            requested_start_at=now - dt.timedelta(seconds=61),
            planned_start_at=None,
            actual_start_at=now - dt.timedelta(seconds=61),
            optimal_duration_seconds=60,
            max_duration_seconds=60,
            status=IrrigationRun.STATUS_RUNNING,
        )

        command = Command()
        with mock.patch("apps.irrigation.services.close_valve"):
            closed = command._stop_running_runs(now)

        run.refresh_from_db()
        self.assertEqual(run.status, IrrigationRun.STATUS_FINISHED)
        self.assertEqual(run.stop_reason, IrrigationRun.STOP_COMPLETED)
        self.assertIn(self.valve.id, closed)

    def test_watchdog_skips_recently_closed(self) -> None:
        now = timezone.now().astimezone(dt.timezone.utc)
        run = IrrigationRun.objects.create(
            valve=self.valve,
            trigger=IrrigationRun.TRIGGER_MANUAL,
            requested_start_at=now - dt.timedelta(seconds=61),
            planned_start_at=None,
            actual_start_at=now - dt.timedelta(seconds=61),
            optimal_duration_seconds=60,
            max_duration_seconds=60,
            status=IrrigationRun.STATUS_RUNNING,
        )
        self.valve.last_known_is_open = True
        self.valve.save(update_fields=["last_known_is_open"])

        command = Command()
        with mock.patch("apps.irrigation.services.close_valve") as close_valve:
            closed = command._stop_running_runs(now)
            command._watchdog_close(now, closed)

        self.assertEqual(IrrigationRun.objects.count(), 1)
        close_valve.assert_called_once_with(self.valve)

    def test_ensure_default_site_uses_default_coordinates(self) -> None:
        Site.objects.all().delete()

        command = Command()
        with mock.patch.dict(
            "os.environ",
            {
                "DEFAULT_SITE_NAME": "",
                "DEFAULT_SITE_LAT": "",
                "DEFAULT_SITE_LON": "",
            },
            clear=False,
        ):
            command._ensure_default_site()

        site = Site.objects.get()
        self.assertEqual(site.name, "Home")
        self.assertEqual(site.latitude, 50.1109)
        self.assertEqual(site.longitude, 8.6821)
