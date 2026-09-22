"""Validate configured group windows without persistent admission state."""
import datetime as dt

from django.core.exceptions import ValidationError
from django.test import TestCase

from apps.irrigation import group_services
from apps.irrigation.models import (
    GroupedRule, GroupedRuleValve, RelayDevice, Schedule, ScheduleRule, Site, Valve,
)


class GroupConfigurationTests(TestCase):
    def setUp(self):
        self.site = Site.objects.create(name="Garden", timezone="UTC")
        self.schedule = Schedule.objects.create(site=self.site, name="Active")
        self.site.active_schedule = self.schedule
        self.site.save(update_fields=["active_schedule"])
        self.device = RelayDevice.objects.create(
            site=self.site, name="Mock relay", host="test.invalid",
        )
        self.valve = Valve.objects.create(
            relay_device=self.device, channel=1, name="Lawn",
        )
        self.rule = GroupedRule.objects.create(
            schedule=self.schedule, mode="FIXED", days_of_week_mask=127,
            start_time=dt.time(6),
        )
        GroupedRuleValve.objects.create(
            rule=self.rule, valve=self.valve, order=0, duration_seconds=60,
        )

    def test_unavailable_start_marker_does_not_reserve_an_overlapping_window(self):
        self.rule.mode = "SMART"
        self.rule.save()
        ScheduleRule.objects.create(
            schedule=self.schedule, valve=self.valve, mode="FIXED",
            start_time=dt.time(5, 59), days_of_week_mask=127,
            max_duration_seconds=300,
        )
        marker = group_services.reservation_details(self.rule)
        self.assertEqual(marker["total_seconds"], 0)
        self.assertFalse(marker["available"])
        group_services.validate_configuration(self.rule)
        self.valve.application_rate_mm_h = 12
        self.valve.save()
        with self.assertRaisesMessage(ValidationError, "overlaps"):
            group_services.validate_configuration(self.rule)
