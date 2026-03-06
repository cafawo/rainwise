from __future__ import annotations

import datetime as dt
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from apps.irrigation.management.commands.controller import Command
from apps.irrigation.models import IrrigationRun, RelayDevice, ScheduleRule, Site, Valve


class ControllerScheduleTests(TestCase):
    def setUp(self) -> None:
        self.site = Site.objects.create(name="Home", timezone="UTC")
        self.device = RelayDevice.objects.create(
            site=self.site, name="Relay", host="127.0.0.1"
        )
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
