import datetime as dt
from unittest import mock

from django.test import TestCase

from apps.irrigation import group_services
from apps.irrigation.balance import delivery_estimate
from apps.irrigation.models import (
    GroupedRule, GroupedRuleValve, RelayDevice, Schedule,
    ScheduleRule, Site, Valve,
)


class ExecutionFixtures:
    @property
    def now(self):
        return self._now

    @now.setter
    def now(self, value):
        self._now = value
        # The relay timer runs independently of controller polling or reads.
        for valve_id, deadline in list(getattr(self, "relay_deadlines", {}).items()):
            if value >= deadline:
                self.physical[valve_id] = False
                del self.relay_deadlines[valve_id]

    def open_timed_valve(self, valve, duration):
        self.physical[valve.pk] = True
        self.relay_deadlines[valve.pk] = self.now + dt.timedelta(seconds=duration)

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
        self.relay_deadlines = {}
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
        self.open.side_effect = self.open_timed_valve
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


class ExistingSingleValveBehaviorTests(ExecutionFixtures, TestCase):
    def test_scheduled_start_deduplicates_same_valve_and_minute(self):
        rule = self.fixed_rule()
        first = group_services.start_single(
            self.valve, 600, "SCHEDULED", planned_start_at=self.now, rule=rule,
        )
        second = group_services.start_single(
            self.valve, 600, "SCHEDULED", planned_start_at=self.now, rule=rule,
        )
        self.assertEqual(first.pk, second.pk)
        self.open.assert_called_once_with(self.valve, 600)

    def test_fixed_run_now_accepts_inactive_disabled_rule(self):
        rule = self.fixed_rule()
        rule.enabled = False
        rule.save(update_fields=["enabled"])
        self.site.active_schedule = None
        self.site.save(update_fields=["active_schedule"])
        group_services.start_single(self.valve, 600, "MANUAL", rule=rule)
        self.open.assert_called_once_with(self.valve, 600)

    def test_fixed_run_now_retains_existing_overlap_behavior(self):
        rule = self.fixed_rule()
        first = group_services.start_single(self.valve, 600, "MANUAL")
        second = group_services.start_single(self.valve, 600, "MANUAL", rule=rule)
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(self.open.call_count, 2)

    def test_later_fixed_schedule_retains_existing_overlap_behavior(self):
        rule = self.fixed_rule()
        first = group_services.start_single(
            self.valve, 600, "SCHEDULED", planned_start_at=self.now, rule=rule,
        )
        self.now += dt.timedelta(minutes=1)
        second = group_services.start_single(
            self.valve, 600, "SCHEDULED", planned_start_at=self.now, rule=rule,
        )
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(self.open.call_count, 2)

    def test_acknowledged_manual_stop_keeps_known_shortened_delivery(self):
        run = group_services.start_single(self.valve, 600, "MANUAL")
        self.now += dt.timedelta(seconds=120)
        group_services.close_member(self.valve)
        run.refresh_from_db()
        self.assertFalse(run.delivery_uncertain)
        self.assertAlmostEqual(delivery_estimate(run)["estimated_mm"], 0.4)
