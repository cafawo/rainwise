from __future__ import annotations

import datetime as dt

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.irrigation.models import IrrigationRun, RelayDevice, Site, Valve


class LogsViewTests(TestCase):
    def setUp(self) -> None:
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="tester",
            password="password",
        )

        site = Site.objects.create(name="Test Site")
        relay = RelayDevice.objects.create(
            site=site,
            name="Relay",
            host="127.0.0.1",
        )
        self.valve = Valve.objects.create(
            relay_device=relay,
            channel=1,
            name="Valve 1",
        )

    def test_logs_requires_login(self) -> None:
        response = self.client.get(reverse("logs"))
        self.assertEqual(response.status_code, 302)

    def test_logs_renders_recent_runs(self) -> None:
        start = timezone.now()
        stop = start + dt.timedelta(minutes=5)
        IrrigationRun.objects.create(
            valve=self.valve,
            trigger=IrrigationRun.TRIGGER_MANUAL,
            requested_start_at=start,
            actual_start_at=start,
            actual_stop_at=stop,
            optimal_duration_seconds=300,
            max_duration_seconds=600,
            status=IrrigationRun.STATUS_FINISHED,
            stop_reason=IrrigationRun.STOP_COMPLETED,
        )

        self.client.login(username="tester", password="password")
        response = self.client.get(reverse("logs"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Logs")
