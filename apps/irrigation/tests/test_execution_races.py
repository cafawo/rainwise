import datetime as dt
import io
import threading
from unittest import mock

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError, connections
from django.db.models.query import QuerySet
from django.test import TestCase, TransactionTestCase

from apps.irrigation import group_services
from apps.irrigation.balance import delivery_estimate
from apps.irrigation.management.commands.controller import Command
from apps.irrigation.models import (
    GroupedRule, GroupedRuleValve, IrrigationRun, RelayDevice, Schedule,
    ScheduleRule, Site, Valve,
)


class ExecutionFixtures:
    def setUp(self):
        super().setUp()
        self.now = dt.datetime(2026, 9, 21, 6, tzinfo=dt.timezone.utc)
        self.site = Site.objects.create(name="Races", timezone="UTC")
        self.schedule = Schedule.objects.create(site=self.site, name="Active")
        self.site.active_schedule = self.schedule
        self.site.save(update_fields=["active_schedule"])
        self.device = RelayDevice.objects.create(
            site=self.site, name="Mock relay", host="192.0.2.1",
        )
        self.valve = Valve.objects.create(
            relay_device=self.device, channel=1, name="A",
            default_max_duration_seconds=600, application_rate_mm_h=12,
        )
        self.other = Valve.objects.create(
            relay_device=self.device, channel=2, name="B",
            default_max_duration_seconds=600,
        )
        self.physical = {self.valve.pk: False, self.other.pk: False}
        patches = [
            mock.patch(
                "apps.irrigation.group_services.timezone.now",
                side_effect=lambda: self.now,
            ),
            mock.patch("apps.irrigation.services.open_valve_for"),
            mock.patch("apps.irrigation.services.close_valve"),
            mock.patch("apps.irrigation.services.read_valve_state"),
        ]
        _, self.open, self.close, self.read = [patch.start() for patch in patches]
        for patch in patches:
            self.addCleanup(patch.stop)
        self.open.side_effect = lambda valve, _: self.physical.update({valve.pk: True})
        self.close.side_effect = lambda valve: self.physical.update({valve.pk: False})
        self.read.side_effect = lambda valve: self.physical[valve.pk]

    def fixed_rule(self):
        return ScheduleRule.objects.create(
            schedule=self.schedule, valve=self.valve, mode="FIXED",
            enabled=True, days_of_week_mask=127, start_time=dt.time(6),
            max_duration_seconds=600,
        )

    def grouped_rule(self):
        rule = GroupedRule.objects.create(
            schedule=self.schedule, mode="FIXED", enabled=True,
            days_of_week_mask=127, start_time=dt.time(6),
        )
        GroupedRuleValve.objects.create(
            rule=rule, valve=self.other, order=0, duration_seconds=60,
        )
        return rule


