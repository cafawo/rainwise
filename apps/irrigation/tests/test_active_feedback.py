"""Dashboard feedback describes actual watering without a sequence cursor."""
import datetime as dt
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.irrigation.models import IrrigationRun, RelayDevice, Site, Valve


class ActiveFeedbackTests(TestCase):
    def setUp(self):
        self.now = dt.datetime(2026, 9, 21, 6, tzinfo=dt.timezone.utc)
        self.client.force_login(get_user_model().objects.create_user("operator"))
        self.site = Site.objects.create(name="Home", timezone="UTC")
        relay = RelayDevice.objects.create(site=self.site, name="Relay", host="invalid")
        self.valve = Valve.objects.create(relay_device=relay, name="Lawn", channel=1)

    def create_run(self, **values):
        return IrrigationRun.objects.create(**{
            "valve": self.valve, "trigger": "GROUP", "status": "PLANNED",
            "max_duration_seconds": 600, **values,
        })

    def assert_feedback(self, status, error=""):
        with mock.patch("apps.irrigation.services.read_valve_state") as read:
            response = self.client.get(reverse("dashboard"))
            self.assertEqual(response.status_code, 200)
            valve = next(v for v in response.context["valves"] if v.pk == self.valve.pk)
            self.assertEqual(valve.action_status, status)
            self.assertEqual(valve.action_error, error)
            payload = self.client.get(reverse("valve_status")).json()
            valve_status = next(v for v in payload if v["id"] == self.valve.pk)
            self.assertEqual(valve_status["action_status"], status)
            self.assertEqual(valve_status["action_error"], error)
        read.assert_not_called()

    def test_current_group_attempt_is_visible(self):
        self.create_run(status="RUNNING", attempt_started_at=self.now)
        self.assert_feedback("Running")

    def test_starting_attempt_is_visible(self):
        self.create_run(attempt_started_at=self.now)
        self.assert_feedback("Starting")

    def test_running_attempt_takes_priority_over_newer_failed_request(self):
        self.create_run(
            status="RUNNING", attempt_started_at=self.now,
            error_message="Early close failed: Relay offline",
        )
        self.create_run(status="FAILED", actual_stop_at=self.now, error_message="Open failed")
        self.assert_feedback("Running", "Early close failed: Relay offline")

    def test_latest_failure_stays_visible(self):
        self.create_run(
            status="FAILED", actual_stop_at=self.now,
            error_message="Opening failed: Relay offline",
        )
        self.assert_feedback("Failed", "Opening failed: Relay offline")

    def test_timeout_completion_does_not_show_stale_closure_error(self):
        self.create_run(
            status="FINISHED", attempt_started_at=self.now,
            actual_stop_at=self.now, error_message="Earlier close failed",
        )
        self.valve.last_known_is_open = True
        self.valve.last_polled_at = self.now - dt.timedelta(minutes=10)
        self.valve.save(update_fields=["last_known_is_open", "last_polled_at"])
        self.assert_feedback("—")
        self.valve.refresh_from_db()
        self.assertTrue(self.valve.last_known_is_open)
        self.assertEqual(self.valve.last_polled_at, self.now - dt.timedelta(minutes=10))

    def test_new_run_takes_priority_over_old_close_error(self):
        self.create_run(
            status="FINISHED", attempt_started_at=self.now,
            actual_stop_at=self.now, error_message="Earlier close failed",
        )
        self.create_run(status="RUNNING", attempt_started_at=self.now)
        self.assert_feedback("Running")
