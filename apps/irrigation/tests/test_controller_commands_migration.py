import datetime as dt
import io
from unittest import mock

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class ControllerCommandsMigrationTests(TransactionTestCase):
    before = ("irrigation", "0010_irrigationrun_dispatch_state")
    after = ("irrigation", "0011_controller_commands")
    instant = dt.datetime(2026, 9, 21, 6, tzinfo=dt.timezone.utc)

    def migrate(self, target):
        executor = MigrationExecutor(connection)
        executor.migrate([target])
        return executor.loader.project_state([target]).apps

    def restore_schema(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def populate_legacy(self, apps):
        def model(name):
            return apps.get_model("irrigation", name)

        site = model("Site").objects.create(name="Legacy", timezone="UTC")
        schedule = model("Schedule").objects.create(site=site, name="Original")
        site.active_schedule = schedule
        site.save(update_fields=["active_schedule"])
        model("CurveSettings").objects.create(
            site=site, coverage_days=3, fallback_temperature_c=22.5,
        )
        device = model("RelayDevice").objects.create(
            site=site, name="Mock relay", host="test.invalid",
        )
        valves = [model("Valve").objects.create(
            relay_device=device, channel=index + 1, name=f"Valve {index}",
            default_max_duration_seconds=600, application_rate_mm_h=12.5,
        ) for index in range(4)]
        model("ScheduleRule").objects.create(
            schedule=schedule, valve=valves[0], mode="FIXED",
            days_of_week_mask=21, start_time=dt.time(8),
            max_duration_seconds=2700, note="Saved 45-minute override",
        )
        rule = model("GroupedRule").objects.create(
            schedule=schedule, mode="SMART", days_of_week_mask=42,
            start_time=dt.time(10), note="Saved Smart settings",
        )
        model("GroupedRuleValve").objects.create(
            rule=rule, valve=valves[0], order=0, duration_seconds=2700,
        )
        occurrence = model("RuleOccurrence").objects.create(
            site=site, rule=rule, mode="SMART", source="SCHEDULED",
            requested_at=self.instant, decision_at=self.instant,
            scheduled_at=self.instant, scheduled_local_date=self.instant.date(),
            reservation_end=self.instant + dt.timedelta(hours=4),
            status="FINISHED", outcome="Preserve original decision",
            config={"members": [{"valve_id": valves[0].pk,
                                  "duration_seconds": 2700}],
                    "pulse_budget": 3},
            decision={"target_mm": 25, "coverage_days": 3},
        )
        run_model = model("IrrigationRun")
        for valve, dispatch, status in zip(
            valves,
            ("UNSENT", "SENDING", "LEGACY", "LEGACY"),
            ("PLANNED", "PLANNED", "PLANNED", "RUNNING"),
        ):
            run_model.objects.create(
                valve=valve, trigger="MANUAL", status=status,
                dispatch_state=dispatch,
                requested_start_at=self.instant,
                attempt_started_at=self.instant if status == "PLANNED" else None,
                actual_start_at=self.instant if status == "RUNNING" else None,
                max_duration_seconds=2700, optimal_duration_seconds=2700,
                application_rate_mm_h=12.5,
                delivery_uncertain=status == "PLANNED",
                sender_interrupted=dispatch == "SENDING",
                error_message="Original legacy sender history",
            )
        run_model.objects.create(
            valve=valves[0], occurrence=occurrence, pass_number=1, member_order=0,
            trigger="SCHEDULED", status="FINISHED", dispatch_state="DONE",
            requested_start_at=self.instant - dt.timedelta(days=1),
            planned_start_at=self.instant - dt.timedelta(days=1),
            attempt_started_at=self.instant - dt.timedelta(days=1),
            attempt_finished_at=self.instant - dt.timedelta(days=1)
            + dt.timedelta(seconds=1),
            actual_start_at=self.instant - dt.timedelta(days=1),
            actual_stop_at=self.instant - dt.timedelta(days=1)
            + dt.timedelta(seconds=2700),
            closure_confirmed_at=self.instant - dt.timedelta(days=1)
            + dt.timedelta(seconds=2701),
            max_duration_seconds=2700, optimal_duration_seconds=2700,
            application_rate_mm_h=12.5, stop_reason="COMPLETED",
            delivery_uncertain=True, sender_interrupted=True,
            error_message="Historical uncertain delivery must remain visible",
        )
        return site.pk, [valve.pk for valve in valves]

    def test_populated_upgrade_preserves_all_history_settings_and_ownership(self):
        old = self.migrate(self.before)
        try:
            self.populate_legacy(old)
            names = (
                "Site", "Schedule", "CurveSettings", "RelayDevice", "Valve",
                "ScheduleRule", "GroupedRule", "GroupedRuleValve",
                "RuleOccurrence", "IrrigationRun",
            )
            originals = {
                name: list(old.get_model("irrigation", name).objects
                           .order_by("pk").values())
                for name in names
            }
            new = self.migrate(self.after)
            for name, records in originals.items():
                with self.subTest(model=name):
                    self.assertEqual(
                        list(new.get_model("irrigation", name).objects
                             .order_by("pk").values()), records,
                    )
            self.assertFalse(
                new.get_model("irrigation", "ValveClosure").objects.exists()
            )
            self.assertEqual(
                new.get_model("irrigation", "IrrigationRun")
                ._meta.get_field("dispatch_state").default,
                "DONE",
            )
        finally:
            self.restore_schema()

    def test_upgraded_manual_senders_require_explicit_stopped_process_reconciliation(self):
        old = self.migrate(self.before)
        try:
            site_id, valve_ids = self.populate_legacy(old)
            self.migrate(self.after)

            from apps.irrigation import group_services
            from apps.irrigation.balance import delivery_estimate
            from apps.irrigation.models import IrrigationRun, Site, Valve

            site = Site.objects.get(pk=site_id)
            valve = Valve.objects.select_related("relay_device__site").get(
                pk=valve_ids[0],
            )
            physical = {valve_id: True for valve_id in valve_ids}
            physical[valve_ids[0]] = False  # UNSENT did not reach the relay.
            history = IrrigationRun.objects.filter(trigger="SCHEDULED").values().get()
            with (
                mock.patch("apps.irrigation.services.open_valve_for") as opening,
                mock.patch(
                    "apps.irrigation.services.close_valve",
                    side_effect=lambda item: physical.update({item.pk: False}),
                ) as closing,
                mock.patch(
                    "apps.irrigation.services.read_valve_state",
                    side_effect=lambda item: physical[item.pk],
                ),
            ):
                group_services.recover_groups()
                group_services.reconcile_attempts()
                with self.assertRaises(ValidationError):
                    group_services.request_single(valve, 2700)
                self.assertEqual(
                    list(IrrigationRun.objects.filter(trigger="MANUAL")
                         .order_by("pk").values_list("dispatch_state", flat=True)),
                    ["UNSENT", "SENDING", "LEGACY", "LEGACY"],
                )
                opening.assert_not_called()

                call_command(
                    "reconcile_openings", senders_stopped=True,
                    stdout=io.StringIO(),
                )
                self.assertFalse(group_services._unresolved_runs(site).exists())
                self.assertEqual(
                    [call.args[0].pk for call in closing.call_args_list],
                    valve_ids[1:],
                )
                self.assertFalse(any(physical.values()))
                unsent = IrrigationRun.objects.get(valve=valve, trigger="MANUAL")
                self.assertEqual(unsent.status, "FAILED")
                self.assertIsNone(unsent.attempt_started_at)
                self.assertFalse(unsent.delivery_uncertain)
                estimate = delivery_estimate(unsent)
                self.assertIsNone(estimate["estimated_mm"])
                self.assertIsNone(estimate["nominal_mm"])
                self.assertFalse(estimate["unknown_extra_delivery"])
                self.assertEqual(
                    IrrigationRun.objects.filter(pk=history["id"]).values().get(),
                    history,
                )
                queued = group_services.request_single(valve, 2700)
                self.assertEqual(queued.dispatch_state, "QUEUED")
                self.assertIsNone(queued.attempt_started_at)
                opening.assert_not_called()
        finally:
            self.restore_schema()

    def test_failed_legacy_close_without_attempt_metadata_keeps_site_blocked(self):
        old = self.migrate(self.before)
        try:
            site_id, valve_ids = self.populate_legacy(old)
            old.get_model("irrigation", "IrrigationRun").objects.filter(
                valve_id=valve_ids[2], trigger="MANUAL",
            ).update(attempt_started_at=None)
            self.migrate(self.after)

            from apps.irrigation import group_services
            from apps.irrigation.models import IrrigationRun, Site, Valve

            site = Site.objects.get(pk=site_id)
            valve = Valve.objects.select_related("relay_device__site").get(
                pk=valve_ids[2],
            )
            other = Valve.objects.select_related("relay_device__site").get(
                pk=valve_ids[0],
            )
            physical = {valve_id: False for valve_id in valve_ids}
            physical[valve.pk] = True

            def close(item):
                if item.pk == valve.pk:
                    raise RuntimeError("Legacy relay unavailable")
                physical[item.pk] = False

            with (
                mock.patch("apps.irrigation.services.open_valve_for") as opening,
                mock.patch(
                    "apps.irrigation.services.close_valve", side_effect=close,
                ) as closing,
                mock.patch(
                    "apps.irrigation.services.read_valve_state",
                    side_effect=lambda item: physical[item.pk],
                ),
            ):
                with self.assertRaisesMessage(CommandError, "1 run(s)"):
                    call_command(
                        "reconcile_openings", senders_stopped=True,
                        stdout=io.StringIO(),
                    )
                legacy = IrrigationRun.objects.get(valve=valve, trigger="MANUAL")
                self.assertEqual(legacy.dispatch_state, "DONE")
                self.assertIsNone(legacy.attempt_started_at)
                self.assertIsNone(legacy.closure_confirmed_at)
                self.assertTrue(legacy.cancellation_requested)
                self.assertEqual(group_services._unresolved_runs(site).count(), 1)
                for target in (valve, other):
                    with self.subTest(valve=target.pk):
                        with self.assertRaises(ValidationError):
                            group_services.request_single(target, 60)
                opening.assert_not_called()

                closing.side_effect = lambda item: physical.update({item.pk: False})
                call_command(
                    "reconcile_openings", senders_stopped=True,
                    stdout=io.StringIO(),
                )
                legacy.refresh_from_db()
                self.assertIsNotNone(legacy.closure_confirmed_at)
                self.assertFalse(group_services._unresolved_runs(site).exists())
                group_services.request_single(valve, 60)
                opening.assert_not_called()
        finally:
            self.restore_schema()