class ScheduledAdmissionTests(ExecutionFixtures, TestCase):
    def assert_stale_rule_rejected(self, mutate):
        rule = self.fixed_rule()
        mutate(rule)
        with self.assertRaises(ValidationError):
            group_services.start_single(
                self.valve, 600, "SCHEDULED", planned_start_at=self.now, rule=rule,
            )
        self.open.assert_not_called()
        self.assertFalse(IrrigationRun.objects.exists())

    def test_rule_deleted_after_selection_never_opens(self):
        self.assert_stale_rule_rejected(
            lambda rule: ScheduleRule.objects.filter(pk=rule.pk).delete()
        )

    def test_rule_disabled_after_selection_never_opens(self):
        self.assert_stale_rule_rejected(
            lambda rule: ScheduleRule.objects.filter(pk=rule.pk).update(enabled=False)
        )

    def test_rule_mode_changed_after_selection_never_falls_through_to_fixed(self):
        self.assert_stale_rule_rejected(
            lambda rule: ScheduleRule.objects.filter(pk=rule.pk).update(mode="SMART")
        )

    def test_rule_converted_after_selection_never_opens(self):
        def convert(rule):
            group = self.grouped_rule()
            group.mode = "SMART"
            group.save(update_fields=["mode"])
            ScheduleRule.objects.filter(pk=rule.pk).delete()

        self.assert_stale_rule_rejected(convert)

    def test_schedule_switched_after_selection_never_opens(self):
        other = Schedule.objects.create(site=self.site, name="Other")
        self.assert_stale_rule_rejected(lambda _: Site.objects.filter(
            pk=self.site.pk,
        ).update(active_schedule=other))

    def test_valve_changed_after_selection_never_opens(self):
        self.assert_stale_rule_rejected(lambda rule: ScheduleRule.objects.filter(
            pk=rule.pk,
        ).update(valve=self.other))

    def test_duration_changed_after_selection_never_opens_old_duration(self):
        self.assert_stale_rule_rejected(lambda rule: ScheduleRule.objects.filter(
            pk=rule.pk,
        ).update(max_duration_seconds=120))

    def test_start_time_changed_after_selection_never_opens(self):
        self.assert_stale_rule_rejected(lambda rule: ScheduleRule.objects.filter(
            pk=rule.pk,
        ).update(start_time=dt.time(7)))

    def test_weekday_changed_after_selection_never_opens(self):
        self.assert_stale_rule_rejected(lambda rule: ScheduleRule.objects.filter(
            pk=rule.pk,
        ).update(days_of_week_mask=2))

    def test_current_due_rule_still_starts_and_deduplicates(self):
        rule = self.fixed_rule()
        first = group_services.start_single(
            self.valve, 600, "SCHEDULED", planned_start_at=self.now, rule=rule,
        )
        second = group_services.start_single(
            self.valve, 600, "SCHEDULED", planned_start_at=self.now, rule=rule,
        )
        self.assertEqual(first.pk, second.pk)
        self.open.assert_called_once_with(self.valve, 600)

    def test_manual_fixed_run_now_retains_inactive_disabled_rule_semantics(self):
        rule = self.fixed_rule()
        rule.enabled = False
        rule.save(update_fields=["enabled"])
        self.site.active_schedule = None
        self.site.save(update_fields=["active_schedule"])
        group_services.start_single(self.valve, 600, "MANUAL", rule=rule)
        self.open.assert_called_once_with(self.valve, 600)

    def test_fresh_confirmation_does_not_move_rest_start(self):
        run = group_services.start_single(self.valve, 60, "MANUAL")
        self.now += dt.timedelta(seconds=60)
        group_services._confirmed_closed(run, close=True)
        run.refresh_from_db()
        closed_at = run.closure_confirmed_at
        self.now += dt.timedelta(seconds=60)
        self.assertTrue(group_services._confirmed_closed(run))
        run.refresh_from_db()
        self.assertEqual(run.closure_confirmed_at, closed_at)
        self.assertEqual(self.read.call_count, 2)

    def test_maintenance_command_requires_explicit_stopped_sender_confirmation(self):
        with self.assertRaises(CommandError):
            call_command("reconcile_openings", stdout=io.StringIO())
        self.open.assert_not_called()
        self.close.assert_not_called()

    def test_maintenance_reconciles_orphaned_sender_without_replay(self):
        run = IrrigationRun.objects.create(
            valve=self.valve, trigger="MANUAL", status="PLANNED",
            attempt_started_at=self.now, dispatch_state="SENDING",
            optimal_duration_seconds=600, max_duration_seconds=600,
            delivery_uncertain=True,
        )
        self.physical[self.valve.pk] = True
        call_command("reconcile_openings", senders_stopped=True, stdout=io.StringIO())
        run.refresh_from_db()
        self.assertEqual(run.dispatch_state, "DONE")
        self.assertIsNotNone(run.closure_confirmed_at)
        self.assertIsNone(run.attempt_finished_at)
        self.assertTrue(run.delivery_uncertain)
        self.open.assert_not_called()
        self.close.assert_called_once_with(self.valve)

    def test_unsent_claim_expires_without_a_hardware_command(self):
        run = IrrigationRun.objects.create(
            valve=self.valve, trigger="MANUAL", status="PLANNED",
            attempt_started_at=self.now - dt.timedelta(hours=1),
            dispatch_state="UNSENT", optimal_duration_seconds=600,
            max_duration_seconds=600, delivery_uncertain=True,
        )
        group_services._send_claimed(run)
        run.refresh_from_db()
        self.assertEqual(run.dispatch_state, "DONE")
        self.assertEqual(run.status, "FAILED")
        self.assertIsNone(run.attempt_started_at)
        self.open.assert_not_called()

    def test_failed_call_acknowledges_cancellation_and_closes_immediately(self):
        def cancelled_ambiguous_open(valve, _duration):
            self.physical[valve.pk] = True
            IrrigationRun.objects.filter(valve=valve).update(
                cancellation_requested=True,
            )
            raise RuntimeError("Ambiguous hardware response")

        self.open.side_effect = cancelled_ambiguous_open
        with self.assertRaisesRegex(RuntimeError, "Ambiguous"):
            group_services.start_single(self.valve, 600, "MANUAL")
        run = IrrigationRun.objects.get()
        self.assertEqual(run.dispatch_state, "DONE")
        self.assertTrue(run.delivery_uncertain)
        self.assertIsNotNone(run.closure_confirmed_at)
        self.assertFalse(self.physical[self.valve.pk])
        self.close.assert_called_once_with(self.valve)

    def test_failed_call_and_failed_acknowledgement_still_attempt_emergency_close(self):
        update = QuerySet.update

        def fail_acknowledgement(queryset, **values):
            if (queryset.model is IrrigationRun and values.get("status") == "FAILED"
                    and values.get("dispatch_state") == "DONE"):
                raise DatabaseError("Acknowledgement unavailable")
            return update(queryset, **values)

        self.open.side_effect = RuntimeError("Ambiguous hardware response")
        with mock.patch.object(QuerySet, "update", new=fail_acknowledgement):
            with self.assertRaises(DatabaseError):
                group_services.start_single(self.valve, 600, "MANUAL")
        run = IrrigationRun.objects.get()
        self.assertEqual(run.dispatch_state, "SENDING")
        self.assertIsNone(run.attempt_finished_at)
        self.assertTrue(group_services._unresolved_runs(self.site).exists())
        self.close.assert_called_once_with(self.valve)

    def test_acknowledged_manual_stop_keeps_known_shortened_delivery(self):
        run = group_services.start_single(self.valve, 600, "MANUAL")
        self.now += dt.timedelta(seconds=120)
        group_services.close_member(self.valve)
        run.refresh_from_db()
        self.assertFalse(run.sender_interrupted)
        self.assertFalse(run.delivery_uncertain)
        self.assertAlmostEqual(delivery_estimate(run)["estimated_mm"], 0.4)


