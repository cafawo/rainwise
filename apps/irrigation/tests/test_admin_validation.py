import datetime as dt

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from apps.irrigation.admin import CurveSettingsAdminForm, ValveAdminForm
from apps.irrigation.models import (
    CurveSettings, GroupedRule, GroupedRuleValve, RelayDevice, Schedule,
    ScheduleRule, Site, Valve,
)


class AdminReservationValidationTests(TestCase):
    def setUp(self):
        self.site = Site.objects.create(name="Garden", timezone="UTC")
        self.schedule = Schedule.objects.create(site=self.site, name="Summer")
        self.device = RelayDevice.objects.create(
            site=self.site, name="Relay", host="test.invalid"
        )
        self.valve = Valve.objects.create(
            relay_device=self.device, name="Lawn", channel=1,
            application_rate_mm_h=12,
        )
        self.curve = CurveSettings.objects.create(
            site=self.site, min_mm=2, max_mm=2, coverage_days=1
        )
        self.rule = GroupedRule.objects.create(
            schedule=self.schedule, mode="SMART", start_time=dt.time(6),
            days_of_week_mask=127,
        )
        self.member = GroupedRuleValve.objects.create(
            rule=self.rule, valve=self.valve, order=1, duration_seconds=900
        )
        self.following = ScheduleRule.objects.create(
            schedule=self.schedule, valve=self.valve, mode="FIXED",
            start_time=dt.time(6, 20), days_of_week_mask=127,
            max_duration_seconds=60,
        )
        self.valve_data = {
            "relay_device": self.device.pk, "name": "Lawn", "channel": 1,
            "description": "", "is_active_high": "on",
            "default_max_duration_seconds": 1800, "application_rate_mm_h": 12,
        }
        self.curve_data = {
            "site": self.site.pk, "min_mm": 2, "max_mm": 2, "g": 0.1852,
            "m": 25.6653, "coverage_days": 1, "fallback_temperature_c": 25,
        }

    def test_admin_rate_change_that_expands_peak_is_rejected_without_writes(self):
        form = ValveAdminForm(
            {**self.valve_data, "application_rate_mm_h": 6}, instance=self.valve
        )
        self.assertFalse(form.is_valid())
        self.assertIn("overlaps", str(form.errors))
        self.valve.refresh_from_db()
        self.assertEqual(self.valve.application_rate_mm_h, 12)
        self.member.refresh_from_db()
        self.assertEqual(self.member.duration_seconds, 900)

    def test_admin_coverage_and_curve_changes_revalidate_peak_reservation(self):
        for changes in ({"coverage_days": 2}, {"max_mm": 4}):
            with self.subTest(changes=changes):
                form = CurveSettingsAdminForm(
                    {**self.curve_data, **changes}, instance=self.curve
                )
                self.assertFalse(form.is_valid())
                self.assertIn("overlaps", str(form.errors))
                self.curve.refresh_from_db()
                self.assertEqual(self.curve.coverage_days, 1)
                self.assertEqual(self.curve.max_mm, 2)

    def test_valid_rate_removal_keeps_member_and_probe_is_not_a_save(self):
        form = ValveAdminForm(
            {**self.valve_data, "application_rate_mm_h": ""}, instance=self.valve
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(
            Valve.objects.get(pk=self.valve.pk).application_rate_mm_h, 12
        )
        form.save()
        self.valve.refresh_from_db()
        self.assertIsNone(self.valve.application_rate_mm_h)
        self.assertTrue(GroupedRuleValve.objects.filter(pk=self.member.pk).exists())

    def test_zero_fallback_override_remains_valid_and_unsaved_until_save(self):
        form = CurveSettingsAdminForm(
            {**self.curve_data, "fallback_temperature_c": 0}, instance=self.curve
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(
            CurveSettings.objects.get(pk=self.curve.pk).fallback_temperature_c, 25
        )
        form.save()
        self.curve.refresh_from_db()
        self.assertEqual(self.curve.fallback_temperature_c, 0)

    def test_new_admin_curve_rejected_without_creating_settings(self):
        self.curve.delete()
        self.following.start_time = dt.time(9)
        self.following.save()
        form = CurveSettingsAdminForm({
            **self.curve_data, "max_mm": 14, "coverage_days": 2,
        })
        self.assertFalse(form.is_valid())
        self.assertIn("overlaps", str(form.errors))
        self.assertFalse(CurveSettings.objects.filter(site=self.site).exists())

    def test_admin_post_rerenders_conflict_instead_of_saving_or_erroring(self):
        user = get_user_model().objects.create_superuser(
            username="admin", password="test-pass"
        )
        self.client.force_login(user)
        response = self.client.post(
            reverse("admin:irrigation_valve_change", args=[self.valve.pk]),
            {**self.valve_data, "application_rate_mm_h": 6, "_save": "Save"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "overlaps")
        self.valve.refresh_from_db()
        self.assertEqual(self.valve.application_rate_mm_h, 12)

    def test_missing_or_invalid_fixed_duration_reports_field_error(self):
        self.rule.mode = "FIXED"
        self.rule.save()
        for value in (None, "invalid"):
            with self.subTest(duration=value):
                self.member.duration_seconds = value
                with self.assertRaises(ValidationError) as caught:
                    self.member.full_clean()
                self.assertIn("duration_seconds", caught.exception.message_dict)
