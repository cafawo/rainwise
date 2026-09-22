"""Historical context is captured with attempts, without driving execution."""
import datetime as dt
import json
from unittest import mock

from django.db import OperationalError, connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from apps.irrigation import group_services
from apps.irrigation.management.commands.controller import Command
from apps.irrigation.models import (
    CurveSettings, GroupedRule, GroupedRuleValve, IrrigationRun, RelayDevice,
    Schedule, ScheduleRule, Site, Valve,
)
from apps.weather.models import WeatherObservation

UTC = dt.timezone.utc


class RunAppendixTests(TestCase):
    def setUp(self):
        self.now = dt.datetime(2026, 9, 21, 6, tzinfo=UTC)
        self.site = Site.objects.create(name="Garden", timezone="UTC")
        self.schedule = Schedule.objects.create(site=self.site, name="Summer")
        self.site.active_schedule = self.schedule
        self.site.save(update_fields=["active_schedule"])
        self.device = RelayDevice.objects.create(
            site=self.site, name="Relay", host="test.invalid",
        )
        self.valve = Valve.objects.create(
            relay_device=self.device, channel=1, name="Lawn",
            application_rate_mm_h=7, default_max_duration_seconds=1800,
        )
        self.runner = group_services.GroupRunner()
        for target, kwargs in (
            ("django.utils.timezone.now", {"side_effect": lambda: self.now}),
            ("apps.irrigation.services.open_valve_for", {"return_value": False}),
            ("apps.irrigation.services.close_valve", {}),
        ):
            patcher = mock.patch(target, **kwargs)
            mocked = patcher.start()
            self.addCleanup(patcher.stop)
            if target.endswith("open_valve_for"):
                self.opening = mocked
            elif target.endswith("close_valve"):
                self.closing = mocked

    def group(self, mode="SMART"):
        if mode == "SMART":
            CurveSettings.objects.create(
                site=self.site, min_mm=7, max_mm=7, coverage_days=1,
            )
        rule = GroupedRule.objects.create(
            schedule=self.schedule, mode=mode, note="Morning",
            start_time=dt.time(6), days_of_week_mask=127,
        )
        GroupedRuleValve.objects.create(
            rule=rule, valve=self.valve, order=0, duration_seconds=1800,
        )
        return rule

    def weather(self):
        WeatherObservation.objects.bulk_create([
            WeatherObservation(
                site=self.site, timestamp=self.now - dt.timedelta(hours=hour),
                retrieved_at=self.now, temperature_c=20, precipitation_mm=0,
            ) for hour in range(24)
        ])

    def test_manual_context_is_saved_before_failed_opening(self):
        def fail_opening(valve, duration):
            run = IrrigationRun.objects.get()
            self.assertEqual(run.appendix["valve"]["name"], "Lawn")
            self.assertEqual(run.appendix["action"], "watering")
            self.assertIsNone(run.appendix["rule"])
            self.assertNotIn("smart", run.appendix)
            self.assertTrue(run.appendix["simulator"])
            raise RuntimeError("Relay unreachable")

        self.opening.side_effect = fail_opening
        with mock.patch("apps.irrigation.services.SIMULATOR", True):
            with self.assertRaisesRegex(RuntimeError, "Relay unreachable"):
                group_services.start_single(self.valve, 60, "MANUAL")
        run = IrrigationRun.objects.get()
        self.assertEqual(run.status, "FAILED")
        self.assertTrue(run.delivery_uncertain)
        self.assertEqual(run.appendix["schema_version"], 1)
        self.assertEqual(run.appendix["decision_at"], self.now.isoformat())

    def test_single_fixed_captures_rule_and_schedule(self):
        rule = ScheduleRule.objects.create(
            schedule=self.schedule, valve=self.valve, mode="FIXED",
            start_time=dt.time(6), days_of_week_mask=127,
            max_duration_seconds=60, note="Border watering",
        )
        run = group_services.start_single(
            self.valve, 60, "SCHEDULED", planned_start_at=self.now, rule=rule,
        )
        self.assertEqual(run.appendix["mode"], "FIXED")
        self.assertEqual(run.appendix["rule"]["id"], rule.pk)
        self.assertEqual(run.appendix["rule"]["type"], "single")
        self.assertEqual(run.appendix["schedule"]["name"], "Summer")
        self.assertNotIn("smart", run.appendix)

    def test_fixed_group_records_only_attempted_pulse_context(self):
        rule = self.group("FIXED")
        self.runner.tick()
        run = IrrigationRun.objects.get()
        self.assertEqual(run.appendix["rule"]["id"], rule.pk)
        self.assertEqual(run.appendix["rule"]["type"], "group")
        self.assertEqual(run.appendix["pulse"]["pass_number"], 1)
        self.assertEqual(run.appendix["pulse"]["duration_seconds"], 1800)
        self.assertNotIn("smart", run.appendix)

    def test_repeated_smart_pulses_keep_original_evidence_after_edits(self):
        rule = self.group()
        self.weather()
        self.runner.tick()
        first = IrrigationRun.objects.get()
        original = json.loads(json.dumps(first.appendix, allow_nan=False))
        self.assertEqual(len(original["smart"]["temperature"]["samples"]), 24)
        self.assertEqual(original["smart"]["valve"]["target_mm"], 7)

        CurveSettings.objects.filter(site=self.site).update(min_mm=3.5, max_mm=3.5)
        WeatherObservation.objects.update(temperature_c=40, precipitation_mm=10)
        Valve.objects.filter(pk=self.valve.pk).update(
            name="Renamed lawn", application_rate_mm_h=14,
        )
        rule.members.update(duration_seconds=900)
        GroupedRule.objects.filter(pk=rule.pk).update(note="Changed rule")
        Schedule.objects.filter(pk=self.schedule.pk).update(name="Winter")
        Site.objects.filter(pk=self.site.pk).update(name="Changed garden")

        self.now += dt.timedelta(seconds=1800)
        self.runner.tick()
        self.now += dt.timedelta(seconds=1800)
        self.runner.tick()
        second = IrrigationRun.objects.latest("pk")
        first.refresh_from_db()
        self.assertEqual(first.appendix, original)
        self.assertEqual(second.appendix["smart"], original["smart"])
        self.assertEqual(second.appendix["valve"]["name"], "Lawn")
        self.assertEqual(second.appendix["site"]["name"], "Garden")
        self.assertEqual(second.appendix["schedule"]["name"], "Summer")
        self.assertEqual(second.appendix["rule"]["note"], "Morning")
        self.assertEqual(second.appendix["pulse"]["pass_number"], 2)
        self.assertEqual(first.appendix["pulse"]["pass_number"], 1)
        self.assertEqual(second.application_rate_mm_h, 7)
        self.assertEqual(second.optimal_duration_seconds, 1800)
        self.assertNotIn("sequence", second.appendix["smart"])
        self.assertNotIn("pulse_seconds", second.appendix["smart"]["valve"])

    def test_other_valves_evidence_and_warnings_are_not_copied(self):
        rule = self.group()
        other = Valve.objects.create(
            relay_device=self.device, channel=2, name="Uncalibrated back lawn",
        )
        GroupedRuleValve.objects.create(
            rule=rule, valve=other, order=1, duration_seconds=1800,
        )
        self.runner.tick()
        appendix = IrrigationRun.objects.get().appendix
        self.assertEqual(appendix["smart"]["valve"]["valve_id"], self.valve.pk)
        self.assertNotIn(other.name, json.dumps(appendix))
        self.assertTrue(appendix["smart"]["temperature"]["fallback"])

    def test_failed_smart_attempt_keeps_decision_and_is_not_replayed(self):
        self.group()
        self.opening.side_effect = RuntimeError("Relay unreachable")
        with self.assertLogs("apps.irrigation.group_services", level="ERROR"):
            self.runner.tick()
        run = IrrigationRun.objects.get()
        self.assertEqual(run.status, "FAILED")
        self.assertEqual(run.appendix["smart"]["valve"]["target_mm"], 7)
        self.now += dt.timedelta(hours=2)
        group_services.GroupRunner().tick()
        self.assertEqual(IrrigationRun.objects.count(), 1)
        self.assertEqual(self.opening.call_count, 1)

    def test_zero_demand_still_creates_no_run(self):
        self.group()
        CurveSettings.objects.filter(site=self.site).update(min_mm=0, max_mm=0)
        self.runner.tick()
        self.assertFalse(IrrigationRun.objects.exists())
        self.opening.assert_not_called()

    def test_recovery_record_has_close_context(self):
        Valve.objects.filter(pk=self.valve.pk).update(last_known_is_open=True)
        Command()._watchdog_close(self.now, set())
        run = IrrigationRun.objects.get()
        self.assertEqual(run.trigger, "RECOVERY")
        self.assertEqual(run.appendix["action"], "recovery_close")
        self.assertEqual(run.appendix["valve"]["id"], self.valve.pk)
        self.assertNotIn("smart", run.appendix)
        self.opening.assert_not_called()

    def test_routine_controller_reads_do_not_select_appendix(self):
        self.group()
        self.runner.tick()
        self.now += dt.timedelta(seconds=60)
        with CaptureQueriesContext(connection) as queries:
            self.runner.tick()
            command = Command()
            command._stop_running_runs(self.now)
            command._watchdog_close(self.now, set())
        run_reads = [query["sql"] for query in queries
                     if query["sql"].startswith("SELECT")
                     and "irrigation_irrigationrun" in query["sql"]]
        self.assertTrue(run_reads)
        for sql in run_reads:
            self.assertNotIn('"appendix"', sql)

    def test_insert_failure_prevents_opening(self):
        with mock.patch.object(
            IrrigationRun.objects, "create", side_effect=OperationalError("Disk full"),
        ):
            with self.assertRaises(OperationalError):
                group_services.start_single(self.valve, 60, "MANUAL")
        self.opening.assert_not_called()
        self.assertFalse(IrrigationRun.objects.exists())

    def test_unserializable_appendix_prevents_opening(self):
        with mock.patch.object(
            group_services, "run_context", return_value={"invalid": object()},
        ):
            with self.assertRaises(TypeError):
                group_services.start_single(self.valve, 60, "MANUAL")
        self.opening.assert_not_called()

    def test_post_open_write_failure_does_not_repeat_the_bounded_command(self):
        with (
            mock.patch(
                "django.db.models.query.QuerySet.update",
                side_effect=OperationalError("Database unavailable"),
            ),
            self.assertLogs("apps.irrigation.group_services", level="ERROR"),
        ):
            with self.assertRaises(OperationalError):
                group_services.start_single(self.valve, 60, "MANUAL")
        self.opening.assert_called_once()
        self.assertEqual(self.opening.call_args.args[1], 60)
        run = IrrigationRun.objects.get()
        self.assertEqual(run.optimal_duration_seconds, 60)
        self.assertEqual(run.max_duration_seconds, 60)
        self.assertTrue(run.delivery_uncertain)

    def test_capture_failure_does_not_skip_stop_and_watchdog_phases(self):
        ScheduleRule.objects.create(
            schedule=self.schedule, valve=self.valve, mode="FIXED",
            start_time=dt.time(6), days_of_week_mask=127,
            max_duration_seconds=60,
        )
        due_stop = IrrigationRun.objects.create(
            valve=self.valve, trigger="MANUAL", status="RUNNING",
            actual_start_at=self.now - dt.timedelta(seconds=120),
            optimal_duration_seconds=60, max_duration_seconds=60,
        )
        command = Command()
        with (
            mock.patch.object(
                group_services, "run_context", side_effect=ValueError("Bad context"),
            ),
            mock.patch.object(command, "_refresh_weather"),
            mock.patch.object(
                command, "_watchdog_close", wraps=command._watchdog_close,
            ) as watchdog,
            self.assertLogs("apps.irrigation.management.commands.controller"),
        ):
            command._tick(self.now)
        self.opening.assert_not_called()
        self.closing.assert_called_once()
        watchdog.assert_called_once_with(self.now, {self.valve.pk})
        due_stop.refresh_from_db()
        self.assertEqual(due_stop.status, "FINISHED")

    def test_recovery_context_or_insert_failure_does_not_skip_other_closures(self):
        other = Valve.objects.create(
            relay_device=self.device, channel=2, name="Back lawn",
            last_known_is_open=True,
        )
        Valve.objects.filter(pk=self.valve.pk).update(last_known_is_open=True)
        for target, attribute in (
            (group_services, "run_context"),
            (IrrigationRun.objects, "create"),
        ):
            with self.subTest(failure=attribute):
                self.closing.reset_mock()
                with (
                    mock.patch.object(
                        target, attribute, side_effect=OperationalError("Unavailable"),
                    ),
                    self.assertLogs(
                        "apps.irrigation.management.commands.controller", level="ERROR",
                    ),
                ):
                    Command()._watchdog_close(self.now, set())
                self.assertEqual(self.closing.call_count, 2)
                self.assertEqual(
                    {call.args[0].pk for call in self.closing.call_args_list},
                    {self.valve.pk, other.pk},
                )
        self.opening.assert_not_called()
