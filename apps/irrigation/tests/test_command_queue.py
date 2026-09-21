"""Controller command ownership, including the two release-blocking races."""
import datetime as dt
import threading
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection, connections
from django.db.models.query import QuerySet
from django.test import TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.irrigation import group_services
from apps.irrigation.balance import delivery_estimate
from apps.irrigation.management.commands.controller import Command
from apps.irrigation.models import (
    GroupedRuleValve, IrrigationRun, RelayDevice, Site, Valve, ValveClosure,
)
from apps.irrigation.tests.test_execution_races import ExecutionFixtures


class DelayedCloseRegressionTests(ExecutionFixtures, TestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(get_user_model().objects.create_user("operator"))

    def test_delayed_http_close_cannot_interrupt_replacement_group(self):
        original = group_services.start_single(self.valve, 600, "MANUAL")
        replacement = None
        paused = False

        def delayed_web_close(valve):
            nonlocal paused, replacement
            if not paused:
                paused = True
                # While the old HTTP close pauses, the controller completes
                # cancellation and admits the next group on this valve.
                group_services.reconcile_attempts()
                original.refresh_from_db()
                self.assertIsNotNone(original.closure_confirmed_at)
                rule = self.grouped_rule()
                rule.members.update(valve=self.valve)
                occurrence = group_services._plan_occurrence(
                    rule, scheduled_at=self.now, now=self.now,
                )
                group_services._progress(occurrence, set())
                replacement = occurrence.runs.get()
                self.assertTrue(self.physical[valve.pk])
            self.physical[valve.pk] = False

        self.close.side_effect = delayed_web_close
        self.client.post(reverse("valve_close", args=[self.valve.pk]))
        if replacement is not None:
            # This failed on the original implementation: replacement was
            # RUNNING/certain while the delayed web command had shut its valve.
            self.assertTrue(self.physical[self.valve.pk])
        self.close.assert_not_called()
        original.refresh_from_db()
        self.assertTrue(original.cancellation_requested)

    def test_delayed_legacy_recovery_cannot_interrupt_replacement_manual(self):
        original = IrrigationRun.objects.create(
            valve=self.valve, trigger="MANUAL", status="PLANNED",
            attempt_started_at=self.now, dispatch_state="SENDING",
            cancellation_requested=True, delivery_uncertain=True,
            optimal_duration_seconds=600, max_duration_seconds=600,
        )
        self.physical[self.valve.pk] = True
        paused = False

        def delayed_controller_close(valve):
            nonlocal paused
            if not paused:
                paused = True
                # The old web sender finishes its final close/ack while the
                # recovery close is paused, then another HTTP Open arrives.
                self.physical[valve.pk] = False
                IrrigationRun.objects.filter(pk=original.pk).update(
                    dispatch_state="DONE", status="FINISHED",
                    attempt_finished_at=self.now, actual_stop_at=self.now,
                    closure_confirmed_at=self.now,
                )
                self.client.post(reverse("valve_open", args=[valve.pk]))
            self.physical[valve.pk] = False

        self.close.side_effect = delayed_controller_close
        group_services.reconcile_attempts()
        replacement = IrrigationRun.objects.exclude(pk=original.pk).first()
        if replacement is not None and replacement.status == "RUNNING":
            self.assertTrue(self.physical[self.valve.pk])
        # Legacy senders require explicit stopped-process reconciliation.
        # Normal recovery must not compete with a surviving old web sender.
        self.close.assert_not_called()
        self.open.assert_not_called()


class CommandQueueTests(ExecutionFixtures, TestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(get_user_model().objects.create_user("operator"))

    def tick(self):
        with mock.patch.object(Command, "_refresh_weather"):
            Command()._tick(self.now)

    def test_web_failure_after_enqueue_does_not_require_acknowledgement(self):
        response = self.client.post(reverse("valve_open", args=[self.valve.pk]))
        self.assertEqual(response.status_code, 302)
        run = IrrigationRun.objects.get()
        self.assertEqual(run.dispatch_state, "QUEUED")
        self.assertIsNone(run.attempt_started_at)
        self.assertIsNone(run.actual_start_at)
        self.assertIsNone(delivery_estimate(run)["estimated_mm"])
        self.open.assert_not_called()
        self.close.assert_not_called()
        self.read.assert_not_called()
        # No response acknowledgement or surviving client participates.
        self.tick()
        self.tick()
        run.refresh_from_db()
        self.assertEqual(run.status, "RUNNING")
        self.open.assert_called_once_with(self.valve, 600)

    def test_all_http_action_handlers_only_persist_requests(self):
        rule = self.fixed_rule()
        group = self.grouped_rule()
        actions = [
            ("valve_open", self.valve.pk),
            ("valve_close", self.valve.pk),
            ("schedule_run", rule.pk),
            ("group_run", group.pk),
            ("group_stop", group.pk),
            ("group_delete", group.pk),
        ]
        with mock.patch("apps.irrigation.services.read_device_states") as read_device:
            for name, pk in actions:
                response = self.client.post(reverse(name, args=[pk]))
                self.assertEqual(response.status_code, 302)
            read_device.assert_not_called()
        self.open.assert_not_called()
        self.close.assert_not_called()
        self.read.assert_not_called()

    def test_duplicate_open_and_close_requests_coalesce(self):
        first = group_services.request_single(self.valve, 2700)
        duplicate = group_services.request_single(self.valve, 2700)
        self.assertEqual(first.pk, duplicate.pk)
        one = group_services.close_member(self.valve)
        two = group_services.close_member(self.valve)
        self.assertEqual(one.pk, two.pk)
        self.assertEqual(one.requested_at, two.requested_at)
        self.tick()
        self.open.assert_not_called()
        first.refresh_from_db()
        self.assertIsNone(first.attempt_started_at)
        self.assertFalse(first.delivery_uncertain)
        self.assertEqual(first.status, "FAILED")
        self.assertEqual(first.max_duration_seconds, 2700)
        two.refresh_from_db()
        self.assertIsNotNone(two.confirmed_at)

    def test_cancellation_before_dispatch_prevents_opening(self):
        run = group_services.request_single(self.valve, 600)
        group_services.close_member(self.valve)
        group_services.dispatch_manual_requests()
        run.refresh_from_db()
        self.assertEqual(run.status, "FAILED")
        self.assertIsNone(run.attempt_started_at)
        self.open.assert_not_called()

    def test_close_without_run_closes_disabled_unexpectedly_open_valve(self):
        self.device.enabled = False
        self.device.save(update_fields=["enabled"])
        self.physical[self.valve.pk] = True
        request = group_services.close_member(self.valve)
        self.assertFalse(IrrigationRun.objects.exists())
        self.close.assert_not_called()
        self.tick()
        request.refresh_from_db()
        self.assertIsNotNone(request.confirmed_at)
        self.assertFalse(self.physical[self.valve.pk])
        self.read.assert_called_with(self.valve)

    def test_failed_closure_retains_request_blocks_replacement_and_is_visible(self):
        self.physical[self.valve.pk] = True
        request = group_services.close_member(self.valve)
        self.close.side_effect = RuntimeError("Relay offline")
        self.tick()
        request.refresh_from_db()
        self.assertIsNone(request.confirmed_at)
        self.assertIn("Relay offline", request.error_message)
        with self.assertRaises(ValidationError):
            group_services.request_single(self.valve, 600)
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "Stopping")
        self.assertContains(response, "Relay offline")
        payload = self.client.get(reverse("valve_status")).json()[0]
        self.assertEqual(payload["action_status"], "Stopping")
        self.assertIn("Relay offline", payload["action_error"])
        self.close.side_effect = lambda valve: self.physical.update({valve.pk: False})
        self.tick()
        request.refresh_from_db()
        self.assertIsNotNone(request.confirmed_at)
        self.assertEqual(request.error_message, "")
        group_services.request_single(self.valve, 600)
        self.tick()
        self.assertTrue(self.physical[self.valve.pk])

    def test_failed_close_with_fresh_closed_read_can_complete(self):
        request = group_services.close_member(self.valve)
        self.close.side_effect = RuntimeError("Close response lost")
        self.tick()
        request.refresh_from_db()
        self.assertIsNotNone(request.confirmed_at)
        self.assertEqual(request.error_message, "")

    def test_queued_open_rechecks_new_unconfirmed_closure(self):
        original = group_services.start_single(self.valve, 30, "MANUAL")
        pending = group_services.request_single(self.other, 60)
        self.now += dt.timedelta(seconds=31)
        self.close.side_effect = RuntimeError("Close unavailable")
        self.tick()
        original.refresh_from_db()
        pending.refresh_from_db()
        self.assertIsNone(original.closure_confirmed_at)
        self.assertEqual(pending.status, "FAILED")
        self.assertIn("unconfirmed closure", pending.error_message)
        self.assertFalse(self.physical[self.other.pk])
        self.open.assert_called_once()

    def test_close_member_stops_entire_group_without_closing_future_pulses(self):
        rule = self.grouped_rule()
        GroupedRuleValve.objects.create(
            rule=rule, valve=self.valve, order=1, duration_seconds=2700,
        )
        occurrence = group_services._plan_occurrence(rule, scheduled_at=self.now)
        group_services._progress(occurrence, set())
        group_services.close_member(self.valve)
        self.assertTrue(self.physical[self.other.pk])
        self.tick()
        occurrence.refresh_from_db()
        self.assertEqual(occurrence.status, "CANCELLED")
        self.assertFalse(self.physical[self.other.pk])
        self.open.assert_called_once_with(self.other, 60)
        future = occurrence.runs.get(valve=self.valve)
        self.assertIsNone(future.attempt_started_at)
        self.assertFalse(future.delivery_uncertain)
        self.assertIsNotNone(future.closure_confirmed_at)

    def test_restart_cancels_queued_single_and_group_without_opening(self):
        run = group_services.request_single(self.valve, 2700)
        group_services.recover_groups()
        run.refresh_from_db()
        self.assertEqual(run.status, "FAILED")
        self.assertIn("restarted", run.error_message)
        self.assertEqual(run.max_duration_seconds, 2700)
        self.assertIsNone(run.attempt_started_at)
        rule = self.grouped_rule()
        pending = group_services.request_fixed_group(rule)
        group_services.recover_groups()
        group_services.group_tick(self.now + dt.timedelta(minutes=1))
        pending.refresh_from_db()
        self.assertEqual(pending.status, "CANCELLED")
        self.open.assert_not_called()

    def test_queued_request_expires_visibly_instead_of_starting_late(self):
        run = group_services.request_single(self.valve, 600)
        self.now += dt.timedelta(hours=1)
        self.tick()
        run.refresh_from_db()
        self.assertEqual(run.status, "FAILED")
        self.assertIn("expired", run.error_message)
        self.open.assert_not_called()

    def test_controller_crash_before_or_after_hardware_never_replays_opening(self):
        for applied in (False, True):
            with self.subTest(applied=applied):
                run = group_services.request_single(self.valve, 600)

                def crash(valve, _duration):
                    self.physical[valve.pk] = applied
                    raise SystemExit("Controller process died")

                self.open.side_effect = crash
                with self.assertRaises(SystemExit):
                    group_services.dispatch_manual_requests()
                run.refresh_from_db()
                self.assertEqual(run.dispatch_state, "OPENING")
                group_services.recover_groups()
                group_services.dispatch_manual_requests()
                run.refresh_from_db()
                self.assertIsNotNone(run.closure_confirmed_at)
                self.assertTrue(run.delivery_uncertain)
                self.assertFalse(self.physical[self.valve.pk])
        self.assertEqual(self.open.call_count, 2)

    def test_failed_dispatch_write_performs_no_hardware_io(self):
        run = group_services.request_single(self.valve, 600)
        update = QuerySet.update

        def fail_dispatch(queryset, **values):
            if values.get("dispatch_state") == "OPENING":
                raise DatabaseError("Dispatch unavailable")
            return update(queryset, **values)

        with mock.patch.object(QuerySet, "update", new=fail_dispatch):
            group_services.dispatch_manual_requests()
        self.open.assert_not_called()
        self.close.assert_not_called()
        run.refresh_from_db()
        self.assertEqual(run.dispatch_state, "QUEUED")
        self.tick()
        self.open.assert_called_once()

    def test_failed_result_and_cleanup_writes_recover_next_tick_without_web_ack(self):
        run = group_services.request_single(self.valve, 600)
        update = QuerySet.update

        def fail_results(queryset, **values):
            if queryset.model is IrrigationRun and values.get("dispatch_state") == "DONE":
                raise DatabaseError("Results unavailable")
            return update(queryset, **values)

        with mock.patch.object(QuerySet, "update", new=fail_results):
            group_services.dispatch_manual_requests()
        self.assertFalse(self.physical[self.valve.pk])
        run.refresh_from_db()
        self.assertEqual(run.dispatch_state, "OPENING")
        self.assertIsNone(run.closure_confirmed_at)
        with self.assertRaises(ValidationError):
            group_services.request_single(self.other, 60)
        self.tick()
        run.refresh_from_db()
        self.assertIsNotNone(run.closure_confirmed_at)
        self.assertTrue(run.delivery_uncertain)
        group_services.request_single(self.valve, 600)
        self.tick()
        self.assertTrue(self.physical[self.valve.pk])
        self.assertEqual(self.open.call_count, 2)

    def test_failed_closure_confirmation_write_does_not_release_request(self):
        request = group_services.close_member(self.valve)
        update = QuerySet.update

        def fail_confirmation(queryset, **values):
            if queryset.model is ValveClosure and values.get("confirmed_at"):
                raise DatabaseError("Confirmation unavailable")
            return update(queryset, **values)

        with mock.patch.object(QuerySet, "update", new=fail_confirmation):
            self.tick()
        request.refresh_from_db()
        self.assertIsNone(request.confirmed_at)
        with self.assertRaises(ValidationError):
            group_services.request_single(self.valve, 600)
        self.tick()
        request.refresh_from_db()
        self.assertIsNotNone(request.confirmed_at)
        self.open.assert_not_called()

    def test_failed_http_close_enqueue_rolls_back_cancellation_without_io(self):
        run = group_services.start_single(self.valve, 600, "MANUAL")
        self.open.reset_mock()
        with mock.patch.object(
            ValveClosure.objects, "get_or_create", side_effect=DatabaseError("Storage unavailable"),
        ):
            response = self.client.post(reverse("valve_close", args=[self.valve.pk]), follow=True)
        self.assertContains(response, "Closure request failed: Storage unavailable")
        run.refresh_from_db()
        self.assertFalse(run.cancellation_requested)
        self.assertFalse(ValveClosure.objects.exists())
        self.open.assert_not_called()
        self.close.assert_not_called()
        self.read.assert_not_called()

    def test_failed_http_open_enqueue_performs_no_io(self):
        with mock.patch.object(
            IrrigationRun.objects, "create", side_effect=DatabaseError("Storage unavailable"),
        ):
            response = self.client.post(reverse("valve_open", args=[self.valve.pk]), follow=True)
        self.assertContains(response, "Opening request failed: Storage unavailable")
        self.assertFalse(IrrigationRun.objects.exists())
        self.open.assert_not_called()
        self.close.assert_not_called()
        self.read.assert_not_called()

    def test_queued_feedback_does_not_claim_opened_or_closed(self):
        response = self.client.post(reverse("valve_open", args=[self.valve.pk]), follow=True)
        self.assertContains(response, "Opening requested")
        self.assertContains(response, "Queued")
        self.assertContains(response, f"every {group_services.controller_interval()} seconds")
        self.assertContains(response, "Unknown")
        self.assertNotContains(response, "Valve opened.")
        self.assertNotContains(response, "Run started.")

    def test_site_with_failed_close_does_not_block_other_site(self):
        group_services.close_member(self.valve)
        other_site = Site.objects.create(name="Independent", timezone="UTC")
        device = RelayDevice.objects.create(site=other_site, name="Other", host="192.0.2.2")
        valve = Valve.objects.create(relay_device=device, name="Other", channel=1)
        self.physical[valve.pk] = False
        run = group_services.request_single(valve, 60)
        group_services.dispatch_manual_requests()
        run.refresh_from_db()
        self.assertEqual(run.status, "RUNNING")
        self.assertTrue(self.physical[valve.pk])

    def test_idle_tick_does_not_rewrite_completed_requests(self):
        group_services.close_member(self.valve)
        self.tick()
        with CaptureQueriesContext(connection) as queries:
            self.tick()
        writes = [q["sql"] for q in queries if q["sql"].startswith(("UPDATE", "INSERT", "DELETE"))]
        self.assertEqual(writes, [])

    def test_acknowledged_single_survives_restart_until_its_stop(self):
        run = group_services.start_single(self.valve, 600, "MANUAL")
        group_services.recover_groups()
        run.refresh_from_db()
        self.assertEqual(run.status, "RUNNING")
        self.close.assert_not_called()

    def test_watchdog_closes_and_confirms_genuine_orphan(self):
        Valve.objects.filter(pk=self.valve.pk).update(last_known_is_open=True)
        self.physical[self.valve.pk] = True
        Command()._watchdog_close(self.now, set())
        self.assertFalse(self.physical[self.valve.pk])
        run = IrrigationRun.objects.get(trigger="RECOVERY")
        self.assertIsNotNone(run.closure_confirmed_at)

    def test_failed_closures_are_attempted_once_per_tick(self):
        group_services.start_single(self.valve, 600, "MANUAL")
        self.physical[self.other.pk] = True
        Valve.objects.update(last_known_is_open=True)
        group_services.close_member(self.valve)
        group_services.close_member(self.other)
        self.close.side_effect = RuntimeError("Offline")
        self.tick()
        self.assertEqual(self.close.call_args_list, [mock.call(self.valve), mock.call(self.other)])
        self.assertEqual(self.read.call_args_list, [mock.call(self.valve), mock.call(self.other)])

    def test_watchdog_still_closes_orphan_if_an_earlier_result_write_failed(self):
        group_services.start_single(self.valve, 600, "MANUAL")
        group_services.close_member(self.valve)
        self.physical[self.other.pk] = True
        Valve.objects.filter(pk=self.other.pk).update(last_known_is_open=True)
        group_services.close_member(self.other)
        update = QuerySet.update

        def fail_run_confirmation(queryset, **values):
            if queryset.model is IrrigationRun and values.get("closure_confirmed_at"):
                raise DatabaseError("Run confirmation unavailable")
            return update(queryset, **values)

        with mock.patch.object(QuerySet, "update", new=fail_run_confirmation):
            self.tick()
        self.assertFalse(self.physical[self.other.pk])
        self.close.assert_any_call(self.other)
        self.open.assert_called_once()

    def test_queued_request_does_not_hide_an_unexpected_open_from_watchdog(self):
        group_services.request_single(self.valve, 600)
        self.physical[self.valve.pk] = True
        Valve.objects.filter(pk=self.valve.pk).update(last_known_is_open=True)
        events = []
        self.close.side_effect = lambda valve: (
            events.append("close"), self.physical.update({valve.pk: False}),
        )
        self.open.side_effect = lambda valve, duration: (
            events.append("open"), self.physical.update({valve.pk: True}),
        )
        self.tick()
        self.assertEqual(events, ["close", "open"])
        self.assertTrue(self.physical[self.valve.pk])

    def test_cancelling_never_attempted_group_does_no_hardware_io(self):
        rule = self.grouped_rule()
        occurrence = group_services._plan_occurrence(rule, scheduled_at=self.now)
        group_services.cancel_occurrence(occurrence)
        self.tick()
        self.open.assert_not_called()
        self.close.assert_not_called()
        self.read.assert_not_called()


class PendingHardwareConfigurationTests(ExecutionFixtures, TestCase):
    def test_pending_open_or_orphan_close_blocks_hardware_identity_changes(self):
        from apps.irrigation.admin import RelayDeviceAdminForm, ValveAdminForm

        for action in ("open", "close"):
            with self.subTest(action=action):
                if action == "open":
                    group_services.request_single(self.valve, 600)
                else:
                    IrrigationRun.objects.all().delete()
                    group_services.close_member(self.valve)
                valve_form = ValveAdminForm({
                    "relay_device": self.device.pk, "channel": 3, "name": "A",
                    "is_active_high": "on", "default_max_duration_seconds": 600,
                    "application_rate_mm_h": 12,
                }, instance=Valve.objects.get(pk=self.valve.pk))
                self.assertFalse(valve_form.is_valid())
                self.assertIn("hardware identity", str(valve_form.errors))
                relay_form = RelayDeviceAdminForm({
                    "site": self.site.pk, "host": "192.0.2.99", "port": 502,
                    "unit_id": 1, "name": "Mock relay", "enabled": "on",
                }, instance=RelayDevice.objects.get(pk=self.device.pk))
                self.assertFalse(relay_form.is_valid())
                self.assertIn("hardware identity", str(relay_form.errors))
        self.open.assert_not_called()
        self.close.assert_not_called()

    def test_pending_orphan_close_prevents_admin_cascade_deletion(self):
        self.client.force_login(get_user_model().objects.create_superuser(
            "admin", password="test-only",
        ))
        group_services.close_member(self.valve)
        for model, pk in (("valve", self.valve.pk), ("relaydevice", self.device.pk),
                          ("site", self.site.pk)):
            response = self.client.post(
                reverse(f"admin:irrigation_{model}_delete", args=[pk]), {"post": "yes"},
            )
            self.assertEqual(response.status_code, 403)
        self.assertTrue(ValveClosure.objects.filter(valve=self.valve).exists())
        self.close.assert_not_called()


class ControllerRequestInterleavingTests(ExecutionFixtures, TransactionTestCase):
    """One controller, concurrent database-only web requests on real connections."""

    def run_controller_paused(self, operation, hardware):
        entered = threading.Event()
        release = threading.Event()
        errors = []
        original = hardware.side_effect

        def paused(*args):
            entered.set()
            if not release.wait(10):
                raise AssertionError("Controller was not released")
            return original(*args)

        hardware.side_effect = paused

        def work():
            try:
                operation()
            except Exception as exc:
                errors.append(exc)
            finally:
                connections.close_all()

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(10))
        return thread, release, errors

    def finish(self, thread, release, errors):
        release.set()
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_http_cancel_during_controller_open_closes_before_replacement(self):
        run = group_services.request_single(self.valve, 600)
        paused = self.run_controller_paused(group_services.dispatch_manual_requests, self.open)
        try:
            group_services.close_member(self.valve)
            self.close.assert_not_called()
            with self.assertRaises(ValidationError):
                group_services.request_single(self.valve, 600)
        finally:
            self.finish(*paused)
        run.refresh_from_db()
        self.assertTrue(run.delivery_uncertain)
        self.assertIsNotNone(run.closure_confirmed_at)
        self.assertFalse(self.physical[self.valve.pk])
        group_services.reconcile_attempts()
        replacement = group_services.request_single(self.valve, 600)
        group_services.dispatch_manual_requests()
        replacement.refresh_from_db()
        self.assertEqual(replacement.status, "RUNNING")
        self.assertTrue(self.physical[self.valve.pk])

    def test_delayed_controller_close_blocks_web_replacement_until_returned(self):
        original = group_services.start_single(self.valve, 600, "MANUAL")
        group_services.close_member(self.valve)
        paused = self.run_controller_paused(group_services.reconcile_attempts, self.close)
        try:
            original.refresh_from_db()
            self.assertIsNone(original.closure_confirmed_at)
            with self.assertRaises(ValidationError):
                group_services.request_single(self.valve, 600)
            # Repeated Close during controller I/O only coalesces intent.
            group_services.close_member(self.valve)
            self.open.assert_called_once()
        finally:
            self.finish(*paused)
        original.refresh_from_db()
        self.assertIsNotNone(original.closure_confirmed_at)
        replacement = group_services.request_single(self.valve, 600)
        group_services.dispatch_manual_requests()
        replacement.refresh_from_db()
        self.assertEqual(replacement.status, "RUNNING")
        self.assertTrue(self.physical[self.valve.pk])
        self.assertFalse(replacement.delivery_uncertain)
