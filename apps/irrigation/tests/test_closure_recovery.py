"""Preserve immediate manual controls and their accepted relay-timeout fallback."""
import datetime as dt

from django.test import TestCase

from apps.irrigation import group_services
from apps.irrigation.balance import delivery_estimate
from apps.irrigation.management.commands.controller import Command
from apps.irrigation.models import IrrigationRun
from apps.irrigation.tests.test_execution_races import ExecutionFixtures


class ExistingManualControlTests(ExecutionFixtures, TestCase):
    def test_manual_open_and_close_are_immediate_without_controller_tick(self):
        run = group_services.start_single(self.valve, 600, "MANUAL")
        self.assertEqual(run.status, "RUNNING")
        self.assertTrue(self.physical[self.valve.pk])
        self.open.assert_called_once_with(self.valve, 600)
        self.now += dt.timedelta(seconds=120)
        group_services.close_member(self.valve)
        run.refresh_from_db()
        self.assertEqual(run.status, "FINISHED")
        self.assertEqual(run.stop_reason, "MANUAL_STOP")
        self.assertFalse(self.physical[self.valve.pk])
        self.assertAlmostEqual(delivery_estimate(run)["estimated_mm"], 0.4)
        self.close.assert_called_once_with(self.valve)

    def test_manual_close_without_run_is_immediate_even_for_disabled_relay(self):
        self.device.enabled = False
        self.device.save(update_fields=["enabled"])
        self.physical[self.valve.pk] = True
        group_services.close_member(self.valve)
        self.assertFalse(self.physical[self.valve.pk])
        self.assertFalse(IrrigationRun.objects.exists())
        self.close.assert_called_once_with(self.valve)

    def test_failed_manual_close_keeps_released_failure_status_and_relay_timer(self):
        run = group_services.start_single(self.valve, 60, "MANUAL")
        self.close.side_effect = RuntimeError("Relay unreachable")
        self.now += dt.timedelta(seconds=10)
        with self.assertRaisesMessage(RuntimeError, "Relay unreachable"):
            group_services.close_member(self.valve)
        self.assertTrue(self.physical[self.valve.pk])
        run.refresh_from_db()
        self.assertEqual(run.status, "FAILED")
        self.assertEqual(run.stop_reason, "ERROR")
        self.now += dt.timedelta(seconds=50)
        self.assertFalse(self.physical[self.valve.pk])
        Command()._stop_running_runs(self.now)
        run.refresh_from_db()
        self.assertEqual(run.status, "FAILED")
        self.close.assert_called_once_with(self.valve)

