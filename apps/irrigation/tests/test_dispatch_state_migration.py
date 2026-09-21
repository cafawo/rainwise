import datetime as dt

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class DispatchStateMigrationTests(TransactionTestCase):
    def test_existing_attempt_history_keeps_legacy_sender_semantics(self):
        before = ("irrigation", "0009_require_fallback_temperature")
        after = ("irrigation", "0010_irrigationrun_dispatch_state")
        executor = MigrationExecutor(connection)
        executor.migrate([before])
        old = executor.loader.project_state([before]).apps
        try:
            site = old.get_model("irrigation", "Site").objects.create(name="Home")
            device = old.get_model("irrigation", "RelayDevice").objects.create(
                site=site, name="Relay", host="test.invalid"
            )
            valve = old.get_model("irrigation", "Valve").objects.create(
                relay_device=device, name="Lawn", channel=1
            )
            runs = old.get_model("irrigation", "IrrigationRun")
            instant = dt.datetime(2026, 9, 21, 6, tzinfo=dt.timezone.utc)
            for state in ("RUNNING", "PLANNED", "FAILED", "FINISHED"):
                runs.objects.create(
                    valve=valve, status=state, trigger="MANUAL",
                    actual_start_at=instant if state != "PLANNED" else None,
                    attempt_started_at=instant, max_duration_seconds=900,
                    optimal_duration_seconds=137, application_rate_mm_h=12,
                    delivery_uncertain=state in ("PLANNED", "FAILED"),
                )
            originals = list(runs.objects.order_by("pk").values())
            executor = MigrationExecutor(connection)
            executor.migrate([after])
            new = executor.loader.project_state([after]).apps
            migrated = list(
                new.get_model("irrigation", "IrrigationRun").objects
                .order_by("pk").values()
            )
            self.assertEqual(
                migrated,
                [{**row, "dispatch_state": "LEGACY", "sender_interrupted": False}
                 for row in originals],
            )
        finally:
            executor = MigrationExecutor(connection)
            executor.migrate(executor.loader.graph.leaf_nodes())
