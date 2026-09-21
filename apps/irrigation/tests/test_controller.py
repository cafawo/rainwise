from __future__ import annotations

import datetime as dt
from unittest import mock

from django.test import TestCase, override_settings
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
        with mock.patch(
            "apps.irrigation.services.open_valve_for"
        ) as open_valve_for:
            command._start_due_runs(now)
            self.assertEqual(IrrigationRun.objects.count(), 1)
            command._start_due_runs(now)
            self.assertEqual(IrrigationRun.objects.count(), 1)
        open_valve_for.assert_called_once_with(self.valve, 600)

        run = IrrigationRun.objects.first()
        assert run is not None
        self.assertEqual(run.trigger, IrrigationRun.TRIGGER_SCHEDULED)
        self.assertEqual(run.status, IrrigationRun.STATUS_RUNNING)

    def test_residual_dynamic_runs_fixed_at_stored_maximum(self) -> None:
        now = timezone.now().astimezone(dt.timezone.utc)
        start_time = now.time().replace(second=0, microsecond=0)

        ScheduleRule.objects.create(
            schedule=self.schedule,
            valve=self.valve,
            enabled=True,
            days_of_week_mask=1 << now.weekday(),
            start_time=start_time,
            mode="FIXED",
            max_duration_seconds=600,
        )

        ScheduleRule.objects.update(mode="DYNAMIC")
        command = Command()
        with (
            mock.patch("apps.irrigation.services.open_valve_for") as opening,
            mock.patch(
                "apps.irrigation.management.commands.controller.ensure_recent_weather"
            ) as weather,
        ):
            command._start_due_runs(now)
        run = IrrigationRun.objects.get()
        self.assertEqual(run.optimal_duration_seconds, 600)
        opening.assert_called_once_with(self.valve, 600)
        weather.assert_not_called()

    def test_unknown_and_misrouted_smart_modes_never_open(self):
        now = timezone.now().astimezone(dt.timezone.utc)
        rule = ScheduleRule.objects.create(
            schedule=self.schedule, valve=self.valve, enabled=True,
            days_of_week_mask=1 << now.weekday(),
            start_time=now.time().replace(second=0, microsecond=0),
            mode="FIXED", max_duration_seconds=600,
        )
        with mock.patch("apps.irrigation.services.open_valve_for") as opening:
            for invalid in ("SMART", "UNKNOWN"):
                ScheduleRule.objects.filter(pk=rule.pk).update(mode=invalid)
                Command()._start_due_runs(now)
        opening.assert_not_called()
        self.assertFalse(IrrigationRun.objects.exists())

    def test_planner_failures_leave_stops_and_watchdog_operational(self):
        command = Command()
        with (
            mock.patch.object(command, "_start_due_runs", side_effect=ValueError("bad rule")),
            mock.patch.object(command, "_stop_running_runs", return_value={self.valve.pk}) as stop,
            mock.patch.object(command, "_watchdog_close") as watchdog,
            mock.patch("apps.irrigation.group_services.group_tick", side_effect=ValueError("bad group")),
            mock.patch.object(command, "_refresh_weather") as weather,
        ):
            now = timezone.now()
            command._tick(now)
        stop.assert_called_once_with(now)
        watchdog.assert_called_once_with(now, {self.valve.pk}, reconciled=True)
        weather.assert_called_once_with(now)

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

    def test_expected_stop_finishes_if_redundant_close_fails(self) -> None:
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
        with mock.patch(
            "apps.irrigation.services.close_valve",
            side_effect=RuntimeError("relay unavailable"),
        ):
            closed = command._stop_running_runs(now)

        run.refresh_from_db()
        self.assertEqual(run.status, IrrigationRun.STATUS_FINISHED)
        self.assertEqual(run.stop_reason, IrrigationRun.STOP_COMPLETED)
        self.assertIn("Best-effort close failed", run.error_message)
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

    def test_ensure_default_site_uses_default_defaults(self) -> None:
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
        self.assertEqual(site.timezone, "Europe/Berlin")

    @override_settings(TIME_ZONE="UTC")
    def test_ensure_default_site_uses_current_settings_timezone(self) -> None:
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
        self.assertEqual(site.timezone, "UTC")
