import datetime as dt

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class FixedSmartMigrationTests(TransactionTestCase):
    """Run the real forward and reverse schema/data migration on the test DB."""

    migrate_from = ("irrigation", "0006_relay_flash_duration_limits")
    migrate_to = ("irrigation", "0007_groupedrule_groupedrulevalve_ruleoccurrence_and_more")

    def test_normalizes_all_dynamic_rows_preserving_config_and_running_history(self):
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        old = executor.loader.project_state([self.migrate_from]).apps
        try:
            site = old.get_model("irrigation", "Site").objects.create(name="Home")
            device = old.get_model("irrigation", "RelayDevice").objects.create(
                site=site, name="Relay", host="test.invalid"
            )
            valve = old.get_model("irrigation", "Valve").objects.create(
                relay_device=device, name="Lawn", channel=1
            )
            rule_model = old.get_model("irrigation", "ScheduleRule")
            originals = []
            for index, active in enumerate((True, False)):
                schedule = old.get_model("irrigation", "Schedule").objects.create(
                    site=site, name=f"Schedule {index}"
                )
                if active:
                    site.active_schedule_id = schedule.pk
                    site.save()
                for enabled in (True, False):
                    rule = rule_model.objects.create(
                        schedule=schedule, valve=valve, enabled=enabled, mode="DYNAMIC",
                        days_of_week_mask=21, start_time=dt.time(6, 30),
                        max_duration_seconds=900, note=f"Original {active} {enabled}",
                    )
                    originals.append(rule_model.objects.filter(pk=rule.pk).values().get())
            fixed = rule_model.objects.create(
                schedule=schedule, valve=valve, mode="FIXED", days_of_week_mask=1,
                start_time=dt.time(7), max_duration_seconds=300,
            )
            run_model = old.get_model("irrigation", "IrrigationRun")
            run = run_model.objects.create(
                valve=valve, trigger="SCHEDULED", status="RUNNING",
                actual_start_at=dt.datetime(2026, 7, 1, 4, tzinfo=dt.timezone.utc),
                optimal_duration_seconds=137, max_duration_seconds=900,
            )
            original_run = run_model.objects.filter(pk=run.pk).values().get()

            executor = MigrationExecutor(connection)
            executor.migrate([self.migrate_to])
            new = executor.loader.project_state([self.migrate_to]).apps
            for original in originals:
                migrated = new.get_model("irrigation", "ScheduleRule").objects.filter(pk=original["id"]).values().get()
                self.assertEqual(migrated, {**original, "mode": "FIXED"})
            migrated_run = new.get_model("irrigation", "IrrigationRun").objects.filter(pk=run.pk).values().get()
            self.assertEqual({key: migrated_run[key] for key in original_run}, original_run)

            executor = MigrationExecutor(connection)
            executor.migrate([self.migrate_from])
            reverted = executor.loader.project_state([self.migrate_from]).apps
            self.assertFalse(reverted.get_model("irrigation", "ScheduleRule").objects.exclude(mode="FIXED").exists())
            self.assertEqual(reverted.get_model("irrigation", "ScheduleRule").objects.get(pk=fixed.pk).mode, "FIXED")
        finally:
            executor = MigrationExecutor(connection)
            executor.migrate(executor.loader.graph.leaf_nodes())
