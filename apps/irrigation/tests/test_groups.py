from __future__ import annotations

import datetime as dt
import threading
from unittest import mock

from django.core.exceptions import ValidationError
from django.db import DatabaseError, close_old_connections, connections
from django.db.models.query import QuerySet
from django.test import TestCase, TransactionTestCase

from apps.irrigation import group_services
from apps.irrigation.management.commands.controller import Command
from apps.irrigation.models import (
    CurveSettings,
    GroupedRule,
    GroupedRuleValve,
    IrrigationRun,
    RelayDevice,
    RuleOccurrence,
    Schedule,
    ScheduleRule,
    Site,
    Valve,
)


class GroupExecutionTests(TestCase):
    """Exercise persisted execution with bounded, entirely mocked hardware."""

    def setUp(self):
        self.clock = dt.datetime(2026, 9, 21, 6, 0, tzinfo=dt.timezone.utc)
        self.site = Site.objects.create(name="Test garden", timezone="UTC")
        self.schedule = Schedule.objects.create(site=self.site, name="Active")
        self.site.active_schedule = self.schedule
        self.site.save(update_fields=["active_schedule"])
        self.device = RelayDevice.objects.create(
            site=self.site, name="Mock relay", host="192.0.2.1"
        )
        self.valves = [
            Valve.objects.create(
                relay_device=self.device,
                channel=channel,
                name=name,
                default_max_duration_seconds=900,
            )
            for channel, name in ((1, "A"), (2, "B"))
        ]
        self.patches = [
            mock.patch(
                "apps.irrigation.group_services.timezone.now",
                side_effect=lambda: self.clock,
            ),
            mock.patch("apps.irrigation.services.open_valve_for"),
            mock.patch("apps.irrigation.services.close_valve"),
            mock.patch("apps.irrigation.services.read_valve_state", return_value=False),
            mock.patch(
                "apps.irrigation.services.read_device_states", return_value=[False] * 8
            ),
        ]
        self.clock_mock, self.open, self.close, self.read, _ = [
            patcher.start() for patcher in self.patches
        ]
        for patcher in self.patches:
            self.addCleanup(patcher.stop)

    def rule(self, *, mode="FIXED", durations=(60, 120), start_time=None):
        rule = GroupedRule.objects.create(
            schedule=self.schedule,
            mode=mode,
            days_of_week_mask=127,
            start_time=start_time or dt.time(6),
        )
        for order, (valve, duration) in enumerate(zip(self.valves, durations)):
            GroupedRuleValve.objects.create(
                rule=rule, valve=valve, order=order, duration_seconds=duration
            )
        return rule

    def smart_rule(self, *, need=2, durations=(900, 900)):
        CurveSettings.objects.create(
            site=self.site, min_mm=need, max_mm=need, g=1, m=20,
            coverage_days=2, fallback_temperature_c=20,
        )
        for valve in self.valves:
            valve.application_rate_mm_h = 12
            valve.save(update_fields=["application_rate_mm_h"])
        return self.rule(mode="SMART", durations=durations)

    def tick(self, *, seconds=0, stop_finished=False):
        self.clock += dt.timedelta(seconds=seconds)
        if stop_finished:
            Command()._stop_running_runs(self.clock)
        group_services.group_tick(self.clock)

    def assert_no_pending_attempts(self, occurrence):
        self.assertFalse(occurrence.runs.filter(
            status="PLANNED", attempt_started_at__isnull=True,
            cancellation_requested=False,
        ).exists())

    def test_reservations_use_configured_maxima_and_selected_cadence(self):
        fixed = self.rule()
        smart = self.rule(mode="SMART", durations=(900, 600), start_time=dt.time(8))
        for cadence in (30, 60):
            with self.subTest(cadence=cadence), mock.patch.dict(
                "os.environ", {"CONTROLLER_INTERVAL_SECONDS": str(cadence)}
            ):
                self.assertEqual(group_services.reservation_seconds(fixed), (180, 2 * cadence))
                self.assertEqual(group_services.reservation_seconds(smart), (3000, 4 * cadence))

    def test_fixed_needs_no_weather_calibration_or_manual_duration_ceiling(self):
        rule = self.rule(durations=(1200, 60))
        group_services.validate_configuration(rule)
        self.tick()
        self.open.assert_called_once_with(self.valves[0], 1200)

    def test_fixed_run_now_is_controller_owned_and_deduplicated(self):
        rule = self.rule(start_time=dt.time(20))
        first = group_services.request_fixed_group(rule)
        second = group_services.request_fixed_group(rule)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(RuleOccurrence.objects.count(), 1)
        self.open.assert_not_called()
        self.tick()
        self.open.assert_called_once_with(self.valves[0], 60)
        attempted = IrrigationRun.objects.get(attempt_started_at__isnull=False)
        self.assertEqual(attempted.trigger, "MANUAL")

    def test_smart_run_now_and_unknown_modes_cannot_open(self):
        smart = self.smart_rule()
        with self.assertRaises(ValidationError):
            group_services.request_fixed_group(smart)
        GroupedRule.objects.filter(pk=smart.pk).update(mode="ALIEN")
        self.tick()
        self.open.assert_not_called()

    def test_fixed_sequence_waits_for_fresh_closure_and_runs_once(self):
        rule = self.rule()
        self.tick()
        occurrence = RuleOccurrence.objects.get(rule=rule)
        self.tick()
        self.open.assert_called_once_with(self.valves[0], 60)
        self.tick(seconds=60, stop_finished=True)
        self.assertEqual(self.open.call_args_list, [
            mock.call(self.valves[0], 60), mock.call(self.valves[1], 120),
        ])
        self.read.assert_any_call(self.valves[0])
        self.tick(seconds=120, stop_finished=True)
        occurrence.refresh_from_db()
        self.assertEqual(occurrence.status, "FINISHED")
        self.tick()
        self.assertEqual(self.open.call_count, 2)

    def test_failed_fresh_read_interrupts_despite_finished_run_and_closed_cache(self):
        rule = self.rule()
        self.tick()
        self.assertFalse(self.valves[0].last_known_is_open)
        self.read.side_effect = RuntimeError("No current read")
        self.tick(seconds=60, stop_finished=True)
        occurrence = RuleOccurrence.objects.get(rule=rule)
        self.assertIn(occurrence.status, ("STOPPING", "FAILED", "CANCELLED"))
        self.open.assert_called_once_with(self.valves[0], 60)
        self.assert_no_pending_attempts(occurrence)

    def test_claim_and_attempt_exist_before_transmission(self):
        rule = self.rule()

        def inspect_attempt(valve, duration):
            run = IrrigationRun.objects.get(valve=valve, attempt_started_at__isnull=False)
            self.assertEqual(run.optimal_duration_seconds, duration)
            self.assertEqual(run.occurrence.rule_id, rule.pk)
            self.assertEqual(run.occurrence.status, "ACTIVE")

        self.open.side_effect = inspect_attempt
        self.tick()
        self.open.assert_called_once()

    def test_cancellation_during_open_closes_without_starting_next_member(self):
        rule = self.rule()

        def cancel_in_flight(*_args):
            occurrence = RuleOccurrence.objects.get(rule=rule)
            group_services.cancel_occurrence(occurrence, "Test cancellation during open")

        self.open.side_effect = cancel_in_flight
        self.tick()
        occurrence = RuleOccurrence.objects.get(rule=rule)
        self.assertTrue(occurrence.cancellation_requested)
        self.close.assert_any_call(self.valves[0])
        self.tick(seconds=60, stop_finished=True)
        self.open.assert_called_once()
        self.assert_no_pending_attempts(occurrence)

    def test_cancellation_before_open_never_transmits(self):
        rule = self.rule(start_time=dt.time(20))
        occurrence = group_services.request_fixed_group(rule)
        group_services.cancel_occurrence(occurrence, "Cancelled before admission")
        self.tick()
        self.open.assert_not_called()
        occurrence.refresh_from_db()
        self.assertEqual(occurrence.status, "CANCELLED")

    def test_closing_any_group_member_cancels_current_member(self):
        rule = self.rule()
        self.tick()
        group_services.close_member(self.valves[1])
        self.tick()
        occurrence = RuleOccurrence.objects.get(rule=rule)
        self.assertTrue(occurrence.cancellation_requested)
        self.close.assert_any_call(self.valves[0])
        self.open.assert_called_once()

    def test_single_admitted_first_causes_skipped_group_without_late_launch(self):
        rule = self.rule()
        group_services.start_single(self.valves[1], 600, "MANUAL")
        self.tick()
        occurrence = RuleOccurrence.objects.get(rule=rule)
        self.assertEqual(occurrence.status, "SKIPPED")
        self.assertFalse(occurrence.runs.exists())
        self.tick(seconds=600, stop_finished=True)
        self.open.assert_called_once_with(self.valves[1], 600)

    def test_group_admitted_first_blocks_individual_start(self):
        self.rule()
        self.tick()
        with self.assertRaises(ValidationError):
            group_services.start_single(self.valves[1], 60, "MANUAL")
        self.open.assert_called_once_with(self.valves[0], 60)

    def test_pending_run_now_rejected_if_conflict_arises_before_admission(self):
        rule = self.rule(start_time=dt.time(20))
        occurrence = group_services.request_fixed_group(rule)
        IrrigationRun.objects.create(
            valve=self.valves[1], trigger="MANUAL", status="RUNNING",
            actual_start_at=self.clock, attempt_started_at=self.clock,
            optimal_duration_seconds=600, max_duration_seconds=600,
        )
        self.tick()
        occurrence.refresh_from_db()
        self.assertIn(occurrence.status, ("SKIPPED", "FAILED", "CANCELLED"))
        self.open.assert_not_called()

    def test_manual_request_rejects_midnight_crossing(self):
        rule = self.rule(start_time=dt.time(20))
        self.clock = self.clock.replace(hour=23, minute=59)
        with self.assertRaises(ValidationError):
            group_services.request_fixed_group(rule)
        self.open.assert_not_called()

    def test_new_group_conflicts_with_existing_single_rule(self):
        rule = self.rule()
        ScheduleRule.objects.create(
            schedule=self.schedule, valve=self.valves[0], mode="FIXED",
            start_time=dt.time(6, 1), days_of_week_mask=127,
            max_duration_seconds=60,
        )
        with self.assertRaises(ValidationError):
            group_services.validate_configuration(rule)

    def test_switching_schedule_cancels_pending_work(self):
        rule = self.rule()
        self.tick()
        other = Schedule.objects.create(site=self.site, name="Replacement")
        self.site.active_schedule = other
        self.site.save(update_fields=["active_schedule"])
        self.tick()
        self.open.assert_called_once()
        self.close.assert_any_call(self.valves[0])
        occurrence = RuleOccurrence.objects.get(rule=rule)
        self.assert_no_pending_attempts(occurrence)

    def test_disabled_device_still_receives_recovery_close_for_attempted_rows(self):
        rule = self.rule(start_time=dt.time(20))
        for index, status in enumerate(("PLANNED", "FAILED")):
            occurrence = RuleOccurrence.objects.create(
                site=self.site, rule=rule, mode="FIXED", source="MANUAL",
                requested_at=self.clock, status="ACTIVE",
                reservation_end=self.clock + dt.timedelta(minutes=10),
            )
            IrrigationRun.objects.create(
                valve=self.valves[index], occurrence=occurrence,
                pass_number=1, member_order=index, status=status,
                trigger="MANUAL", attempt_started_at=self.clock,
                optimal_duration_seconds=60, max_duration_seconds=60,
            )
        self.device.enabled = False
        self.device.save(update_fields=["enabled"])
        group_services.recover_groups()
        self.open.assert_not_called()
        for valve in self.valves:
            self.close.assert_any_call(valve)
            self.read.assert_any_call(valve)
        self.assertFalse(IrrigationRun.objects.filter(closure_confirmed_at__isnull=True).exists())

    def test_recovery_requires_fresh_read_even_if_redundant_close_fails(self):
        rule = self.rule()
        self.tick()
        occurrence = RuleOccurrence.objects.get(rule=rule)
        group_services.cancel_occurrence(occurrence)
        self.close.side_effect = RuntimeError("Redundant close unavailable")
        self.read.return_value = True
        self.tick()
        occurrence.refresh_from_db()
        self.assertEqual(occurrence.status, "STOPPING")
        self.read.return_value = False
        self.tick()
        occurrence.refresh_from_db()
        self.assertEqual(occurrence.status, "CANCELLED")
        self.assertFalse(occurrence.runs.filter(
            attempt_started_at__isnull=False, closure_confirmed_at__isnull=True
        ).exists())
        self.open.assert_called_once()

    def test_smart_rotates_first_pass_then_partial_second_pass(self):
        rule = self.smart_rule(need=2)
        self.tick()
        self.tick(seconds=900, stop_finished=True)
        self.tick(seconds=900, stop_finished=True)
        self.tick(seconds=300, stop_finished=True)
        self.tick(seconds=300, stop_finished=True)
        self.assertEqual(self.open.call_args_list, [
            mock.call(self.valves[0], 900), mock.call(self.valves[1], 900),
            mock.call(self.valves[0], 300), mock.call(self.valves[1], 300),
        ])
        occurrence = RuleOccurrence.objects.get(rule=rule)
        self.assertEqual(occurrence.status, "FINISHED")
        self.assertEqual(list(occurrence.runs.order_by("pass_number", "member_order").values_list(
            "valve_id", "pass_number", flat=False,
        )), [(self.valves[0].pk, 1), (self.valves[1].pk, 1),
             (self.valves[0].pk, 2), (self.valves[1].pk, 2)])

    def test_zero_demand_is_recorded_once_without_hardware(self):
        rule = self.smart_rule(need=0)
        self.tick()
        self.tick()
        occurrence = RuleOccurrence.objects.get(rule=rule)
        self.assertEqual(occurrence.status, "ZERO")
        self.assertFalse(occurrence.runs.exists())
        self.open.assert_not_called()

    def test_excluded_weekday_never_creates_automatic_work(self):
        rule = self.smart_rule()
        rule.days_of_week_mask = 1 << 1  # Tuesday; the fixed clock is Monday.
        rule.save(update_fields=["days_of_week_mask"])
        self.tick()
        self.assertFalse(RuleOccurrence.objects.exists())
        self.open.assert_not_called()

    def test_rule_deletion_preserves_execution_snapshot(self):
        rule = self.rule()
        self.tick()
        occurrence = RuleOccurrence.objects.get(rule=rule)
        snapshot = occurrence.config
        group_services.cancel_rule(rule, "Deleted")
        rule.delete()
        self.tick()
        occurrence.refresh_from_db()
        self.assertIsNone(occurrence.rule_id)
        self.assertEqual(occurrence.config, snapshot)
        self.assertTrue(occurrence.runs.exists())
        self.open.assert_called_once()

    def test_command_deadline_rechecked_after_claim_with_fresh_clock(self):
        rule = self.rule(durations=(60,))
        original_send = group_services._send_claimed

        def delayed_send(run):
            self.clock += dt.timedelta(seconds=100)
            return original_send(run)

        with mock.patch.object(group_services, "_send_claimed", side_effect=delayed_send):
            self.tick()
        self.open.assert_not_called()
        occurrence = RuleOccurrence.objects.get(rule=rule)
        self.assertTrue(occurrence.cancellation_requested)
        self.assertIn("deadline", occurrence.outcome.lower())

    def test_reservation_overrun_blocks_manual_until_closure_confirmed(self):
        rule = self.rule(durations=(60,))
        self.tick()
        self.read.side_effect = RuntimeError("Cannot establish closure")
        self.tick(seconds=1000, stop_finished=True)
        with self.assertRaises(ValidationError):
            group_services.start_single(self.valves[1], 60, "MANUAL")
        self.open.assert_called_once()
        occurrence = RuleOccurrence.objects.get(rule=rule)
        self.assertEqual(occurrence.status, "STOPPING")
        self.read.side_effect = None
        self.tick()
        group_services.start_single(self.valves[1], 60, "MANUAL")
        self.assertEqual(self.open.call_count, 2)

    def test_increased_cadence_revalidates_existing_group_conflicts(self):
        rule = self.rule(durations=(60,), start_time=dt.time(6))
        ScheduleRule.objects.create(
            schedule=self.schedule, valve=self.valves[1], mode="FIXED",
            days_of_week_mask=127, start_time=dt.time(6, 1, 40),
            max_duration_seconds=60,
        )
        with mock.patch.dict("os.environ", {"CONTROLLER_INTERVAL_SECONDS": "30"}):
            group_services.validate_configuration(rule)
        with mock.patch.dict("os.environ", {"CONTROLLER_INTERVAL_SECONDS": "60"}):
            with self.assertRaises(ValidationError):
                group_services.validate_configuration(rule)
            self.tick()
        self.open.assert_not_called()

    def test_failed_durable_claim_prevents_any_hardware_command(self):
        self.rule()
        original_update = QuerySet.update

        def fail_claim(queryset, **values):
            if queryset.model is IrrigationRun and "attempt_started_at" in values:
                raise DatabaseError("Simulated claim persistence failure")
            return original_update(queryset, **values)

        with mock.patch.object(QuerySet, "update", new=fail_claim):
            self.tick()
        self.open.assert_not_called()
        self.assertFalse(IrrigationRun.objects.filter(attempt_started_at__isnull=False).exists())

    def test_failed_result_write_after_command_closes_and_never_replays(self):
        rule = self.rule()
        original_update = QuerySet.update

        def fail_command_result(queryset, **values):
            if queryset.model is IrrigationRun and values.get("status") == "RUNNING":
                raise DatabaseError("Simulated post-command persistence failure")
            return original_update(queryset, **values)

        with mock.patch.object(QuerySet, "update", new=fail_command_result):
            self.tick()
        self.open.assert_called_once()
        self.close.assert_any_call(self.valves[0])
        attempted = IrrigationRun.objects.get(attempt_started_at__isnull=False)
        self.assertTrue(attempted.delivery_uncertain)
        group_services.recover_groups()
        self.tick(seconds=60)
        self.open.assert_called_once()
        occurrence = RuleOccurrence.objects.get(rule=rule)
        self.assertTrue(occurrence.cancellation_requested)

    def test_two_uncertain_attempts_on_old_schedule_prevent_a_third(self):
        self.smart_rule(need=2, durations=(900,))
        old_schedule = Schedule.objects.create(site=self.site, name="Old schedule")
        old_rule = GroupedRule.objects.create(
            schedule=old_schedule, mode="SMART", start_time=dt.time(4),
            days_of_week_mask=127,
        )
        occurrence = RuleOccurrence.objects.create(
            site=self.site, rule=old_rule, mode="SMART", source="SCHEDULED",
            status="CANCELLED", requested_at=self.clock - dt.timedelta(hours=2),
        )
        for pass_number in (1, 2):
            when = self.clock - dt.timedelta(hours=2) + dt.timedelta(minutes=pass_number)
            IrrigationRun.objects.create(
                valve=self.valves[0], occurrence=occurrence, trigger="SCHEDULED",
                status="FAILED", pass_number=pass_number, member_order=0,
                attempt_started_at=when, attempt_finished_at=when,
                closure_confirmed_at=when + dt.timedelta(seconds=1),
                optimal_duration_seconds=1, max_duration_seconds=900,
                application_rate_mm_h=12, delivery_uncertain=True,
            )
        self.tick()
        self.open.assert_not_called()
        self.assertEqual(IrrigationRun.objects.filter(
            valve=self.valves[0], attempt_started_at__isnull=False,
            occurrence__mode="SMART",
        ).count(), 2)

    def test_completed_delivery_earlier_in_admitted_minute_is_credited(self):
        rule = self.smart_rule(need=2)
        start = self.clock - dt.timedelta(minutes=10) + dt.timedelta(seconds=20)
        stop = self.clock + dt.timedelta(seconds=20)
        IrrigationRun.objects.create(
            valve=self.valves[0], trigger="MANUAL", status="FINISHED",
            actual_start_at=start, actual_stop_at=stop,
            attempt_started_at=start, attempt_finished_at=start,
            closure_confirmed_at=stop, optimal_duration_seconds=600,
            max_duration_seconds=600, application_rate_mm_h=12,
            stop_reason="COMPLETED",
        )
        self.tick(seconds=40)
        occurrence = RuleOccurrence.objects.get(rule=rule)
        self.assertEqual(occurrence.decision_at, self.clock)
        self.assertEqual(occurrence.scheduled_at, self.clock.replace(second=0))
        self.open.assert_called_once_with(self.valves[0], 600)

    def test_dst_gap_is_recorded_as_skipped(self):
        self.site.timezone = "Europe/Berlin"
        self.site.save(update_fields=["timezone"])
        self.clock = dt.datetime(2026, 3, 29, 1, 0, tzinfo=dt.timezone.utc)
        rule = self.rule(start_time=dt.time(2, 30))
        self.tick()
        occurrence = RuleOccurrence.objects.get(rule=rule)
        self.assertEqual(occurrence.status, "SKIPPED")
        self.assertIn("DST", occurrence.outcome)
        self.open.assert_not_called()

    def test_dst_repeated_minute_produces_one_occurrence(self):
        self.site.timezone = "Europe/Berlin"
        self.site.save(update_fields=["timezone"])
        self.clock = dt.datetime(2026, 10, 25, 0, 30, tzinfo=dt.timezone.utc)
        rule = self.rule(durations=(60,), start_time=dt.time(2, 30))
        self.tick()
        self.tick(seconds=3600, stop_finished=True)
        self.assertEqual(RuleOccurrence.objects.filter(rule=rule).count(), 1)
        self.open.assert_called_once()

    def test_other_site_group_does_not_block_individual_opening(self):
        self.rule()
        self.tick()
        other_site = Site.objects.create(name="Independent garden", timezone="UTC")
        other_device = RelayDevice.objects.create(
            site=other_site, name="Other mock", host="192.0.2.3",
        )
        other_valve = Valve.objects.create(
            relay_device=other_device, name="Independent valve", channel=1,
            default_max_duration_seconds=600,
        )
        group_services.start_single(other_valve, 60, "MANUAL")
        self.open.assert_has_calls([
            mock.call(self.valves[0], 60), mock.call(other_valve, 60),
        ])

    def test_unexpected_open_future_member_interrupts_the_reservation(self):
        rule = self.rule()
        self.tick()
        Valve.objects.filter(pk=self.valves[1].pk).update(last_known_is_open=True)
        self.tick(seconds=60, stop_finished=True)
        self.open.assert_called_once_with(self.valves[0], 60)
        occurrence = RuleOccurrence.objects.get(rule=rule)
        self.assertTrue(occurrence.cancellation_requested)

    def test_second_pass_open_state_is_not_mistaken_for_failed_first_pass_closure(self):
        rule = self.smart_rule(need=2)
        self.tick()
        self.tick(seconds=900, stop_finished=True)
        self.tick(seconds=900, stop_finished=True)
        self.read.side_effect = lambda valve: valve.pk == self.valves[0].pk
        self.tick(seconds=60)
        occurrence = RuleOccurrence.objects.get(rule=rule)
        self.assertFalse(occurrence.cancellation_requested)
        self.assertEqual(occurrence.status, "ACTIVE")
        self.assertEqual(self.open.call_count, 3)
        self.read.side_effect = None
        self.tick(seconds=240, stop_finished=True)
        self.open.assert_called_with(self.valves[1], 300)

    def test_legacy_running_pulse_needs_closure_confirmation_before_group_admission(self):
        rule = self.rule()
        legacy = IrrigationRun.objects.create(
            valve=self.valves[1], trigger="MANUAL", status="RUNNING",
            actual_start_at=self.clock - dt.timedelta(seconds=61),
            optimal_duration_seconds=60, max_duration_seconds=60,
        )
        self.close.side_effect = RuntimeError("Legacy redundant close failed")
        self.read.side_effect = RuntimeError("No fresh state available")
        self.tick(stop_finished=True)
        self.open.assert_not_called()
        self.assertEqual(RuleOccurrence.objects.get(rule=rule).status, "SKIPPED")
        legacy.refresh_from_db()
        self.assertEqual(legacy.status, "FINISHED")
        self.assertEqual(legacy.optimal_duration_seconds, 60)
        self.assertIsNone(legacy.closure_confirmed_at)

    def test_failed_startup_recovery_preserves_stops_and_blocks_group_progression(self):
        command = Command()
        command._recovery_pending = True
        with (
            mock.patch.object(
                group_services, "recover_groups",
                side_effect=DatabaseError("Unavailable"),
            ),
            mock.patch.object(group_services, "group_tick") as group_tick,
            mock.patch.object(command, "_start_due_runs"),
            mock.patch.object(command, "_stop_running_runs", return_value=set()) as stop,
            mock.patch.object(command, "_watchdog_close") as watchdog,
            mock.patch.object(command, "_refresh_weather"),
        ):
            command._tick(self.clock)
        stop.assert_called_once()
        watchdog.assert_called_once()
        group_tick.assert_not_called()
        self.assertTrue(command._recovery_pending)

    def assert_daily_deliveries(self, need, expected):
        rule = self.smart_rule(need=need)
        first_decision = self.clock
        for day, expected_mm in enumerate(expected):
            self.clock = first_decision + dt.timedelta(days=day)
            self.tick()
            occurrence = RuleOccurrence.objects.get(
                rule=rule, scheduled_local_date=self.clock.date(),
            )
            for _ in range(4):
                running = occurrence.runs.filter(status="RUNNING").first()
                if running is None:
                    break
                self.tick(
                    seconds=running.optimal_duration_seconds,
                    stop_finished=True,
                )
            occurrence.refresh_from_db()
            self.assertIn(occurrence.status, ("FINISHED", "ZERO"))
            for valve in self.valves:
                delivered_seconds = sum(occurrence.runs.filter(
                    valve=valve, attempt_started_at__isnull=False,
                ).values_list("optimal_duration_seconds", flat=True))
                self.assertAlmostEqual(
                    delivered_seconds * 12 / 3600, expected_mm,
                    msg=f"Day {day + 1}, valve {valve.name}",
                )

    def test_complete_multiday_sequence_alternates_four_and_zero(self):
        self.assert_daily_deliveries(2, [4, 0, 4, 0])

    def test_complete_multiday_sequence_alternates_six_and_two(self):
        self.assert_daily_deliveries(4, [6, 2, 6, 2])

    def test_complete_multiday_sequence_stays_at_capacity_for_high_demand(self):
        self.assert_daily_deliveries(7, [6, 6, 6, 6])

    def test_faster_calibrated_valve_is_fulfilled_and_skips_second_pass(self):
        self.smart_rule(need=2)
        self.valves[1].application_rate_mm_h = 24
        self.valves[1].save(update_fields=["application_rate_mm_h"])
        self.tick()
        self.tick(seconds=900, stop_finished=True)
        self.tick(seconds=600, stop_finished=True)
        self.tick(seconds=300, stop_finished=True)
        self.assertEqual(self.open.call_args_list, [
            mock.call(self.valves[0], 900), mock.call(self.valves[1], 600),
            mock.call(self.valves[0], 300),
        ])

    def test_pending_manual_request_keeps_saved_duration_and_calibration(self):
        self.valves[0].application_rate_mm_h = 12
        self.valves[0].save(update_fields=["application_rate_mm_h"])
        rule = self.rule(durations=(60,), start_time=dt.time(20))
        occurrence = group_services.request_fixed_group(rule)
        rule.members.update(duration_seconds=120)
        self.valves[0].application_rate_mm_h = 24
        self.valves[0].save(update_fields=["application_rate_mm_h"])
        self.tick()
        self.open.assert_called_once_with(self.valves[0], 60)
        attempted = occurrence.runs.get(attempt_started_at__isnull=False)
        self.assertEqual(attempted.optimal_duration_seconds, 60)
        self.assertEqual(attempted.application_rate_mm_h, 12)


