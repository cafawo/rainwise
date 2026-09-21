from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from apps.irrigation.balance import (
    accounting_windows,
    build_smart_decision,
    delivery_estimate,
    irrigation_credit,
    plan_dose,
    rain_credit,
    temperature_selection,
)
from apps.irrigation.curves import daily_water_required
from apps.irrigation.models import (
    CurveSettings,
    DEFAULT_FALLBACK_TEMPERATURE_C,
    GroupedRule,
    GroupedRuleValve,
    IrrigationRun,
    RelayDevice,
    RuleOccurrence,
    Schedule,
    ScheduleRule,
    Site,
    get_curve_settings,
)
from apps.weather.models import WeatherObservation


UTC = dt.timezone.utc


class BalanceTests(TestCase):
    def setUp(self):
        self.site = Site.objects.create(name="Home", timezone="Europe/Berlin")
        self.curve = CurveSettings.objects.create(site=self.site, fallback_temperature_c=20)
        device = RelayDevice.objects.create(site=self.site, name="Relay", host="test.invalid")
        self.valve = device.valve_set.create(name="Lawn", channel=1, application_rate_mm_h=12)
        self.at = dt.datetime(2026, 7, 8, 4, 30, tzinfo=UTC)

    def run_record(self, start, seconds, **kwargs):
        return IrrigationRun.objects.create(
            valve=self.valve, trigger="MANUAL", status="FINISHED",
            actual_start_at=start, actual_stop_at=start + dt.timedelta(seconds=seconds),
            optimal_duration_seconds=seconds, max_duration_seconds=seconds,
            application_rate_mm_h=12, **kwargs,
        )

    def hourly(self, start, end, **kwargs):
        values = {"temperature_c": 20, "precipitation_mm": 0, "retrieved_at": self.at}
        values.update(kwargs)
        rows = []
        while start <= end:
            rows.append(WeatherObservation(site=self.site, timestamp=start, **values))
            start += dt.timedelta(hours=1)
        WeatherObservation.objects.bulk_create(rows)

    def test_multiday_sequences_and_independent_zone_rates(self):
        for daily_need, expected in ((2, [4, 0, 4, 0]), (4, [6, 2, 6, 2]), (7, [6, 6, 6, 6])):
            previous = 0
            delivered = []
            for _ in range(4):
                result = plan_dose(daily_need, 2, 0, previous, 12, 900)
                previous = result["estimated_delivery_mm"]
                delivered.append(previous)
            self.assertEqual(delivered, expected)
        self.assertEqual(plan_dose(2, 2, 3, 0, 12, 900)["planned_seconds"], 300)
        self.assertEqual(plan_dose(4, 2, 0, 4, 12, 900)["planned_seconds"], 1200)
        self.assertEqual(plan_dose(2, 2, 0, 0, 6, 900)["estimated_delivery_mm"], 3)
        self.assertEqual(plan_dose(2, 1, 0, 0, 12, 900)["pulse_seconds"], [600])
        self.assertEqual(plan_dose(2, 7, 0, 0, 12, 900)["unmet_mm"], 8)
        self.assertEqual(plan_dose(2, 2, 4, 0, 12, 900)["pulse_seconds"], [])
        self.assertEqual(plan_dose(7, 2, 0, 0, 1.1, 1)["planned_seconds"], 2)

    def test_invalid_and_nonfinite_inputs_fail(self):
        for days in (0, 8, 1.5, True):
            with self.assertRaises(ValueError):
                plan_dose(2, days, 0, 0, 12, 900)
        for rate in (0, -1, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                plan_dose(2, 2, 0, 0, rate, 900)
        for value in (float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                daily_water_required(value)
        self.assertEqual(daily_water_required(-1e200), 0)
        self.assertEqual(daily_water_required(1e200), 7)
        self.curve.coverage_days = 1.5
        with self.assertRaises(ValidationError):
            self.curve.full_clean()

    def test_calendar_credit_expires_monday_on_wednesday(self):
        self.run_record(dt.datetime(2026, 7, 6, 4, 1, tzinfo=UTC), 1200)
        self.assertEqual(irrigation_credit(self.valve, self.at, 2)["credit_mm"], 0)
        self.run_record(dt.datetime(2026, 7, 7, 4, 1, tzinfo=UTC), 600)
        self.run_record(self.at - dt.timedelta(minutes=5), 120)
        self.assertAlmostEqual(irrigation_credit(self.valve, self.at, 2)["credit_mm"], 2.4)
        self.assertAlmostEqual(irrigation_credit(self.valve, self.at, 1)["credit_mm"], 0.4)

    def test_delivery_crossing_midnight_and_cutoff_is_intersected(self):
        local_midnight = dt.datetime(2026, 7, 8, tzinfo=ZoneInfo("Europe/Berlin"))
        self.run_record(local_midnight - dt.timedelta(minutes=5), 600)
        self.assertEqual(irrigation_credit(self.valve, self.at, 1)["credit_mm"], 1)
        run = self.run_record(self.at - dt.timedelta(seconds=30), 60)
        self.assertEqual(delivery_estimate(run, self.at)["estimated_mm"], 0.1)

    def test_rate_snapshot_and_delayed_stop_bookkeeping(self):
        run = self.run_record(self.at - dt.timedelta(minutes=20), 600)
        run.actual_stop_at += dt.timedelta(minutes=5)
        run.save()
        self.valve.application_rate_mm_h = 24
        self.valve.save()
        self.assertEqual(delivery_estimate(run)["estimated_mm"], 2)
        run.actual_stop_at = run.actual_start_at + dt.timedelta(seconds=60)
        self.assertEqual(delivery_estimate(run)["estimated_mm"], 0.2)
        run.application_rate_mm_h = None
        self.assertIsNone(delivery_estimate(run)["estimated_mm"])

    def test_uncertain_command_credits_nominal_and_retry_interval(self):
        start = self.at - dt.timedelta(minutes=30)
        run = IrrigationRun.objects.create(
            valve=self.valve, trigger="MANUAL", status="FAILED",
            attempt_started_at=start, attempt_finished_at=start + dt.timedelta(seconds=10),
            max_duration_seconds=600, application_rate_mm_h=12, delivery_uncertain=True,
        )
        estimate = delivery_estimate(run)
        self.assertTrue(estimate["uncertain"])
        self.assertAlmostEqual(estimate["estimated_mm"], 610 * 12 / 3600)
        run.attempt_finished_at = None
        estimate = delivery_estimate(run)
        self.assertEqual(estimate["estimated_mm"], 2)
        self.assertTrue(estimate["unknown_extra_delivery"])

    def test_recovered_uncertain_command_keeps_full_nominal_cutoff_credit(self):
        start = self.at - dt.timedelta(seconds=80)
        run = IrrigationRun.objects.create(
            valve=self.valve, trigger="MANUAL", status="FAILED",
            attempt_started_at=start,
            attempt_finished_at=start + dt.timedelta(seconds=5),
            actual_stop_at=self.at - dt.timedelta(seconds=10),
            closure_confirmed_at=self.at - dt.timedelta(seconds=10),
            max_duration_seconds=900, application_rate_mm_h=12,
            delivery_uncertain=True,
        )
        estimate = delivery_estimate(run, cutoff=self.at)
        self.assertAlmostEqual(estimate["estimated_mm"], 905 * 12 / 3600)
        self.assertTrue(estimate["nominal_allowance_beyond_cutoff"])
        credit = irrigation_credit(self.valve, self.at, 1)
        self.assertAlmostEqual(credit["credit_mm"], 905 * 12 / 3600)
        self.assertTrue(credit["nominal_allowance_beyond_cutoff"])
        decision = build_smart_decision(
            self.site,
            [SimpleNamespace(valve=self.valve, order=0, duration_seconds=900)],
            self.at,
        )
        self.assertTrue(any("extends beyond" in value for value in decision["warnings"]))

    def test_uncertain_nominal_credit_still_splits_at_local_midnight(self):
        midnight = dt.datetime(2026, 7, 8, tzinfo=ZoneInfo(self.site.timezone))
        at = midnight + dt.timedelta(seconds=20)
        start = midnight - dt.timedelta(seconds=60)
        IrrigationRun.objects.create(
            valve=self.valve, trigger="MANUAL", status="FAILED",
            attempt_started_at=start, attempt_finished_at=start,
            actual_stop_at=midnight + dt.timedelta(seconds=10),
            closure_confirmed_at=midnight + dt.timedelta(seconds=10),
            max_duration_seconds=900, application_rate_mm_h=12,
            delivery_uncertain=True,
        )
        self.assertEqual(irrigation_credit(self.valve, at, 1)["credit_mm"], 2.8)
        self.assertEqual(irrigation_credit(self.valve, at, 2)["credit_mm"], 3)

    def test_daylight_saving_windows_use_calendar_days(self):
        for date, hours in ((dt.date(2026, 3, 30), 47), (dt.date(2026, 10, 26), 49)):
            at = dt.datetime.combine(date, dt.time(6, 30), tzinfo=ZoneInfo(self.site.timezone))
            windows = accounting_windows(self.site, at, 2)
            self.assertEqual((windows["rain_end"] - windows["rain_start"]).total_seconds() / 3600, hours)
            self.assertEqual(windows["irrigation_start"].astimezone(ZoneInfo(self.site.timezone)).hour, 0)

    def test_rain_preceding_hour_boundary_counts_two_full_days(self):
        end = self.at.replace(minute=0)
        self.hourly(end - dt.timedelta(hours=48), end, precipitation_mm=0.5)
        rain = rain_credit(self.site, self.at, 2)
        self.assertEqual(rain["expected_hours"], 48)
        self.assertEqual(rain["credit_mm"], 24)
        self.assertEqual(rain["missing_hours"], 0)
        WeatherObservation.objects.filter(timestamp=end).update(retrieved_at=None)
        rain = rain_credit(self.site, self.at, 2)
        self.assertEqual(rain["credit_mm"], 23.5)
        self.assertEqual(rain["missing_hours"], 1)

    def test_temperature_fallback_sparse_future_and_unknown_provenance(self):
        end = self.at.replace(minute=0)
        self.hourly(end - dt.timedelta(hours=17), end)
        selected = temperature_selection(self.site, self.at)
        self.assertFalse(selected["fallback"])
        self.assertEqual(selected["valid_hours"], 18)
        WeatherObservation.objects.filter(timestamp=end).update(retrieved_at=None)
        self.hourly(end + dt.timedelta(hours=1), end + dt.timedelta(hours=20), temperature_c=50)
        selected = temperature_selection(self.site, self.at)
        self.assertTrue(selected["fallback"])
        self.assertEqual(selected["temperature_c"], 20)
        self.assertEqual(selected["valid_hours"], 17)
        WeatherObservation.objects.filter(timestamp=end - dt.timedelta(hours=1)).update(
            retrieved_at=end - dt.timedelta(hours=2)
        )
        self.assertEqual(temperature_selection(self.site, self.at)["valid_hours"], 16)

    def test_temperature_rejects_subhour_values_and_exposes_old_valid_time(self):
        start = self.at - dt.timedelta(hours=1)
        for minute in range(20):
            WeatherObservation.objects.create(
                site=self.site, timestamp=start + dt.timedelta(minutes=minute),
                temperature_c=40, retrieved_at=self.at,
            )
        old_time = (self.at - dt.timedelta(days=2)).replace(minute=0)
        WeatherObservation.objects.create(
            site=self.site, timestamp=old_time, temperature_c=10,
            retrieved_at=self.at,
        )
        selected = temperature_selection(self.site, self.at)
        self.assertEqual(selected["valid_hours"], 0)
        self.assertEqual(selected["latest_valid_at"], old_time.isoformat())
        self.assertTrue(selected["fallback"])

    def test_stale_weather_and_resolved_history_warning(self):
        end = self.at.replace(minute=0) - dt.timedelta(hours=6)
        self.hourly(end - dt.timedelta(hours=17), end)
        selected = temperature_selection(self.site, self.at)
        self.assertEqual(selected["valid_hours"], 18)
        self.assertTrue(selected["fallback"])
        self.assertIn("older", selected["reason"])
        self.assertTrue(irrigation_credit(self.valve, self.at, 2)["incomplete_history"])
        RuleOccurrence.objects.create(
            site=self.site, mode="SMART", source="SCHEDULED", status="ZERO",
            requested_at=self.at - dt.timedelta(days=3),
            decision_at=self.at - dt.timedelta(days=3),
        )
        self.assertFalse(irrigation_credit(self.valve, self.at, 2)["incomplete_history"])

    def test_build_decision_uses_shared_fallback_and_first_activation_warning(self):
        members = [SimpleNamespace(valve=self.valve, order=0, duration_seconds=900)]
        decision = build_smart_decision(self.site, members, self.at)
        self.assertTrue(decision["temperature"]["fallback"])
        self.assertTrue(decision["rain"]["warning"])
        self.assertTrue(decision["valves"][str(self.valve.pk)]["irrigation"]["incomplete_history"])
        self.assertGreater(decision["valves"][str(self.valve.pk)]["planned_seconds"], 0)
        self.curve.fallback_temperature_c = float("inf")
        self.curve.save()
        with self.assertRaises(ValidationError):
            build_smart_decision(self.site, members, self.at)

    def test_missing_curve_row_uses_defaults_without_writes(self):
        self.curve.delete()
        settings = get_curve_settings(self.site)
        self.assertIsNone(settings.pk)
        self.assertEqual(settings.fallback_temperature_c, 25)
        self.assertEqual(settings.coverage_days, 2)
        selection = temperature_selection(self.site, self.at)
        self.assertEqual(selection["temperature_c"], 25)
        decision = build_smart_decision(
            self.site,
            [SimpleNamespace(valve=self.valve, order=0, duration_seconds=900)],
            self.at,
        )
        self.assertEqual(decision["temperature"]["temperature_c"], 25)
        self.assertGreater(
            decision["valves"][str(self.valve.pk)]["planned_seconds"], 0
        )
        self.assertFalse(CurveSettings.objects.filter(site=self.site).exists())

    def test_new_site_and_saved_fallback_overrides_include_zero(self):
        site = Site.objects.create(name="New garden", timezone="UTC")
        self.assertEqual(
            get_curve_settings(site).fallback_temperature_c,
            DEFAULT_FALLBACK_TEMPERATURE_C,
        )
        self.assertEqual(
            temperature_selection(site, self.at)["temperature_c"], 25
        )
        self.assertFalse(CurveSettings.objects.filter(site=site).exists())
        settings = CurveSettings.objects.create(site=site)
        self.assertEqual(settings.fallback_temperature_c, 25)
        for value in (0, 18.5, -5):
            settings.fallback_temperature_c = value
            settings.full_clean()
            settings.save()
            self.assertEqual(
                temperature_selection(site, self.at)["temperature_c"], value
            )
            self.assertEqual(get_curve_settings(site).pk, settings.pk)

    def test_missing_rate_skips_only_affected_member_and_restoration_clears_warning(self):
        other = self.valve.relay_device.valve_set.create(
            name="Uncalibrated", channel=2
        )
        members = [
            SimpleNamespace(valve=other, order=0, duration_seconds=900),
            SimpleNamespace(valve=self.valve, order=1, duration_seconds=900),
        ]
        decision = build_smart_decision(self.site, members, self.at)
        skipped = decision["valves"][str(other.pk)]
        self.assertTrue(skipped["skipped"])
        self.assertEqual(skipped["planned_seconds"], 0)
        self.assertEqual(skipped["pulse_seconds"], [])
        self.assertIsNone(skipped["target_mm"])
        self.assertIsNone(skipped["application_rate_mm_h"])
        self.assertIn("watering rate", skipped["skip_reason"])
        calibrated = decision["valves"][str(self.valve.pk)]
        self.assertFalse(calibrated["skipped"])
        self.assertGreater(calibrated["planned_seconds"], 0)
        self.assertEqual(calibrated["order"], 1)
        other.application_rate_mm_h = 12
        other.save()
        restored = build_smart_decision(self.site, members, self.at)
        self.assertFalse(restored["valves"][str(other.pk)]["skipped"])
        self.assertFalse(any(
            "watering rate is required" in value
            for value in restored["warnings"]
        ))
        self.assertTrue(decision["valves"][str(other.pk)]["skipped"])

    def test_all_missing_or_invalid_rates_produce_json_safe_skip_decisions(self):
        members = [
            SimpleNamespace(valve=self.valve, order=0, duration_seconds=900)
        ]
        for rate in (None, 0, -1, float("inf"), float("nan")):
            with self.subTest(rate=rate):
                self.valve.application_rate_mm_h = rate
                self.assertFalse(self.valve.has_valid_application_rate)
                decision = build_smart_decision(self.site, members, self.at)
                self.assertTrue(all(
                    item["skipped"] for item in decision["valves"].values()
                ))
                json.dumps(decision, allow_nan=False)

    def test_clearing_current_rate_preserves_calibrated_delivery_history(self):
        run = self.run_record(self.at - dt.timedelta(minutes=20), 600)
        self.valve.application_rate_mm_h = None
        self.valve.full_clean()
        self.valve.save()
        run.refresh_from_db()
        self.assertEqual(run.application_rate_mm_h, 12)
        self.assertEqual(delivery_estimate(run)["estimated_mm"], 2)
        self.assertEqual(
            irrigation_credit(self.valve, self.at, 1)["credit_mm"], 2
        )


class GroupModelTests(TestCase):
    def setUp(self):
        self.site = Site.objects.create(name="Home")
        self.schedule = Schedule.objects.create(site=self.site, name="Summer")
        self.device = RelayDevice.objects.create(site=self.site, name="Relay", host="test.invalid")
        self.valve = self.device.valve_set.create(name="Lawn", channel=1)
        self.rule = GroupedRule.objects.create(schedule=self.schedule, mode="FIXED", start_time="06:00")

    def test_membership_and_occurrence_constraints_and_preserved_history(self):
        GroupedRuleValve.objects.create(rule=self.rule, valve=self.valve, order=0, duration_seconds=120)
        with self.assertRaises(IntegrityError), transaction.atomic():
            GroupedRuleValve.objects.create(rule=self.rule, valve=self.valve, order=1, duration_seconds=120)
        at = dt.datetime(2026, 7, 8, 4, tzinfo=UTC)
        occurrence = RuleOccurrence.objects.create(
            site=self.site, rule=self.rule, mode="FIXED", requested_at=at,
            source="SCHEDULED", scheduled_local_date=at.date(), config={"saved": True},
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            RuleOccurrence.objects.create(
                site=self.site, rule=self.rule, mode="FIXED", requested_at=at,
                source="SCHEDULED", scheduled_local_date=at.date(),
            )
        self.rule.delete()
        occurrence.refresh_from_db()
        self.assertIsNone(occurrence.rule_id)
        self.assertEqual(occurrence.config, {"saved": True})

    def test_fixed_legacy_spelling_saves_and_displays_fixed(self):
        rule = ScheduleRule(
            schedule=self.schedule, valve=self.valve, mode="DYNAMIC", start_time="06:00",
            days_of_week_mask=1, max_duration_seconds=120,
        )
        self.assertEqual(rule.get_mode_display(), "Fixed")
        rule.full_clean()
        rule.save()
        self.assertEqual(ScheduleRule.objects.get(pk=rule.pk).mode, "FIXED")
        self.assertEqual(ScheduleRule.MODE_CHOICES, [("FIXED", "Fixed")])
        ScheduleRule.objects.filter(pk=rule.pk).update(mode="DYNAMIC")
        rule.refresh_from_db()
        rule.note = "Edited"
        rule.save(update_fields=["note"])
        self.assertEqual(ScheduleRule.objects.get(pk=rule.pk).mode, "FIXED")

    def test_fixed_has_no_calibration_or_valve_maximum_requirement(self):
        member = GroupedRuleValve(rule=self.rule, valve=self.valve, order=0, duration_seconds=3000)
        member.full_clean()
        self.rule.mode = "SMART"
        self.rule.save()
        with self.assertRaises(ValidationError):
            member.full_clean()

    def test_existing_smart_membership_survives_missing_rate_but_new_selection_fails(self):
        self.rule.mode = "SMART"
        self.rule.save()
        member = GroupedRuleValve(
            rule=self.rule, valve=self.valve, order=0, duration_seconds=900
        )
        with self.assertRaisesMessage(ValidationError, "watering rate"):
            member.full_clean()
        self.valve.application_rate_mm_h = 12
        self.valve.save()
        member.full_clean()
        member.save()
        self.valve.application_rate_mm_h = None
        self.valve.full_clean()
        self.valve.save()
        member.duration_seconds = 600
        member.full_clean()
        member.save()
        self.assertEqual(
            GroupedRuleValve.objects.get(pk=member.pk).valve_id, self.valve.pk
        )
        other = self.device.valve_set.create(name="No rate", channel=2)
        member.valve = other
        with self.assertRaisesMessage(ValidationError, "watering rate"):
            member.full_clean()

    def test_fixed_membership_conversion_requires_calibration(self):
        member = GroupedRuleValve.objects.create(
            rule=self.rule, valve=self.valve, order=0, duration_seconds=600
        )
        self.rule.mode = "SMART"
        member.rule = self.rule
        with self.assertRaisesMessage(ValidationError, "watering rate"):
            member.full_clean()
