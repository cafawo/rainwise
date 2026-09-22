from __future__ import annotations

import datetime as dt
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.irrigation.management.commands.controller import Command
from apps.irrigation.models import (
    CurveSettings,
    GroupedRule,
    GroupedRuleValve,
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

    def test_due_fixed_rules_survive_relay_call_crossing_minute_boundary(self):
        now = dt.datetime(2026, 9, 21, 6, 0, 59, tzinfo=dt.timezone.utc)
        other = Valve.objects.create(
            relay_device=self.device, channel=2, name="Back",
        )
        for valve in (self.valve, other):
            ScheduleRule.objects.create(
                schedule=self.schedule, valve=valve, mode="FIXED",
                enabled=True, days_of_week_mask=127, start_time=dt.time(6),
                max_duration_seconds=60,
            )
        with (
            mock.patch("django.utils.timezone.now", return_value=now) as clock,
            mock.patch("apps.irrigation.services.open_valve_for") as opening,
        ):
            def slow_open(_valve, _duration):
                clock.return_value += dt.timedelta(seconds=2)
                return False

            opening.side_effect = slow_open
            Command()._start_due_runs(now)
        self.assertEqual(opening.call_count, 2)
        self.assertEqual(IrrigationRun.objects.filter(status="RUNNING").count(), 2)
        self.assertEqual(
            set(IrrigationRun.objects.values_list("planned_start_at", flat=True)),
            {now.replace(second=0)},
        )

    def test_active_group_holds_automatic_fixed_rules_for_its_site(self):
        now = dt.datetime(2026, 9, 21, 6, tzinfo=dt.timezone.utc)
        ScheduleRule.objects.create(
            schedule=self.schedule, valve=self.valve, mode="FIXED",
            enabled=True, days_of_week_mask=127, start_time=dt.time(6),
            max_duration_seconds=60,
        )
        command = Command()
        command._groups = mock.Mock(active_sites={self.site.pk})
        with mock.patch("apps.irrigation.services.open_valve_for") as opening:
            command._start_due_runs(now)
        opening.assert_not_called()
        self.assertFalse(IrrigationRun.objects.exists())

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

    def test_single_valve_completion_leaves_group_pulses_to_group_service(self):
        now = timezone.now()
        run = IrrigationRun.objects.create(
            valve=self.valve, trigger=IrrigationRun.TRIGGER_GROUP,
            actual_start_at=now - dt.timedelta(seconds=61),
            optimal_duration_seconds=60, max_duration_seconds=60,
            status=IrrigationRun.STATUS_RUNNING,
        )
        with mock.patch("apps.irrigation.services.close_valve") as close:
            self.assertEqual(Command()._stop_running_runs(now), set())
        run.refresh_from_db()
        self.assertEqual(run.status, IrrigationRun.STATUS_RUNNING)
        self.assertIsNone(run.actual_stop_at)
        close.assert_not_called()

    def test_automatic_tick_does_not_shorten_delayed_smart_pulse(self):
        started = dt.datetime(2026, 9, 21, 6, tzinfo=dt.timezone.utc)
        self.valve.application_rate_mm_h = 12
        self.valve.save(update_fields=["application_rate_mm_h"])
        CurveSettings.objects.create(
            site=self.site, min_mm=0.2, max_mm=0.2, coverage_days=1,
        )
        rule = GroupedRule.objects.create(
            schedule=self.schedule, mode="SMART", enabled=True,
            days_of_week_mask=127, start_time=dt.time(6),
        )
        GroupedRuleValve.objects.create(
            rule=rule, valve=self.valve, order=0, duration_seconds=60,
        )
        relay_deadline = None
        command = Command()
        with (
            mock.patch("django.utils.timezone.now", return_value=started) as clock,
            mock.patch("apps.irrigation.services.open_valve_for") as opening,
            mock.patch("apps.irrigation.services.close_valve") as closing,
            mock.patch("apps.irrigation.services.read_device_states") as polling,
            mock.patch.object(command, "_refresh_weather"),
        ):
            def delayed_open(valve, duration):
                nonlocal relay_deadline
                clock.return_value += dt.timedelta(seconds=10)
                relay_deadline = clock.return_value + dt.timedelta(seconds=duration)
                return False

            opening.side_effect = delayed_open
            polling.side_effect = lambda _device: [
                relay_deadline is not None and clock.return_value < relay_deadline,
            ] + [False] * 7
            command._tick(started)
            self.assertEqual(relay_deadline, started + dt.timedelta(seconds=70))

            clock.return_value = started + dt.timedelta(seconds=60)
            command._poll_relays(clock.return_value)
            command._tick(clock.return_value)
            run = IrrigationRun.objects.get(trigger=IrrigationRun.TRIGGER_GROUP)
            self.assertEqual(run.status, IrrigationRun.STATUS_RUNNING)
            self.assertIsNone(run.actual_stop_at)
            closing.assert_not_called()

            clock.return_value = relay_deadline
            command._poll_relays(clock.return_value)
            command._tick(clock.return_value)
            run.refresh_from_db()
            self.assertEqual(run.status, IrrigationRun.STATUS_FINISHED)
            self.assertEqual(run.actual_stop_at, relay_deadline)
            self.assertFalse(Valve.objects.get(pk=self.valve.pk).last_known_is_open)
            opening.assert_called_once_with(self.valve, 60)
            closing.assert_not_called()

    def test_group_error_does_not_prevent_existing_controls_or_weather(self):
        command = Command()
        command._groups = mock.Mock()
        command._groups.tick.side_effect = ValueError("Bad group")
        now = timezone.now()
        with (
            mock.patch.object(command, "_start_due_runs") as start,
            mock.patch.object(command, "_stop_running_runs", return_value={1}) as stop,
            mock.patch.object(command, "_watchdog_close") as watchdog,
            mock.patch.object(command, "_refresh_weather") as weather,
        ):
            command._tick(now)
        start.assert_called_once_with(now)
        stop.assert_called_once_with(now)
        watchdog.assert_called_once_with(now, {1})
        weather.assert_called_once_with(now)

    def test_group_hook_preserves_existing_controller_order(self):
        command = Command()
        command._groups = mock.Mock()
        now = timezone.now()
        order = mock.Mock()
        with (
            mock.patch.object(command, "_start_due_runs") as start,
            mock.patch.object(command, "_stop_running_runs", return_value={1}) as stop,
            mock.patch.object(command, "_watchdog_close") as watchdog,
            mock.patch.object(command, "_refresh_weather") as weather,
        ):
            for name, operation in (
                ("start", start), ("stop", stop), ("groups", command._groups.tick),
                ("watchdog", watchdog), ("weather", weather),
            ):
                order.attach_mock(operation, name)
            command._tick(now)
        self.assertEqual(order.mock_calls, [
            mock.call.start(now), mock.call.stop(now), mock.call.groups(),
            mock.call.watchdog(now, {1}), mock.call.weather(now),
        ])

    def test_control_failures_do_not_skip_other_tick_work(self):
        now = timezone.now()
        for failing in ("_start_due_runs", "_stop_running_runs", "_watchdog_close"):
            with self.subTest(failing=failing):
                command = Command()
                command._groups = mock.Mock()
                with (
                    mock.patch.object(command, "_start_due_runs") as start,
                    mock.patch.object(command, "_stop_running_runs", return_value=set()) as stop,
                    mock.patch.object(command, "_watchdog_close") as watchdog,
                    mock.patch.object(command, "_refresh_weather") as weather,
                ):
                    getattr(command, failing).side_effect = RuntimeError("Unavailable")
                    command._tick(now)
                start.assert_called_once_with(now)
                stop.assert_called_once_with(now)
                command._groups.tick.assert_called_once_with()
                watchdog.assert_called_once_with(now, set())
                weather.assert_called_once_with(now)

    def test_new_controller_has_empty_group_memory(self):
        first = Command()
        second = Command()
        self.assertIsNot(first._group_runner(), second._group_runner())
        self.assertEqual(second._group_runner().active_sites, set())

    def test_watchdog_retains_released_cached_open_recovery(self):
        now = timezone.now()
        self.valve.last_known_is_open = True
        self.valve.save(update_fields=["last_known_is_open"])
        command = Command()
        with (
            mock.patch("apps.irrigation.services.close_valve") as close,
            mock.patch("apps.irrigation.services.read_valve_state") as read,
        ):
            command._watchdog_close(now, set())
            command._watchdog_close(now + dt.timedelta(seconds=60), set())
        close.assert_called_once_with(self.valve)
        read.assert_not_called()
        recovery = IrrigationRun.objects.get(trigger=IrrigationRun.TRIGGER_RECOVERY)
        self.assertEqual(recovery.status, IrrigationRun.STATUS_FINISHED)
        self.assertEqual(recovery.stop_reason, IrrigationRun.STOP_FAILSAFE)
        self.assertEqual(recovery.actual_start_at, now)
        self.assertEqual(recovery.actual_stop_at, now)

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
