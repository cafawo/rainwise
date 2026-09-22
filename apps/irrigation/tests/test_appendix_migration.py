from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class AppendixMigrationTests(TransactionTestCase):
    def test_existing_history_gets_empty_appendix_without_other_changes(self):
        old_target = ("irrigation", "0013_in_memory_group_sequences")
        new_target = ("irrigation", "0014_irrigationrun_appendix")
        executor = MigrationExecutor(connection)
        executor.migrate([old_target])
        old = executor.loader.project_state([old_target]).apps
        try:
            site = old.get_model("irrigation", "Site").objects.create(name="Home")
            relay = old.get_model("irrigation", "RelayDevice").objects.create(
                site=site, name="Relay", host="test.invalid",
            )
            valve = old.get_model("irrigation", "Valve").objects.create(
                relay_device=relay, name="Lawn", channel=1,
            )
            runs = old.get_model("irrigation", "IrrigationRun")
            run = runs.objects.create(
                valve=valve, trigger="MANUAL", status="FINISHED",
                max_duration_seconds=60, error_message="Original history",
            )
            original = runs.objects.values().get(pk=run.pk)
            executor = MigrationExecutor(connection)
            executor.migrate([new_target])
            new = executor.loader.project_state([new_target]).apps
            migrated = new.get_model("irrigation", "IrrigationRun").objects.get(
                pk=run.pk,
            )
            self.assertEqual(migrated.appendix, {})
            self.assertEqual(
                {key: getattr(migrated, key) for key in original}, original,
            )
        finally:
            executor = MigrationExecutor(connection)
            executor.migrate(executor.loader.graph.leaf_nodes())
