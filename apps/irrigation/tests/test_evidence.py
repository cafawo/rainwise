from __future__ import annotations

import datetime as dt
import json
import os
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from apps.irrigation.balance import (
    accounting_windows,
    build_smart_decision,
    irrigation_credit,
    rain_credit,
    temperature_selection,
)
from apps.irrigation.models import CurveSettings, IrrigationRun, RelayDevice, Site
from apps.weather.models import WeatherObservation


UTC = dt.timezone.utc


class DecisionEvidenceTests(TestCase):
    def setUp(self):
        self.site = Site.objects.create(name="Home", timezone="Europe/Berlin")
        self.curve = CurveSettings.objects.create(
            site=self.site, fallback_temperature_c=20,
        )
        self.device = RelayDevice.objects.create(
            site=self.site, name="Relay", host="test.invalid",
        )
        self.valve = self.device.valve_set.create(
            name="Lawn", channel=1, application_rate_mm_h=12,
        )
        self.at = dt.datetime(2026, 7, 8, 4, 30, tzinfo=UTC)
        self.member = SimpleNamespace(
            valve=self.valve, order=0, duration_seconds=900,
        )

    def observation(self, timestamp, **kwargs):
        values = {
            "temperature_c": 0, "precipitation_mm": 0,
            "retrieved_at": self.at,
        }
        values.update(kwargs)
        return WeatherObservation.objects.create(
            site=self.site, timestamp=timestamp, **values,
        )

    def decision(self, **kwargs):
        return build_smart_decision(
            self.site, [self.member], self.at,
            controller_interval_seconds=60, command_allowance_seconds=3,
            **kwargs,
        )

    def test_temperature_samples_preserve_zero_and_reject_untrusted_values(self):
        end = self.at.replace(minute=0)
        for hours in range(18):
            self.observation(end - dt.timedelta(hours=hours))
        for hours, values in (
            (18, {"temperature_c": float("inf")}),
            (19, {"temperature_c": float("nan")}),
            (20, {"retrieved_at": None}),
            (21, {"retrieved_at": self.at + dt.timedelta(hours=1)}),
            (22, {"retrieved_at": end - dt.timedelta(hours=23)}),
        ):
            self.observation(end - dt.timedelta(hours=hours), **values)
        self.observation(end + dt.timedelta(minutes=1), temperature_c=30)
        with patch.dict(os.environ, {"WEATHER_REFRESH_HOURS": "4"}):
            selected = temperature_selection(
                self.site, self.at, include_evidence=True,
            )
        self.assertEqual(selected["temperature_c"], 0)
        self.assertFalse(selected["fallback"])
        self.assertEqual(len(selected["samples"]), 18)
        self.assertEqual(selected["parameters"], {
            "lookback_hours": 24, "percentile": 0.9,
            "minimum_valid_hours": 18, "freshness_hours": 4,
        })
        self.assertEqual(selected["cutoff_at"], self.at.isoformat())
        self.assertEqual(selected["latest_valid_sample"], {
            "timestamp": end.isoformat(),
            "retrieved_at": self.at.isoformat(),
            "temperature_c": 0,
        })
        self.assertTrue(all(row["temperature_c"] == 0 for row in selected["samples"]))
        json.dumps(selected, allow_nan=False)

    def test_fallback_records_missing_inputs_and_old_latest_sample(self):
        old_at = (self.at - dt.timedelta(days=2)).replace(minute=0)
        self.observation(old_at, temperature_c=12)
        selected = temperature_selection(
            self.site, self.at, include_evidence=True,
        )
        self.assertTrue(selected["fallback"])
        self.assertEqual(selected["temperature_c"], 20)
        self.assertEqual(selected["fallback_temperature_c"], 20)
        self.assertEqual(selected["samples"], [])
        self.assertIn("Only 0", selected["reason"])
        self.assertEqual(
            selected["latest_valid_sample"]["timestamp"], old_at.isoformat(),
        )
        self.assertEqual(selected["latest_valid_sample"]["temperature_c"], 12)
        self.curve.fallback_temperature_c = float("nan")
        selected = temperature_selection(
            self.site, self.at, settings=self.curve, include_evidence=True,
        )
        self.assertIsNone(selected["temperature_c"])
        self.assertIsNone(selected["fallback_temperature_c"])
        json.dumps(selected, allow_nan=False)

    def test_rain_evidence_tracks_accepted_samples_and_missing_hours_across_dst(self):
        for date, expected in (
            (dt.date(2026, 3, 30), 47), (dt.date(2026, 10, 26), 49),
        ):
            with self.subTest(date=date):
                at = dt.datetime.combine(
                    date, dt.time(6, 30), tzinfo=ZoneInfo(self.site.timezone),
                )
                windows = accounting_windows(self.site, at, 2)
                for hour in range(1, expected + 1):
                    self.observation(
                        windows["rain_start"] + dt.timedelta(hours=hour),
                        precipitation_mm=0.5, retrieved_at=at,
                    )
                WeatherObservation.objects.filter(
                    site=self.site, timestamp=windows["rain_end"],
                ).update(precipitation_mm=None)
                rain = rain_credit(self.site, at, 2, include_evidence=True)
                self.assertEqual(rain["expected_hours"], expected)
                self.assertEqual(rain["missing_hours"], 1)
                self.assertTrue(rain["warning"])
                self.assertEqual(len(rain["samples"]), expected - 1)
                self.assertEqual(
                    rain["credit_mm"],
                    sum(row["precipitation_mm"] for row in rain["samples"]),
                )
                self.assertTrue(all(
                    row["retrieved_at"] == at.astimezone(UTC).isoformat()
                    for row in rain["samples"]
                ))

    def test_irrigation_contributions_keep_intervals_unknowns_and_uncertainty(self):
        midnight = dt.datetime.combine(
            self.at.astimezone(ZoneInfo(self.site.timezone)).date(),
            dt.time.min, tzinfo=ZoneInfo(self.site.timezone),
        )
        calibrated = IrrigationRun.objects.create(
            valve=self.valve, trigger="MANUAL", status="FINISHED",
            actual_start_at=midnight - dt.timedelta(minutes=5),
            actual_stop_at=midnight + dt.timedelta(minutes=5),
            optimal_duration_seconds=600, max_duration_seconds=600,
            application_rate_mm_h=12, appendix={"old": "excluded"},
        )
        uncalibrated = IrrigationRun.objects.create(
            valve=self.valve, trigger="MANUAL", status="FINISHED",
            actual_start_at=midnight, optimal_duration_seconds=600,
            max_duration_seconds=600, application_rate_mm_h=None,
        )
        ambiguous = IrrigationRun.objects.create(
            valve=self.valve, trigger="MANUAL", status="FAILED",
            attempt_started_at=self.at - dt.timedelta(seconds=30),
            max_duration_seconds=600, application_rate_mm_h=12,
            delivery_uncertain=True,
        )
        with CaptureQueriesContext(connection) as captured:
            credit = irrigation_credit(
                self.valve, self.at, 1, include_evidence=True,
            )
        history_sql = [
            item["sql"] for item in captured
            if "irrigation_irrigationrun" in item["sql"]
        ]
        self.assertEqual(len(history_sql), 1)
        self.assertNotIn("appendix", history_sql[0])
        evidence = {row["run_id"]: row for row in credit["contributions"]}
        self.assertEqual(evidence[calibrated.pk]["credit_mm"], 1)
        self.assertEqual(evidence[calibrated.pk]["application_rate_mm_h"], 12)
        self.assertEqual(
            evidence[calibrated.pk]["credited_start_at"],
            midnight.astimezone(UTC).isoformat(),
        )
        self.assertIsNone(evidence[uncalibrated.pk]["credit_mm"])
        self.assertIsNone(evidence[uncalibrated.pk]["application_rate_mm_h"])
        self.assertFalse(evidence[uncalibrated.pk]["calibrated"])
        self.assertTrue(evidence[ambiguous.pk]["uncertain"])
        self.assertTrue(evidence[ambiguous.pk]["unknown_extra_delivery"])
        self.assertTrue(evidence[ambiguous.pk]["nominal_allowance_beyond_cutoff"])
        self.assertEqual(evidence[ambiguous.pk]["credit_mm"], 2)
        self.assertEqual(credit["credit_mm"], sum(
            row["credit_mm"] for row in evidence.values()
            if row["credit_mm"] is not None
        ))
        self.assertTrue(all("appendix" not in row for row in evidence.values()))
        json.dumps(credit, allow_nan=False)

    def test_decision_evidence_uses_effective_settings_and_remains_a_snapshot(self):
        observation = self.observation(self.at.replace(minute=0))
        effective = CurveSettings.objects.get(pk=self.curve.pk)
        effective.min_mm = 1
        effective.max_mm = 5
        effective.g = 0.2
        effective.m = 19
        effective.coverage_days = 1
        effective.fallback_temperature_c = 18
        with patch(
            "apps.irrigation.balance.get_curve_settings", return_value=effective,
        ) as settings_read:
            decision = self.decision(include_evidence=True)
        settings_read.assert_called_once_with(self.site)
        self.assertEqual(decision["settings"], {
            "min_mm": 1, "max_mm": 5, "g": 0.2, "m": 19,
            "coverage_days": 1, "fallback_temperature_c": 18,
        })
        self.assertEqual(decision["temperature"]["temperature_c"], 18)
        self.assertEqual(decision["calculation_version"], 1)
        self.assertEqual(decision["sequence_options"]["command_allowance_seconds"], 3)
        serialized = json.dumps(decision, sort_keys=True, allow_nan=False)
        effective.fallback_temperature_c = 50
        self.curve.max_mm = 10
        self.curve.save()
        observation.temperature_c = 40
        observation.precipitation_mm = 2
        observation.save()
        self.valve.name = "Renamed"
        self.valve.application_rate_mm_h = 24
        self.valve.save()
        self.assertEqual(
            json.dumps(decision, sort_keys=True, allow_nan=False), serialized,
        )

    def test_default_results_omit_evidence_and_preserve_calculation(self):
        plain = self.decision()
        recorded = self.decision(include_evidence=True)
        for key in (
            "daily_need_mm", "coverage_days", "sequence", "peak_sequence",
            "warnings",
        ):
            self.assertEqual(plain[key], recorded[key])
        self.assertNotIn("settings", plain)
        self.assertNotIn("samples", plain["temperature"])
        self.assertNotIn("samples", plain["rain"])
        valve_key = str(self.valve.pk)
        self.assertNotIn("contributions", plain["valves"][valve_key]["irrigation"])
        self.assertEqual(
            plain["valves"][valve_key]["planned_seconds"],
            recorded["valves"][valve_key]["planned_seconds"],
        )

    def test_warnings_are_separated_per_valve_for_run_snapshots(self):
        other = self.device.valve_set.create(
            name="Other", channel=2, application_rate_mm_h=12,
        )
        IrrigationRun.objects.create(
            valve=other, trigger="MANUAL", status="FINISHED",
            actual_start_at=self.at - dt.timedelta(hours=1),
            max_duration_seconds=600,
        )
        decision = build_smart_decision(
            self.site, [self.member, SimpleNamespace(
                valve=other, order=1, duration_seconds=900,
            )], self.at, include_evidence=True,
        )
        self.assertEqual(decision["valves"][str(self.valve.pk)]["warnings"], [])
        self.assertTrue(all(
            value.startswith("Other:")
            for value in decision["valves"][str(other.pk)]["warnings"]
        ))
        self.assertEqual(
            decision["warnings"], decision["shared_warnings"]
            + decision["valves"][str(other.pk)]["warnings"],
        )