class SenderInterleavingTests(ExecutionFixtures, TransactionTestCase):
    def start_sender(self, *, before_dispatch=False, physical_before_pause=False):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.errors = []
        sender = group_services._send_claimed

        def pause():
            self.entered.set()
            if not self.release.wait(5):
                raise AssertionError("Sender was not released by its test")

        if before_dispatch:
            def delayed_dispatch(run):
                pause()
                return sender(run)

            patcher = mock.patch.object(
                group_services, "_send_claimed", side_effect=delayed_dispatch,
            )
            patcher.start()
            self.addCleanup(patcher.stop)
        else:
            def delayed_hardware(valve, _duration):
                if physical_before_pause:
                    self.physical[valve.pk] = True
                    Valve.objects.filter(pk=valve.pk).update(last_known_is_open=True)
                pause()
                if not physical_before_pause:
                    self.physical[valve.pk] = True

            self.open.side_effect = delayed_hardware

        def work():
            try:
                group_services.start_single(self.valve, 600, "MANUAL")
            except Exception as exc:
                self.errors.append(exc)
            finally:
                connections.close_all()

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        self.assertTrue(self.entered.wait(5))
        self.addCleanup(self.release.set)
        return thread

    def finish_sender(self, thread):
        self.release.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.errors, [])

    def test_restart_cannot_release_manual_sender_before_late_open_acknowledges(self):
        rule = self.grouped_rule()
        thread = self.start_sender()
        try:
            group_services.recover_groups()
            run = IrrigationRun.objects.get(valve=self.valve)
            self.assertEqual(run.dispatch_state, "SENDING")
            self.assertTrue(run.cancellation_requested)
            self.assertIsNone(run.closure_confirmed_at)
            self.read.assert_called_with(self.valve)
            occurrence = group_services._plan_occurrence(rule, scheduled_at=self.now)
            self.assertEqual(occurrence.status, "SKIPPED")
            with self.assertRaises(ValidationError):
                group_services.start_single(self.other, 60, "MANUAL")
        finally:
            self.finish_sender(thread)
        run.refresh_from_db()
        self.assertEqual(run.dispatch_state, "DONE")
        self.assertEqual(run.status, "FINISHED")
        self.assertIsNotNone(run.closure_confirmed_at)
        self.assertFalse(self.physical[self.valve.pk])

    def test_elapsed_time_never_releases_unacknowledged_manual_sender(self):
        thread = self.start_sender()
        try:
            self.now += dt.timedelta(days=1)
            group_services.reconcile_attempts()
            run = IrrigationRun.objects.get(valve=self.valve)
            self.assertEqual(run.dispatch_state, "SENDING")
            self.assertIsNone(run.closure_confirmed_at)
            with self.assertRaises(ValidationError):
                group_services.start_single(self.other, 60, "MANUAL")
        finally:
            self.finish_sender(thread)

    def test_restart_revokes_unsent_claim_and_late_sender_cannot_transmit(self):
        rule = self.grouped_rule()
        thread = self.start_sender(before_dispatch=True)
        try:
            self.assertEqual(IrrigationRun.objects.get().dispatch_state, "UNSENT")
            group_services.recover_groups()
            occurrence = group_services._plan_occurrence(rule, scheduled_at=self.now)
            self.assertEqual(occurrence.status, "ACTIVE")
        finally:
            self.finish_sender(thread)
        self.open.assert_not_called()
        revoked = IrrigationRun.objects.get(valve=self.valve)
        self.assertEqual(revoked.dispatch_state, "DONE")
        self.assertIsNone(revoked.attempt_started_at)

    def test_late_result_clears_obsolete_closure_until_new_read_confirms(self):
        thread = self.start_sender()
        try:
            group_services.recover_groups()
            IrrigationRun.objects.filter(valve=self.valve).update(
                closure_confirmed_at=self.now - dt.timedelta(minutes=1),
            )
            self.read.side_effect = RuntimeError("Fresh confirmation unavailable")
        finally:
            self.finish_sender(thread)
        run = IrrigationRun.objects.get(valve=self.valve)
        self.assertIsNone(run.closure_confirmed_at)
        self.assertTrue(group_services._unresolved_runs(self.site).exists())

    def test_watchdog_does_not_close_valid_opening_awaiting_acknowledgement(self):
        thread = self.start_sender(physical_before_pause=True)
        try:
            Command()._watchdog_close(self.now, set())
            self.close.assert_not_called()
            self.assertTrue(self.physical[self.valve.pk])
            self.assertFalse(IrrigationRun.objects.filter(trigger="RECOVERY").exists())
        finally:
            self.finish_sender(thread)
        self.assertEqual(IrrigationRun.objects.get().status, "RUNNING")

    def test_watchdog_expiry_cancels_but_holds_sender_until_acknowledgement(self):
        thread = self.start_sender(physical_before_pause=True)
        try:
            self.now += dt.timedelta(seconds=120)
            Command()._watchdog_close(self.now, set())
            run = IrrigationRun.objects.get(trigger="MANUAL")
            self.assertTrue(run.cancellation_requested)
            self.assertEqual(run.dispatch_state, "SENDING")
            self.assertIsNone(run.closure_confirmed_at)
        finally:
            self.finish_sender(thread)
        run.refresh_from_db()
        self.assertEqual(run.status, "FINISHED")
        self.assertLess((run.actual_stop_at - run.actual_start_at).total_seconds(), 600)
        self.assertTrue(run.sender_interrupted)
        estimate = delivery_estimate(run)
        self.assertTrue(estimate["uncertain"])
        self.assertGreaterEqual(estimate["estimated_mm"], 2)
        self.assertIn("physical delivery is uncertain", run.error_message)

    def test_restart_close_before_acknowledgement_retains_uncertain_delivery(self):
        thread = self.start_sender(physical_before_pause=True)
        try:
            self.now += dt.timedelta(seconds=20)
            group_services.recover_groups()
            self.assertFalse(self.physical[self.valve.pk])
        finally:
            self.finish_sender(thread)
        run = IrrigationRun.objects.get(trigger="MANUAL")
        self.assertTrue(run.sender_interrupted)
        self.assertTrue(run.delivery_uncertain)
        self.assertGreaterEqual(delivery_estimate(run)["estimated_mm"], 2)
        self.assertIsNotNone(run.closure_confirmed_at)

    def test_manual_stop_before_acknowledgement_retains_uncertain_delivery(self):
        thread = self.start_sender(physical_before_pause=True)
        try:
            self.now += dt.timedelta(seconds=20)
            group_services.close_member(self.valve)
            self.assertFalse(self.physical[self.valve.pk])
        finally:
            self.finish_sender(thread)
        run = IrrigationRun.objects.get(trigger="MANUAL")
        self.assertTrue(run.sender_interrupted)
        self.assertTrue(delivery_estimate(run)["uncertain"])

    def test_cancellation_before_acknowledgement_keeps_conservative_delivery(self):
        thread = self.start_sender(physical_before_pause=True)
        try:
            self.now += dt.timedelta(seconds=20)
            IrrigationRun.objects.filter(valve=self.valve).update(
                cancellation_requested=True,
            )
            self.close.assert_not_called()
        finally:
            self.finish_sender(thread)
        run = IrrigationRun.objects.get(trigger="MANUAL")
        self.assertFalse(run.sender_interrupted)
        estimate = delivery_estimate(run)
        self.assertTrue(estimate["uncertain"])
        self.assertGreaterEqual(estimate["estimated_mm"], 2)
        self.assertIn("physical delivery is uncertain", run.error_message)

    def test_failed_recovery_interruption_record_still_preserves_uncertainty(self):
        thread = self.start_sender(physical_before_pause=True)
        try:
            self.now += dt.timedelta(seconds=20)
            with mock.patch.object(
                group_services, "mark_sender_interrupted",
                side_effect=DatabaseError("Transient interruption record failure"),
            ):
                group_services.recover_groups()
            self.assertFalse(self.physical[self.valve.pk])
        finally:
            self.finish_sender(thread)
        run = IrrigationRun.objects.get(trigger="MANUAL")
        self.assertTrue(run.cancellation_requested)
        self.assertFalse(run.sender_interrupted)
        self.assertTrue(delivery_estimate(run)["uncertain"])
        self.assertGreaterEqual(delivery_estimate(run)["estimated_mm"], 2)
        self.assertIn("physical delivery is uncertain", run.error_message)

    def test_failed_watchdog_interruption_record_still_preserves_uncertainty(self):
        thread = self.start_sender(physical_before_pause=True)
        try:
            self.now += dt.timedelta(seconds=120)
            with mock.patch.object(
                group_services, "mark_sender_interrupted",
                side_effect=DatabaseError("Transient interruption record failure"),
            ):
                Command()._watchdog_close(self.now, set())
            self.assertFalse(self.physical[self.valve.pk])
        finally:
            self.finish_sender(thread)
        run = IrrigationRun.objects.get(trigger="MANUAL")
        self.assertTrue(run.cancellation_requested)
        self.assertFalse(run.sender_interrupted)
        self.assertTrue(delivery_estimate(run)["uncertain"])
        self.assertGreaterEqual(delivery_estimate(run)["estimated_mm"], 2)

    def test_failed_manual_stop_interruption_record_still_preserves_uncertainty(self):
        thread = self.start_sender(physical_before_pause=True)
        try:
            self.now += dt.timedelta(seconds=20)
            with mock.patch.object(
                group_services, "mark_sender_interrupted",
                side_effect=DatabaseError("Transient interruption record failure"),
            ):
                with self.assertRaises(DatabaseError):
                    group_services.close_member(self.valve)
            self.assertFalse(self.physical[self.valve.pk])
        finally:
            self.finish_sender(thread)
        run = IrrigationRun.objects.get(trigger="MANUAL")
        self.assertTrue(run.cancellation_requested)
        self.assertFalse(run.sender_interrupted)
        self.assertTrue(delivery_estimate(run)["uncertain"])
        self.assertGreaterEqual(delivery_estimate(run)["estimated_mm"], 2)

    def test_watchdog_still_closes_genuine_orphan(self):
        Valve.objects.filter(pk=self.valve.pk).update(last_known_is_open=True)
        self.physical[self.valve.pk] = True
        Command()._watchdog_close(self.now, set())
        self.close.assert_called_once_with(self.valve)
        self.assertFalse(self.physical[self.valve.pk])
        self.assertTrue(IrrigationRun.objects.filter(trigger="RECOVERY").exists())

    def test_acknowledged_manual_run_survives_controller_restart_until_its_stop(self):
        run = group_services.start_single(self.valve, 600, "MANUAL")
        group_services.recover_groups()
        run.refresh_from_db()
        self.assertEqual(run.dispatch_state, "DONE")
        self.assertEqual(run.status, "RUNNING")
        self.close.assert_not_called()
