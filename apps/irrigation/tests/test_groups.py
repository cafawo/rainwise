"""Behavior tests for transient sequences and ordinary timed watering logs."""
import datetime as dt
from unittest import mock

from django.core.exceptions import ValidationError
from django.test import TestCase

from apps.irrigation import group_services
from apps.irrigation.models import (
    CurveSettings, GroupedRule, GroupedRuleValve, IrrigationRun,
    RelayDevice, Schedule, ScheduleRule, Site, Valve,
)

UTC = dt.timezone.utc


class GroupExecutionTests(TestCase):
    def setUp(self):
        self.now = dt.datetime(2026, 9, 21, 6, tzinfo=UTC)
        self.site = Site.objects.create(name="Garden", timezone="UTC")
        self.schedule = Schedule.objects.create(site=self.site, name="Summer")
        self.site.active_schedule = self.schedule
        self.site.save(update_fields=["active_schedule"])
        self.device = RelayDevice.objects.create(
            site=self.site, name="Test relay", host="test.invalid",
        )
        self.a, self.b = [
            Valve.objects.create(
                relay_device=self.device, channel=index, name=name,
                application_rate_mm_h=7, default_max_duration_seconds=1800,
            )
            for index, name in ((1, "A"), (2, "B"))
        ]
        self.runner = group_services.GroupRunner()
        patches = [
            mock.patch("apps.irrigation.group_services.timezone.now",
                       side_effect=lambda: self.now),
            mock.patch("apps.irrigation.services.open_valve_for", return_value=False),
            mock.patch("apps.irrigation.services.close_valve"),
        ]
        self.clock, self.open, self.close = [patcher.start() for patcher in patches]
        for patcher in patches:
            self.addCleanup(patcher.stop)

    def rule(self, mode="FIXED", durations=(60, 120)):
        rule = GroupedRule.objects.create(
            schedule=self.schedule, mode=mode, start_time=dt.time(6),
            days_of_week_mask=127,
        )
        for order, (valve, duration) in enumerate(zip((self.a, self.b), durations)):
            GroupedRuleValve.objects.create(
                rule=rule, valve=valve, order=order, duration_seconds=duration,
            )
        return rule

    def smart(self, durations=(1800,), demand=7, coverage=1):
        CurveSettings.objects.create(
            site=self.site, min_mm=demand, max_mm=demand,
            coverage_days=coverage, fallback_temperature_c=25,
        )
        return self.rule("SMART", durations)

    def tick(self, seconds=0):
        self.now += dt.timedelta(seconds=seconds)
        self.runner.tick()

    def commands(self):
        return [(call.args[0].name, call.args[1])
                for call in self.open.call_args_list]

    def test_fixed_group_logs_only_attempted_pulses_in_order(self):
        self.rule()
        self.tick()
        self.assertEqual(self.commands(), [("A", 60)])
        self.assertEqual(IrrigationRun.objects.count(), 1)
        self.tick(60)
        self.assertEqual(self.commands(), [("A", 60), ("B", 120)])
        self.tick(120)
        self.assertFalse(self.runner.active_sites)
        self.assertEqual(IrrigationRun.objects.filter(status="FINISHED").count(), 2)
        self.close.assert_not_called()  # The original relay timer ends the pulse.

    def test_enough_smart_rounds_with_equal_duration_rests(self):
        self.smart(durations=(1200,))
        for seconds in (0, 1200, 1200, 1200, 1200, 1200):
            self.tick(seconds)
        self.assertEqual(self.commands(), [("A", 1200)] * 3)
        self.assertFalse(self.runner.active_sites)
        self.assertEqual(
            list(IrrigationRun.objects.order_by("pk").values_list("actual_start_at", flat=True)),
            [dt.datetime(2026, 9, 21, hour, minute, tzinfo=UTC)
             for hour, minute in ((6, 0), (6, 40), (7, 20))],
        )

    def test_shorter_other_valve_does_not_replace_required_rest(self):
        self.b.application_rate_mm_h = 84
        self.b.save(update_fields=["application_rate_mm_h"])
        self.smart(durations=(1800, 300))
        self.tick()
        self.tick(1800)
        self.tick(300)
        self.assertEqual(self.commands(), [("A", 1800), ("B", 300)])
        self.tick(1499)
        self.assertEqual(self.open.call_count, 2)
        self.tick(1)
        self.assertEqual(self.commands()[-1], ("A", 1800))
        self.tick(1800)
        self.assertFalse(self.runner.active_sites)

    def test_forty_five_minute_pulses_supported(self):
        self.smart(durations=(2700,), demand=10.5)
        self.tick()
        self.tick(2700)
        self.assertEqual(self.open.call_count, 1)
        self.tick(2700)
        self.assertEqual(self.commands(), [("A", 2700), ("A", 2700)])

    def test_curve_rate_and_limit_edits_apply_next_day(self):
        rule = self.smart()
        self.tick()
        CurveSettings.objects.filter(site=self.site).update(min_mm=3.5, max_mm=3.5)
        self.a.application_rate_mm_h = 14
        self.a.save(update_fields=["application_rate_mm_h"])
        rule.members.update(duration_seconds=900)
        self.tick(1800)
        self.tick(1800)
        self.assertEqual(self.commands(), [("A", 1800), ("A", 1800)])
        self.assertEqual(
            list(IrrigationRun.objects.values_list("application_rate_mm_h", flat=True)),
            [7, 7],
        )
        self.tick(1800)
        self.now = dt.datetime(2026, 9, 22, 6, tzinfo=UTC)
        self.tick()
        self.assertEqual(self.commands()[-1], ("A", 900))
        self.assertEqual(IrrigationRun.objects.latest("pk").application_rate_mm_h, 14)

    def test_membership_and_order_edits_do_not_rewrite_current_sequence(self):
        rule = self.rule()
        self.tick()
        rule.members.all().delete()
        for order, (valve, duration) in enumerate(((self.b, 300), (self.a, 180))):
            GroupedRuleValve.objects.create(
                rule=rule, valve=valve, order=order, duration_seconds=duration,
            )
        self.tick(60)
        self.assertEqual(self.commands(), [("A", 60), ("B", 120)])
        self.tick(120)
        self.now = dt.datetime(2026, 9, 22, 6, tzinfo=UTC)
        self.tick()
        self.assertEqual(self.commands()[-1], ("B", 300))

    def test_lost_rate_applies_next_invocation(self):
        self.smart(durations=(1800, 1800))
        self.tick()
        Valve.objects.filter(pk=self.b.pk).update(application_rate_mm_h=None)
        self.tick(1800)
        self.assertEqual(self.commands()[-1], ("B", 1800))
        self.assertEqual(IrrigationRun.objects.latest("pk").application_rate_mm_h, 7)

    def test_missing_rate_at_admission_skips_only_that_valve(self):
        self.smart(durations=(1800, 1800))
        Valve.objects.filter(pk=self.a.pk).update(application_rate_mm_h=None)
        self.tick()
        self.assertEqual(self.commands(), [("B", 1800)])

    def test_zero_target_creates_no_watering_records(self):
        self.smart(demand=0)
        self.tick()
        self.tick(30)
        self.assertFalse(IrrigationRun.objects.exists())
        self.assertFalse(self.runner.active_sites)
        self.open.assert_not_called()

    def test_disable_stops_current_pulse_and_future_rounds(self):
        rule = self.smart()
        self.tick()
        rule.enabled = False
        rule.save(update_fields=["enabled"])
        self.tick(60)
        self.close.assert_called_once()
        self.assertFalse(self.runner.active_sites)
        self.assertEqual(self.open.call_count, 1)
        self.assertEqual(IrrigationRun.objects.get().stop_reason, "MANUAL_STOP")

    def test_disable_while_resting_needs_no_command_queue(self):
        rule = self.smart()
        self.tick()
        self.tick(1800)
        rule.enabled = False
        rule.save(update_fields=["enabled"])
        self.tick(60)
        self.assertFalse(self.runner.active_sites)
        self.close.assert_not_called()
        self.assertEqual(self.open.call_count, 1)

    def test_delete_and_schedule_switch_stop_next_tick(self):
        for action in ("delete", "switch"):
            with self.subTest(action=action):
                self.setUp_for_next_scenario()
                rule = self.rule()
                self.tick()
                if action == "delete":
                    rule.delete()
                else:
                    other = Schedule.objects.create(site=self.site, name="Other")
                    Site.objects.filter(pk=self.site.pk).update(active_schedule=other)
                self.tick(10)
                self.assertFalse(self.runner.active_sites)
                self.assertEqual(self.open.call_count, 1)
                self.close.assert_called_once()

    def setUp_for_next_scenario(self):
        GroupedRule.objects.all().delete()
        IrrigationRun.objects.all().delete()
        Site.objects.filter(pk=self.site.pk).update(active_schedule=self.schedule)
        self.now = dt.datetime(2026, 9, 21, 6, tzinfo=UTC)
        self.runner = group_services.GroupRunner()
        self.open.reset_mock()
        self.close.reset_mock()

    def test_failed_early_close_waits_for_timeout_without_retry_machinery(self):
        rule = self.rule()
        self.tick()
        self.close.side_effect = RuntimeError("Offline")
        rule.enabled = False
        rule.save(update_fields=["enabled"])
        self.tick(10)
        self.tick(10)
        self.assertEqual(self.close.call_count, 1)
        self.assertIn(self.site.pk, self.runner.active_sites)
        self.tick(40)
        self.assertFalse(self.runner.active_sites)
        self.assertEqual(self.open.call_count, 1)
        self.assertIsNotNone(IrrigationRun.objects.get().actual_stop_at)

    def test_opening_failure_never_replays_or_opens_next_member(self):
        self.rule()
        self.open.side_effect = RuntimeError("Lost response")
        self.tick()
        self.tick(600)
        self.assertEqual(self.open.call_count, 1)
        run = IrrigationRun.objects.get()
        self.assertTrue(run.delivery_uncertain)
        self.assertFalse(self.runner.active_sites)

    def test_transport_retry_stops_later_pulses_without_changing_driver(self):
        self.rule()
        self.open.return_value = True
        self.tick()
        self.tick(60)
        self.assertEqual(self.open.call_count, 1)
        self.assertFalse(self.runner.active_sites)
        self.assertTrue(IrrigationRun.objects.get().delivery_uncertain)

    def test_manual_close_of_current_pulse_ends_sequence(self):
        self.rule()
        self.tick()
        self.now += dt.timedelta(seconds=10)
        group_services.close_member(self.a)
        self.tick()
        self.assertFalse(self.runner.active_sites)
        self.assertEqual(self.open.call_count, 1)

    def test_restart_abandons_sequence_and_completes_only_existing_log(self):
        self.rule()
        self.tick()
        self.runner = group_services.GroupRunner()
        self.tick(30)
        self.assertEqual(self.open.call_count, 1)
        self.tick(30)
        self.assertEqual(self.open.call_count, 1)
        self.assertEqual(IrrigationRun.objects.get().status, "FINISHED")
        self.assertFalse(self.runner.active_sites)

    def test_restart_during_rest_does_not_resume(self):
        self.smart()
        self.tick()
        self.tick(1800)
        self.runner = group_services.GroupRunner()
        self.tick(1800)
        self.assertEqual(self.open.call_count, 1)

    def test_membership_edit_and_restart_do_not_repeat_the_same_start(self):
        rule = self.smart(durations=(1,), demand=0.003)
        self.tick()
        self.tick(1)
        rule.members.update(valve=self.b)
        self.runner = group_services.GroupRunner()
        self.tick(1)
        self.assertEqual(self.commands(), [("A", 1)])

    def test_missed_start_is_not_backfilled(self):
        self.rule()
        self.tick(60)
        self.open.assert_not_called()
        self.assertFalse(IrrigationRun.objects.exists())

    def test_repeated_dst_time_does_not_restart_after_controller_restart(self):
        self.site.timezone = "Europe/Berlin"
        self.site.save(update_fields=["timezone"])
        self.now = dt.datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
        rule = self.rule()
        rule.start_time = dt.time(2, 30)
        rule.save(update_fields=["start_time"])
        self.tick()
        self.runner = group_services.GroupRunner()
        self.tick(3600)
        self.assertEqual(self.open.call_count, 1)

    def test_existing_watering_skips_group_without_future_rows(self):
        self.rule()
        group_services.start_single(self.a, 600, "MANUAL")
        self.tick()
        self.assertEqual(self.open.call_count, 1)
        self.assertFalse(IrrigationRun.objects.filter(trigger="GROUP").exists())

    def test_intervening_manual_watering_abandons_remaining_group(self):
        self.smart()
        self.tick()
        self.tick(1800)
        group_services.start_single(self.b, 300, "MANUAL")
        self.tick(1)
        # Manual watering is accepted; the group will not overlap its next pulse.
        self.tick(1799)
        self.assertEqual(self.open.call_count, 2)
        self.tick(1)
        self.assertFalse(self.runner.active_sites)

    def test_disabled_relay_prevents_next_opening(self):
        self.rule()
        self.tick()
        self.device.enabled = False
        self.device.save(update_fields=["enabled"])
        self.tick(60)
        self.tick(1)
        self.assertEqual(self.open.call_count, 1)
        self.assertFalse(self.runner.active_sites)

    def test_deleted_valve_abandons_sequence_without_blocking_the_site(self):
        self.rule()
        self.tick()
        self.a.delete()
        self.tick(10)
        self.assertFalse(self.runner.active_sites)
        self.close.assert_called_once()
        self.assertEqual(self.open.call_count, 1)

    def test_slow_opening_uses_return_time_for_completion_and_rest(self):
        self.smart()
        def delayed_open(*args):
            self.now += dt.timedelta(seconds=10)
            return False
        self.open.side_effect = delayed_open
        self.tick()
        self.tick(1790)
        self.assertEqual(IrrigationRun.objects.get().status, "RUNNING")
        self.tick(10)
        self.assertEqual(IrrigationRun.objects.get().actual_stop_at, self.now)
        self.tick(1799)
        self.assertEqual(self.open.call_count, 1)
        self.tick(1)
        self.assertEqual(self.open.call_count, 2)

    def test_reservation_expiry_discards_remaining_sequence(self):
        self.smart()
        self.tick()
        self.tick(23 * 3600)
        self.assertEqual(self.open.call_count, 1)
        self.tick()
        self.assertFalse(self.runner.active_sites)

    def test_slow_preparation_cannot_start_a_pulse_past_its_deadline(self):
        rule = self.rule()
        reserved = group_services.reservation_details(rule)["total_seconds"]
        started = self.now
        self.tick()
        self.now = started + dt.timedelta(
            seconds=reserved - 120 - group_services.command_allowance() - 1,
        )

        def delayed_enabled_check():
            self.now += dt.timedelta(seconds=2)
            return True

        with mock.patch(
            "apps.irrigation.group_services.RelayDevice.objects.filter",
        ) as devices:
            devices.return_value.exists.side_effect = delayed_enabled_check
            self.tick()
        self.assertEqual(self.commands(), [("A", 60)])


    def test_overlapping_group_rejected_but_independent_fixed_overlap_preserved(self):
        group = self.rule()
        ScheduleRule.objects.create(
            schedule=self.schedule, valve=self.a, start_time=dt.time(6),
            days_of_week_mask=127, mode="FIXED", max_duration_seconds=60,
        )
        with self.assertRaises(ValidationError):
            group_services.validate_configuration(group)
        group.delete()
        for duration in (60, 120):
            single = ScheduleRule.objects.create(
                schedule=self.schedule, valve=self.b, start_time=dt.time(6),
                days_of_week_mask=127, mode="FIXED", max_duration_seconds=duration,
            )
            group_services.validate_configuration(single)

    def test_impossible_peak_rejected_without_allocating_watering_rows(self):
        rule = self.smart()
        self.a.application_rate_mm_h = 1e-200
        self.a.save(update_fields=["application_rate_mm_h"])
        with self.assertRaises(ValidationError):
            group_services.validate_configuration(rule)
        self.assertFalse(IrrigationRun.objects.exists())
