import datetime as dt
from unittest import mock

from django.core.exceptions import ValidationError
from django.test import TestCase

from apps.irrigation import group_services
from apps.irrigation.management.commands.controller import Command
from apps.irrigation.models import (
    CurveSettings, GroupedRule, GroupedRuleValve, RelayDevice, Schedule,
    ScheduleRule, Site, Valve,
)


class GroupConfigurationRaceTests(TestCase):
    """Configuration refreshed at admission must still match its owner/minute."""

    def setUp(self):
        self.now = dt.datetime(2026, 9, 21, 6, tzinfo=dt.timezone.utc)
        self.site = Site.objects.create(name="Garden", timezone="UTC")
        self.schedule = Schedule.objects.create(site=self.site, name="Active")
        self.site.active_schedule = self.schedule
        self.site.save(update_fields=["active_schedule"])
        self.device = RelayDevice.objects.create(
            site=self.site, name="Mock relay", host="test.invalid"
        )
        self.valve = Valve.objects.create(
            relay_device=self.device, channel=1, name="Lawn"
        )
        self.rule = GroupedRule.objects.create(
            schedule=self.schedule, mode="FIXED", days_of_week_mask=127,
            start_time=dt.time(6),
        )
        GroupedRuleValve.objects.create(
            rule=self.rule, valve=self.valve, order=0, duration_seconds=60
        )
        self.patches = [
            mock.patch("apps.irrigation.group_services.timezone.now", return_value=self.now),
            mock.patch("apps.irrigation.services.open_valve_for"),
            mock.patch("apps.irrigation.services.close_valve"),
            mock.patch("apps.irrigation.services.read_valve_state", return_value=False),
        ]
        self.clock, self.open, self.close, self.read = [
            patcher.start() for patcher in self.patches
        ]
        for patcher in self.patches:
            self.addCleanup(patcher.stop)

    def admit(self):
        return group_services._plan_occurrence(
            self.rule, scheduled_at=self.now, now=self.now
        )

    def test_rescheduled_group_cannot_start_from_stale_due_selection(self):
        GroupedRule.objects.filter(pk=self.rule.pk).update(start_time=dt.time(7))
        occurrence = self.admit()
        self.assertEqual(occurrence.status, "SKIPPED")
        self.assertFalse(occurrence.runs.exists())
        self.open.assert_not_called()

    def test_weekday_removed_after_selection_records_skip(self):
        GroupedRule.objects.filter(pk=self.rule.pk).update(days_of_week_mask=2)
        occurrence = self.admit()
        self.assertEqual(occurrence.status, "SKIPPED")
        self.assertFalse(occurrence.runs.exists())
        self.open.assert_not_called()

    def test_moving_schedule_and_device_cannot_change_occurrence_site(self):
        occurrence = self.admit()
        self.assertEqual(occurrence.status, "ACTIVE")
        other_site = Site.objects.create(name="Other garden", timezone="UTC")
        Schedule.objects.filter(pk=self.schedule.pk).update(site=other_site)
        RelayDevice.objects.filter(pk=self.device.pk).update(site=other_site)
        group_services._progress(occurrence, set())
        occurrence.refresh_from_db()
        self.assertIn(occurrence.status, ("STOPPING", "CANCELLED"))
        self.assertFalse(occurrence.runs.filter(status="PLANNED").exists())
        self.assertEqual(occurrence.site_id, self.site.pk)
        self.open.assert_not_called()

    def test_moving_rule_to_new_active_schedule_keeps_frozen_owner(self):
        occurrence = self.admit()
        other_schedule = Schedule.objects.create(site=self.site, name="New active")
        GroupedRule.objects.filter(pk=self.rule.pk).update(schedule=other_schedule)
        Site.objects.filter(pk=self.site.pk).update(active_schedule=other_schedule)
        group_services._progress(occurrence, set())
        occurrence.refresh_from_db()
        self.assertIn(occurrence.status, ("STOPPING", "CANCELLED"))
        self.assertFalse(occurrence.runs.filter(status="PLANNED").exists())
        self.assertEqual(occurrence.config["schedule_id"], self.schedule.pk)
        self.open.assert_not_called()

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

    def test_repeated_rest_ticks_do_not_slide_closure_or_write_admission(self):
        self.rule.mode = "SMART"
        self.rule.save()
        self.rule.members.update(duration_seconds=1800)
        self.valve.application_rate_mm_h = 7
        self.valve.save()
        CurveSettings.objects.create(site=self.site, min_mm=7, max_mm=7)
        occurrence = self.admit()
        group_services._progress(occurrence, set())
        first = occurrence.runs.get(pass_number=1)
        closed_at = self.now + dt.timedelta(minutes=30)
        self.clock.return_value = closed_at
        Command()._stop_running_runs(closed_at)
        group_services.group_tick(closed_at)
        first.refresh_from_db()
        self.assertEqual(first.closure_confirmed_at, closed_at)
        eligible = group_services.occurrence_next_eligible_at(occurrence)
        self.assertEqual(eligible, self.now + dt.timedelta(hours=1))
        self.site.refresh_from_db()
        version = self.site.admission_version
        for minutes in (35, 40, 45):
            self.clock.return_value = self.now + dt.timedelta(minutes=minutes)
            group_services._progress(occurrence, set())
            first.refresh_from_db()
            self.site.refresh_from_db()
            self.assertEqual(first.closure_confirmed_at, closed_at)
            self.assertEqual(self.site.admission_version, version)
            self.assertEqual(
                group_services.occurrence_next_eligible_at(occurrence), eligible
            )
        self.open.assert_called_once()