class GroupAdmissionRaceTests(TransactionTestCase):
    """Independent DB connections see the committed claim before hardware I/O."""

    def setUp(self):
        self.now = dt.datetime(2026, 9, 21, 6, 0, tzinfo=dt.timezone.utc)
        self.site = Site.objects.create(name="Race garden", timezone="UTC")
        self.schedule = Schedule.objects.create(site=self.site, name="Active")
        self.site.active_schedule = self.schedule
        self.site.save(update_fields=["active_schedule"])
        device = RelayDevice.objects.create(
            site=self.site, name="Mock only", host="192.0.2.2",
        )
        self.valve = Valve.objects.create(
            relay_device=device, channel=1, name="A",
            default_max_duration_seconds=600,
        )
        self.rule = GroupedRule.objects.create(
            schedule=self.schedule, mode="FIXED", days_of_week_mask=127,
            start_time=dt.time(6),
        )
        GroupedRuleValve.objects.create(
            rule=self.rule, valve=self.valve, order=0, duration_seconds=60,
        )
        self.claimed = threading.Event()
        self.release = threading.Event()
        self.errors = []
        self.patches = [
            mock.patch("apps.irrigation.group_services.timezone.now", return_value=self.now),
            mock.patch("apps.irrigation.services.open_valve_for"),
            mock.patch("apps.irrigation.services.close_valve"),
            mock.patch("apps.irrigation.services.read_valve_state", return_value=False),
        ]
        _, self.open, self.close, self.read = [patcher.start() for patcher in self.patches]
        for patcher in self.patches:
            self.addCleanup(patcher.stop)

    def start_claimed_thread(self, operation):
        send = group_services._send_claimed

        def pause_after_claim(run):
            self.claimed.set()
            if not self.release.wait(5):
                raise AssertionError("Test did not release the claimed command")
            return send(run)

        patcher = mock.patch.object(group_services, "_send_claimed", side_effect=pause_after_claim)
        patcher.start()
        self.addCleanup(patcher.stop)

        def work():
            close_old_connections()
            try:
                operation()
            except Exception as exc:
                self.errors.append(exc)
            finally:
                connections.close_all()

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        self.addCleanup(self.release.set)
        self.assertTrue(self.claimed.wait(5), "The first path did not commit a claim")
        return thread

    def finish(self, thread):
        self.release.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.errors, [])

    def test_group_claim_blocks_manual_during_in_flight_open(self):
        thread = self.start_claimed_thread(lambda: group_services.group_tick(self.now))
        try:
            with self.assertRaises(ValidationError):
                group_services.start_single(self.valve, 60, "MANUAL")
        finally:
            self.finish(thread)
        self.open.assert_called_once_with(self.valve, 60)

    def test_manual_claim_causes_group_skip_during_in_flight_open(self):
        thread = self.start_claimed_thread(
            lambda: group_services.start_single(self.valve, 60, "MANUAL")
        )
        try:
            group_services.group_tick(self.now)
            self.assertEqual(RuleOccurrence.objects.get(rule=self.rule).status, "SKIPPED")
        finally:
            self.finish(thread)
        self.open.assert_called_once_with(self.valve, 60)

    def test_group_claim_blocks_automatic_start_during_in_flight_open(self):
        thread = self.start_claimed_thread(lambda: group_services.group_tick(self.now))
        try:
            skipped = group_services.start_single(
                self.valve, 60, "SCHEDULED", planned_start_at=self.now,
            )
            self.assertEqual(skipped.status, "FAILED")
            self.assertIsNone(skipped.attempt_started_at)
            self.assertIn("Skipped", skipped.error_message)
        finally:
            self.finish(thread)
        self.open.assert_called_once_with(self.valve, 60)

    def test_automatic_claim_causes_group_skip_during_in_flight_open(self):
        thread = self.start_claimed_thread(lambda: group_services.start_single(
            self.valve, 60, "SCHEDULED", planned_start_at=self.now,
        ))
        try:
            group_services.group_tick(self.now)
            self.assertEqual(RuleOccurrence.objects.get(rule=self.rule).status, "SKIPPED")
        finally:
            self.finish(thread)
        self.open.assert_called_once_with(self.valve, 60)
