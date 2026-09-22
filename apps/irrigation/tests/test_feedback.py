from __future__ import annotations

import datetime as dt
import re
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.irrigation.models import (
    GroupedRule, GroupedRuleValve, RelayDevice, Schedule, Site, Valve,
)


class PageFeedbackTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="feedback", password="test",
        )
        self.client.force_login(self.user)
        self.site = Site.objects.create(name="Home", timezone="UTC")
        self.schedule = Schedule.objects.create(site=self.site, name="Default")
        self.site.active_schedule = self.schedule
        self.site.save(update_fields=["active_schedule"])
        relay = RelayDevice.objects.create(site=self.site, name="Relay", host="invalid")
        self.valve = Valve.objects.create(
            relay_device=relay, channel=1, name="Lawn", application_rate_mm_h=12,
        )
        self.rule = GroupedRule.objects.create(
            schedule=self.schedule, mode="SMART", start_time=dt.time(6),
            days_of_week_mask=127,
        )
        GroupedRuleValve.objects.create(
            rule=self.rule, valve=self.valve, order=1, duration_seconds=900,
        )

    def assert_feedback_before_heading(self, response):
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        heading = html.index("<h1")
        feedback = html.index('id="page-feedback"')
        alerts = list(re.finditer(r'class="alert\s', html))
        self.assertTrue(alerts, "Expected visible page feedback")
        for alert in alerts:
            self.assertLess(feedback, alert.start())
            self.assertLess(alert.start(), heading)

    def assert_invalid_field(self, response, name):
        html = response.content.decode()
        field = re.search(
            rf'<(?:input|select|textarea)\b[^>]*\bid="id_{re.escape(name)}"[^>]*>',
            html,
        )
        self.assertIsNotNone(field, f"Expected the {name} input")
        self.assertIn('aria-invalid="true"', field.group())
        self.assertIn("is-invalid", field.group())
        self.assertIn(f'aria-describedby="id_{name}_errors"', field.group())
        self.assertContains(response, f'id="id_{name}_errors"')

    @override_settings(POSTGRES_HOST="", SQLITE_PATH="")
    def test_setup_and_weather_notices_precede_dashboard_heading(self):
        response = self.client.get(reverse("dashboard"))
        self.assert_feedback_before_heading(response)
        self.assertContains(response, "Using the default SQLite database")
        self.assertContains(response, "Operating with fallback temperature")

    def test_weather_notices_precede_curve_heading(self):
        response = self.client.get(reverse("curve"))
        self.assert_feedback_before_heading(response)
        self.assertNotContains(response, 'id="formErrors"')

    def test_invalid_forms_show_summary_above_heading_and_link_to_field(self):
        for name, data, field in (
            ("curve", {"min_mm": "invalid"}, "min_mm"),
            ("schedule_new", {"name": ""}, "name"),
            ("schedule_load", {"schedule": "invalid"}, "schedule"),
        ):
            with self.subTest(page=name):
                response = self.client.post(reverse(name), data)
                self.assert_feedback_before_heading(response)
                self.assertContains(response, 'id="formErrors"')
                self.assertContains(response, f'href="#id_{field}"')
                self.assert_invalid_field(response, field)
        self.assertEqual(Schedule.objects.count(), 1)

    def test_curve_clean_error_marks_the_actual_input_and_preserves_its_value(self):
        response = self.client.post(reverse("curve"), {
            "min_mm": -1, "max_mm": 7, "g": 0.2, "m": 25,
            "coverage_days": 2, "fallback_temperature_c": 25,
        })
        self.assert_feedback_before_heading(response)
        self.assert_invalid_field(response, "min_mm")
        self.assertContains(response, 'value="-1"')
        self.assertContains(response, "Min must be 0 or higher.")

    def test_rule_errors_stay_linked_to_inline_errors_above_heading(self):
        response = self.client.post(reverse("schedule_create"), {
            "mode": "FIXED", "days_of_week": ["0"], "start_time": "",
            "note": "Keep this name", "members-TOTAL_FORMS": "1",
            "members-INITIAL_FORMS": "0", "members-0-valve": "",
            "members-0-duration_seconds": "", "members-0-ORDER": "1",
        })
        self.assert_feedback_before_heading(response)
        self.assertContains(response, 'href="#id_start_time"')
        self.assertContains(response, 'id="id_start_time_errors"')
        self.assertContains(response, 'class="form-control is-invalid"')
        self.assertContains(response, 'value="Keep this name"')

    def test_missing_rate_warning_precedes_rule_heading(self):
        self.valve.application_rate_mm_h = None
        self.valve.save(update_fields=["application_rate_mm_h"])
        response = self.client.get(reverse("group_edit", args=[self.rule.pk]))
        self.assert_feedback_before_heading(response)
        self.assertContains(response, "Lawn: skipped in Smart")

    def test_preview_warnings_and_failure_precede_heading(self):
        response = self.client.get(reverse("group_preview", args=[self.rule.pk]))
        self.assert_feedback_before_heading(response)
        self.rule.mode = "FIXED"
        self.rule.save(update_fields=["mode"])
        response = self.client.get(reverse("group_preview", args=[self.rule.pk]))
        self.assert_feedback_before_heading(response)
        self.assertContains(response, "Preview is available for Smart rules.")

    def test_action_success_and_failure_precede_dashboard_heading(self):
        with mock.patch("apps.irrigation.group_services.close_member"):
            response = self.client.post(
                reverse("valve_close", args=[self.valve.pk]), follow=True,
            )
        self.assert_feedback_before_heading(response)
        self.assertContains(response, 'class="alert alert-success" role="status"')
        with mock.patch(
            "apps.irrigation.group_services.start_single",
            side_effect=RuntimeError("Another rule is active."),
        ):
            response = self.client.post(
                reverse("valve_open", args=[self.valve.pk]), follow=True,
            )
        self.assert_feedback_before_heading(response)
        self.assertContains(response, "Failed to open valve: Another rule is active.")
        self.assertContains(response, 'class="alert alert-danger" role="alert"')

    def test_login_failure_precedes_sign_in_heading(self):
        self.client.logout()
        response = self.client.post(reverse("login"), {
            "username": "feedback", "password": "incorrect",
        })
        self.assert_feedback_before_heading(response)
        self.assertContains(response, "Sign in failed.")
