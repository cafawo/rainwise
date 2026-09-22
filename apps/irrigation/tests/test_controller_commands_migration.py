"""Upgrade keeps actual watering and configuration, removing execution state."""
import datetime as dt
from unittest import mock

from django.core.exceptions import FieldDoesNotExist
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class ControllerCommandsMigrationTests(TransactionTestCase):
    released = ("irrigation", "0006_relay_flash_duration_limits")
    queued = ("irrigation", "0011_controller_commands")
    current = ("irrigation", "0013_in_memory_group_sequences")
    instant = dt.datetime(2026, 9, 21, 6, tzinfo=dt.timezone.utc)

    def migrate(self, target):
        executor = MigrationExecutor(connection)
        executor.migrate([target])
        return executor.loader.project_state([target]).apps

    def restore_schema(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def populate_site(self, apps):
        def model(name):
            return apps.get_model("irrigation", name)

        site = model("Site").objects.create(name="Existing garden", timezone="UTC")
        schedule = model("Schedule").objects.create(site=site, name="Original")
        site.active_schedule = schedule
        site.save(update_fields=["active_schedule"])
        device = model("RelayDevice").objects.create(
            site=site, name="Relay", host="test.invalid",
        )
        valve = model("Valve").objects.create(
            relay_device=device, channel=1, name="Lawn",
            default_max_duration_seconds=2700,
        )
        model("CurveSettings").objects.create(
            site=site, min_mm=1.5, max_mm=8, g=0.4, m=24,
        )
        model("ScheduleRule").objects.create(
            schedule=schedule, valve=valve, mode="FIXED", enabled=True,
            days_of_week_mask=21, start_time=dt.time(8),
            max_duration_seconds=2700, note="Saved 45-minute runtime",
        )
        return site, schedule, valve

    def assert_queue_removed(self, apps):
        run_model = apps.get_model("irrigation", "IrrigationRun")
        for name in (
            "dispatch_state", "sender_interrupted", "occurrence", "pass_number",
            "member_order", "cancellation_requested",
        ):
            with self.assertRaises(FieldDoesNotExist):
                run_model._meta.get_field(name)
        with self.assertRaises(LookupError):
            apps.get_model("irrigation", "ValveClosure")
        self.assertNotIn("irrigation_valveclosure", connection.introspection.table_names())
        with self.assertRaises(LookupError):
            apps.get_model("irrigation", "RuleOccurrence")
        with self.assertRaises(FieldDoesNotExist):
            apps.get_model("irrigation", "Site")._meta.get_field("admission_version")
        self.assertNotIn("irrigation_ruleoccurrence", connection.introspection.table_names())

    def test_released_database_upgrades_with_history_and_fixed_settings_intact(self):
        old = self.migrate(self.released)
        try:
            site, schedule, valve = self.populate_site(old)
            inactive = old.get_model("irrigation", "Schedule").objects.create(
                site=site, name="Inactive",
            )
            dynamic = old.get_model("irrigation", "ScheduleRule").objects.create(
                schedule=inactive, valve=valve, mode="DYNAMIC", enabled=False,
                days_of_week_mask=42, start_time=dt.time(10),
                max_duration_seconds=2700, note="Preserve legacy settings",
            )
            old.get_model("irrigation", "IrrigationRun").objects.create(
                valve=valve, trigger="MANUAL", status="FINISHED",
                requested_start_at=self.instant, actual_start_at=self.instant,
                optimal_duration_seconds=2700, max_duration_seconds=2700,
                actual_stop_at=self.instant + dt.timedelta(seconds=2700),
                stop_reason="COMPLETED", error_message="Original history",
            )
            names = (
                "Site", "Schedule", "RelayDevice", "Valve", "CurveSettings",
                "ScheduleRule", "IrrigationRun",
            )
            originals = {
                name: list(old.get_model("irrigation", name).objects.order_by("pk").values())
                for name in names
            }
            new = self.migrate(self.current)
            for name, records in originals.items():
                fields = tuple(records[0])
                expected = [dict(row) for row in records]
                if name == "ScheduleRule":
                    for row in expected:
                        if row["id"] == dynamic.pk:
                            row["mode"] = "FIXED"
                with self.subTest(model=name):
                    self.assertEqual(
                        list(new.get_model("irrigation", name).objects
                             .order_by("pk").values(*fields)), expected,
                    )
            curve = new.get_model("irrigation", "CurveSettings").objects.get(site_id=site.pk)
            self.assertEqual(curve.fallback_temperature_c, 25)
            self.assertEqual(curve.coverage_days, 2)
            self.assertIsNone(new.get_model("irrigation", "Valve").objects.get(
                pk=valve.pk,
            ).application_rate_mm_h)
            self.assert_queue_removed(new)
        finally:
            self.restore_schema()

    def test_development_upgrade_preserves_completed_smart_history_and_settings(self):
        old = self.migrate(self.queued)
        try:
            site, schedule, valve = self.populate_site(old)
            valve.application_rate_mm_h = 12.5
            valve.save(update_fields=["application_rate_mm_h"])
            old.get_model("irrigation", "CurveSettings").objects.filter(site=site).update(
                coverage_days=3, fallback_temperature_c=22.5,
            )
            rule = old.get_model("irrigation", "GroupedRule").objects.create(
                schedule=schedule, mode="SMART", days_of_week_mask=42,
                start_time=dt.time(10), note="Saved Smart settings",
            )
            old.get_model("irrigation", "GroupedRuleValve").objects.create(
                rule=rule, valve=valve, order=0, duration_seconds=2700,
            )
            occurrence = old.get_model("irrigation", "RuleOccurrence").objects.create(
                site=site, rule=rule, mode="SMART", source="SCHEDULED",
                requested_at=self.instant, decision_at=self.instant,
                scheduled_at=self.instant, scheduled_local_date=self.instant.date(),
                reservation_end=self.instant + dt.timedelta(hours=4),
                status="FINISHED", outcome="Preserve original decision",
                config={"members": [{"valve_id": valve.pk, "duration_seconds": 2700}],
                        "pulse_budget": 3},
                decision={"target_mm": 25, "coverage_days": 3},
            )
            old.get_model("irrigation", "IrrigationRun").objects.create(
                valve=valve, occurrence=occurrence, pass_number=1, member_order=0,
                trigger="SCHEDULED", status="FINISHED", dispatch_state="DONE",
                attempt_started_at=self.instant, actual_start_at=self.instant,
                attempt_finished_at=self.instant + dt.timedelta(seconds=1),
                actual_stop_at=self.instant + dt.timedelta(seconds=2700),
                closure_confirmed_at=self.instant + dt.timedelta(seconds=2701),
                max_duration_seconds=2700, optimal_duration_seconds=2700,
                application_rate_mm_h=12.5, stop_reason="COMPLETED",
                delivery_uncertain=True, sender_interrupted=True,
                error_message="Original uncertain delivery",
            )
            names = (
                "Site", "Schedule", "CurveSettings", "RelayDevice", "Valve",
                "ScheduleRule", "GroupedRule", "GroupedRuleValve",
                "IrrigationRun",
            )
            originals = {}
            for name in names:
                records = list(old.get_model("irrigation", name).objects.order_by("pk").values())
                for row in records:
                    for field in (
                        "dispatch_state", "sender_interrupted", "occurrence_id",
                        "pass_number", "member_order", "cancellation_requested",
                        "admission_version",
                    ):
                        row.pop(field, None)
                    if name == "IrrigationRun":
                        row["trigger"] = "GROUP"
                originals[name] = records
            # Never turn unattempted future pulses into watering history.
            old.get_model("irrigation", "IrrigationRun").objects.create(
                valve=valve, occurrence=occurrence, pass_number=2, member_order=0,
                trigger="SCHEDULED", status="PLANNED", dispatch_state="UNSENT",
                planned_start_at=self.instant + dt.timedelta(hours=1),
                max_duration_seconds=2700, optimal_duration_seconds=2700,
            )
            new = self.migrate(self.current)
            for name, records in originals.items():
                with self.subTest(model=name):
                    self.assertEqual(
                        list(new.get_model("irrigation", name).objects
                             .order_by("pk").values()), records,
                    )
            self.assert_queue_removed(new)
        finally:
            self.restore_schema()

    def test_abandoned_requests_are_retired_without_replay_or_invented_delivery(self):
        old = self.migrate(self.queued)
        try:
            _, _, valve = self.populate_site(old)
            run_model = old.get_model("irrigation", "IrrigationRun")
            runs = {}
            for state in ("QUEUED", "UNSENT", "OPENING", "SENDING"):
                runs[state] = run_model.objects.create(
                    valve=valve, trigger="MANUAL", status="PLANNED",
                    dispatch_state=state, sender_interrupted=state == "SENDING",
                    requested_start_at=self.instant,
                    attempt_started_at=self.instant,
                    max_duration_seconds=2700, optimal_duration_seconds=2700,
                    application_rate_mm_h=12.5, delivery_uncertain=True,
                )
            old.get_model("irrigation", "ValveClosure").objects.create(valve=valve)
            with (
                mock.patch("apps.irrigation.services.open_valve_for") as opening,
                mock.patch("apps.irrigation.services.close_valve") as closing,
                mock.patch("apps.irrigation.services.read_valve_state") as reading,
            ):
                new = self.migrate(self.current)
            opening.assert_not_called()
            closing.assert_not_called()
            reading.assert_not_called()
            run_model = new.get_model("irrigation", "IrrigationRun")
            self.assertEqual(run_model.objects.count(), 4)
            for state, previous in runs.items():
                with self.subTest(state=state):
                    run = run_model.objects.get(pk=previous.pk)
                    self.assertEqual(run.status, "FAILED")
                    self.assertIsNotNone(run.actual_stop_at)
                    self.assertIsNone(run.actual_start_at)
                    self.assertIsNone(run.attempt_finished_at)
                    self.assertEqual(run.optimal_duration_seconds, 2700)
                    self.assertEqual(run.application_rate_mm_h, 12.5)
                    if state in ("QUEUED", "UNSENT"):
                        self.assertEqual(run.stop_reason, "MANUAL_STOP")
                        self.assertIsNone(run.attempt_started_at)
                        self.assertFalse(run.delivery_uncertain)
                        self.assertIn("queued opening cancelled", run.error_message)
                    else:
                        self.assertEqual(run.stop_reason, "ERROR")
                        self.assertEqual(run.attempt_started_at, self.instant)
                        self.assertTrue(run.delivery_uncertain)
                        self.assertIn("delivery is uncertain", run.error_message)
            self.assert_queue_removed(new)
        finally:
            self.restore_schema()

    def test_sequence_cleanup_keeps_uncertain_attempts_and_actual_only_history(self):
        old = self.migrate(("irrigation", "0012_remove_command_queue"))
        try:
            site, schedule, valve = self.populate_site(old)
            rule = old.get_model("irrigation", "GroupedRule").objects.create(
                schedule=schedule, mode="SMART", start_time=dt.time(6),
                days_of_week_mask=127,
            )
            occurrence = old.get_model("irrigation", "RuleOccurrence").objects.create(
                site=site, rule=rule, mode="SMART", source="SCHEDULED",
                requested_at=self.instant, status="ACTIVE",
            )
            runs = old.get_model("irrigation", "IrrigationRun").objects
            uncertain = runs.create(
                valve=valve, occurrence=occurrence, pass_number=1,
                trigger="SCHEDULED", status="FAILED", max_duration_seconds=600,
                attempt_started_at=self.instant, delivery_uncertain=True,
                actual_stop_at=self.instant, application_rate_mm_h=7,
                error_message="Interrupted relay command",
            )
            actual_only = runs.create(
                valve=valve, occurrence=occurrence, pass_number=2,
                trigger="SCHEDULED", status="FINISHED", max_duration_seconds=600,
                actual_start_at=self.instant,
                actual_stop_at=self.instant + dt.timedelta(seconds=600),
            )
            runs.create(
                valve=valve, occurrence=occurrence, pass_number=3,
                trigger="SCHEDULED", status="FAILED", max_duration_seconds=600,
                actual_stop_at=self.instant, error_message="Never attempted",
            )
            with (
                mock.patch("apps.irrigation.services.open_valve_for") as opening,
                mock.patch("apps.irrigation.services.close_valve") as closing,
            ):
                new = self.migrate(self.current)
            opening.assert_not_called()
            closing.assert_not_called()
            runs = new.get_model("irrigation", "IrrigationRun").objects
            self.assertEqual(set(runs.values_list("pk", flat=True)), {
                uncertain.pk, actual_only.pk,
            })
            self.assertEqual(set(runs.values_list("trigger", flat=True)), {"GROUP"})
            kept = runs.get(pk=uncertain.pk)
            self.assertEqual(kept.attempt_started_at, self.instant)
            self.assertIsNone(kept.actual_start_at)
            self.assertIsNone(kept.attempt_finished_at)
            self.assertTrue(kept.delivery_uncertain)
            self.assertIsNone(kept.closure_confirmed_at)
            self.assertEqual(kept.application_rate_mm_h, 7)
            self.assertEqual(kept.error_message, "Interrupted relay command")
            self.assertEqual(runs.get(pk=actual_only.pk).actual_stop_at,
                             self.instant + dt.timedelta(seconds=600))
            self.assert_queue_removed(new)
        finally:
            self.restore_schema()
