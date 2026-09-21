"""Verify the upgrade fills only missing settings, without rewriting history."""
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class FallbackDefaultsMigrationTests(TransactionTestCase):
    migrate_from = (
        "irrigation", "0007_groupedrule_groupedrulevalve_ruleoccurrence_and_more"
    )
    migrate_to = ("irrigation", "0009_require_fallback_temperature")

    def test_defaults_preserve_existing_settings_rates_and_history(self):
        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_from])
        old = executor.loader.project_state([self.migrate_from]).apps
        try:
            Site = old.get_model("irrigation", "Site")
            CurveSettings = old.get_model("irrigation", "CurveSettings")
            absent = Site.objects.create(name="No settings")
            originals = []
            for index, fallback in enumerate((None, 0, 18.5, 25, -5)):
                site = Site.objects.create(name=f"Existing {index}")
                row = CurveSettings.objects.create(
                    site=site, fallback_temperature_c=fallback,
                    min_mm=1, max_mm=9, g=0.2, m=27, coverage_days=5,
                )
                originals.append(
                    CurveSettings.objects.filter(pk=row.pk).values().get()
                )
            RelayDevice = old.get_model("irrigation", "RelayDevice")
            Valve = old.get_model("irrigation", "Valve")
            IrrigationRun = old.get_model("irrigation", "IrrigationRun")
            device = RelayDevice.objects.create(
                site=absent, name="Relay", host="test.invalid"
            )
            valve = Valve.objects.create(
                relay_device=device, name="Unknown rate", channel=1
            )
            legacy_run = IrrigationRun.objects.create(
                valve=valve, trigger="MANUAL", status="FINISHED",
                max_duration_seconds=300,
            )
            calibrated_run = IrrigationRun.objects.create(
                valve=valve, trigger="SCHEDULED", status="RUNNING",
                max_duration_seconds=900, optimal_duration_seconds=123,
                application_rate_mm_h=12,
            )
            original_runs = list(IrrigationRun.objects.order_by("pk").values())

            executor = MigrationExecutor(connection)
            executor.migrate([self.migrate_to])
            new = executor.loader.project_state([self.migrate_to]).apps
            CurveSettings = new.get_model("irrigation", "CurveSettings")
            for original in originals:
                migrated = CurveSettings.objects.filter(
                    pk=original["id"]
                ).values().get()
                expected = original.copy()
                if expected["fallback_temperature_c"] is None:
                    expected["fallback_temperature_c"] = 25
                self.assertEqual(migrated, expected)
            supplied = CurveSettings.objects.get(site_id=absent.pk)
            self.assertEqual(supplied.fallback_temperature_c, 25)
            self.assertEqual(supplied.coverage_days, 2)
            self.assertEqual(supplied.min_mm, 0)
            self.assertEqual(supplied.max_mm, 7)
            self.assertEqual(supplied.g, 0.1852)
            self.assertEqual(supplied.m, 25.6653)
            self.assertIsNone(
                new.get_model("irrigation", "Valve").objects.get(
                    pk=valve.pk
                ).application_rate_mm_h
            )
            IrrigationRun = new.get_model("irrigation", "IrrigationRun")
            self.assertEqual(
                list(IrrigationRun.objects.order_by("pk").values()),
                original_runs,
            )
            self.assertIsNone(
                IrrigationRun.objects.get(pk=legacy_run.pk).application_rate_mm_h
            )
            self.assertEqual(
                IrrigationRun.objects.get(pk=calibrated_run.pk)
                .optimal_duration_seconds,
                123,
            )
            new_site = new.get_model("irrigation", "Site").objects.create(
                name="After upgrade"
            )
            self.assertEqual(
                CurveSettings.objects.create(site=new_site).fallback_temperature_c,
                25,
            )

            executor = MigrationExecutor(connection)
            executor.migrate([self.migrate_from])
            reversed_apps = executor.loader.project_state([self.migrate_from]).apps
            reversed_settings = reversed_apps.get_model(
                "irrigation", "CurveSettings"
            )
            self.assertEqual(
                reversed_settings.objects.get(site_id=absent.pk)
                .fallback_temperature_c,
                25,
            )
            self.assertEqual(
                reversed_settings.objects.get(pk=originals[1]["id"])
                .fallback_temperature_c,
                0,
            )
        finally:
            executor = MigrationExecutor(connection)
            executor.migrate(executor.loader.graph.leaf_nodes())
