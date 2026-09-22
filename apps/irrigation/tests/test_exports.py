from __future__ import annotations

import datetime as dt
import json
import math
from unittest import mock, skipUnless

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.irrigation.balance import delivery_estimate
from apps.irrigation.exports import iter_logs
from apps.irrigation.models import IrrigationRun, RelayDevice, Site, Valve
from apps.irrigation.site_context import ACTIVE_SITE_SESSION_KEY


def reject_nonfinite(value):
    raise ValueError(f"Invalid JSON number: {value}")


class LogsExportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(username="exporter")
        cls.site = Site.objects.create(name="Garden", timezone="Europe/Berlin")
        relay = RelayDevice.objects.create(
            site=cls.site, name="Relay", host="127.0.0.1",
        )
        cls.valve = Valve.objects.create(
            relay_device=relay, channel=1, name='Lawn "East"',
        )
        cls.exported_at = dt.datetime(2026, 9, 22, 12, tzinfo=dt.timezone.utc)

    def setUp(self):
        self.client.force_login(self.user)

    def create_run(self, **overrides):
        start = self.exported_at - dt.timedelta(minutes=10)
        values = {
            "valve": self.valve,
            "trigger": IrrigationRun.TRIGGER_MANUAL,
            "status": IrrigationRun.STATUS_FINISHED,
            "requested_start_at": start,
            "actual_start_at": start,
            "actual_stop_at": start + dt.timedelta(minutes=5),
            "optimal_duration_seconds": 300,
            "max_duration_seconds": 300,
            "application_rate_mm_h": 12,
        }
        values.update(overrides)
        return IrrigationRun.objects.create(**values)

    def download(self, *, extended=False):
        with mock.patch(
            "apps.irrigation.views.timezone.now", return_value=self.exported_at,
        ):
            response = self.client.get(
                reverse("logs_export"), {"extended": "1"} if extended else {},
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.streaming)
        payload = json.loads(
            b"".join(response.streaming_content), parse_constant=reject_nonfinite,
        )
        return response, payload

    def test_export_requires_authentication_and_get(self):
        self.client.logout()
        response = self.client.get(reverse("logs_export"))
        self.assertEqual(response.status_code, 302)
        self.client.force_login(self.user)
        response = self.client.post(reverse("logs_export"))
        self.assertEqual(response.status_code, 405)

    def test_empty_history_metadata_and_attachment_headers(self):
        response, payload = self.download()
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(
            response["Content-Disposition"],
            f'attachment; filename="rainwise-logs-{self.site.pk}-'
            '20260922T120000Z.json"',
        )
        self.assertEqual(payload, {
            "schema_version": 1,
            "exported_at": "2026-09-22T12:00:00+00:00",
            "site": {
                "id": self.site.pk, "name": "Garden", "timezone": "Europe/Berlin",
            },
            "runs": [],
        })

    def test_no_configured_site_exports_empty_history(self):
        self.site.delete()
        _, payload = self.download()
        self.assertIsNone(payload["site"])
        self.assertEqual(payload["runs"], [])

    def test_exports_selected_site_only(self):
        first = self.create_run()
        other_site = Site.objects.create(name="Other", timezone="UTC")
        relay = RelayDevice.objects.create(
            site=other_site, name="Relay", host="127.0.0.1",
        )
        valve = Valve.objects.create(relay_device=relay, name="Other", channel=1)
        other = self.create_run(valve=valve)
        session = self.client.session
        session[ACTIVE_SITE_SESSION_KEY] = other_site.pk
        session.save()
        _, payload = self.download(extended=True)
        self.assertEqual(payload["site"]["id"], other_site.pk)
        self.assertEqual([run["id"] for run in payload["runs"]], [other.pk])
        self.assertNotEqual(payload["runs"][0]["id"], first.pk)

    def test_exports_more_than_logs_page_limit_in_newest_first_order(self):
        IrrigationRun.objects.bulk_create([
            IrrigationRun(
                valve=self.valve, trigger="MANUAL", status="FINISHED",
                max_duration_seconds=300,
            ) for _ in range(205)
        ])
        with CaptureQueriesContext(connection) as queries:
            _, payload = self.download()
        self.assertEqual(len(payload["runs"]), 205)
        self.assertEqual(
            [run["id"] for run in payload["runs"]],
            list(IrrigationRun.objects.order_by("-id").values_list("pk", flat=True)),
        )
        run_queries = [
            query["sql"] for query in queries
            if 'FROM "irrigation_irrigationrun"' in query["sql"]
        ]
        self.assertEqual(len(run_queries), 2)
        self.assertTrue(all("LIMIT 200" in sql for sql in run_queries))
        self.assertTrue(all("OFFSET" not in sql for sql in run_queries))

    def test_run_fields_and_delivery_are_explicit_and_utc(self):
        run = self.create_run(
            requested_start_at=dt.datetime(
                2026, 9, 22, 13, 50, tzinfo=dt.timezone(dt.timedelta(hours=2)),
            ),
            error_message='Relay said "closed"\nVerified',
        )
        _, payload = self.download()
        row = payload["runs"][0]
        self.assertEqual(set(row), {
            "id", "valve_id", "valve", "trigger", "status", "stop_reason",
            "error_message", "requested_start_at", "planned_start_at",
            "attempt_started_at", "attempt_finished_at", "actual_start_at",
            "actual_stop_at", "closure_confirmed_at", "optimal_duration_seconds",
            "max_duration_seconds", "application_rate_mm_h", "delivery_uncertain",
            "delivery",
        })
        self.assertEqual(row["id"], run.pk)
        self.assertEqual(row["requested_start_at"], "2026-09-22T11:50:00+00:00")
        self.assertEqual(row["valve"], {
            "id": self.valve.pk, "name": self.valve.name,
            "relay_device_id": self.valve.relay_device_id, "channel": 1,
        })
        self.assertEqual(row["delivery"]["nominal_mm"], 1)
        self.assertEqual(row["delivery"]["estimated_mm"], 1)
        self.assertTrue(row["delivery"]["calibrated"])
        self.assertFalse(row["delivery"]["uncertain"])

    def test_extended_adds_stored_appendix_and_handles_legacy_empty_appendix(self):
        legacy = self.create_run()
        appendix = {"schema_version": 1, "temperature_c": 0, "missing": None}
        current = self.create_run(appendix=appendix)
        _, standard = self.download()
        response, extended = self.download(extended=True)
        self.assertIn("-extended.json", response["Content-Disposition"])
        self.assertEqual(extended["runs"][0]["id"], current.pk)
        self.assertEqual(extended["runs"][1]["id"], legacy.pk)
        self.assertEqual(extended["runs"][0]["appendix"], appendix)
        self.assertEqual(extended["runs"][1]["appendix"], {})
        for row in extended["runs"]:
            del row["appendix"]
        self.assertEqual(standard, extended)

    def test_active_delivery_uses_export_cutoff(self):
        run = self.create_run(
            status="RUNNING", actual_stop_at=None,
            actual_start_at=self.exported_at - dt.timedelta(seconds=60),
        )
        with mock.patch(
            "apps.irrigation.exports.delivery_estimate", wraps=delivery_estimate,
        ) as estimate:
            _, payload = self.download()
        estimate.assert_called_once()
        self.assertEqual(estimate.call_args.args[0].pk, run.pk)
        self.assertEqual(estimate.call_args.kwargs["cutoff"], self.exported_at)
        self.assertEqual(payload["runs"][0]["delivery"]["estimated_mm"], 0.2)
        self.assertEqual(
            payload["runs"][0]["delivery"]["end_at"], self.exported_at.isoformat(),
        )

    def test_uncertain_and_uncalibrated_delivery_remain_explicit(self):
        self.create_run(
            actual_start_at=None, actual_stop_at=None,
            attempt_started_at=self.exported_at - dt.timedelta(seconds=30),
            delivery_uncertain=True, application_rate_mm_h=None,
        )
        _, payload = self.download()
        delivery = payload["runs"][0]["delivery"]
        self.assertIsNone(delivery["estimated_mm"])
        self.assertIsNone(delivery["nominal_mm"])
        self.assertFalse(delivery["calibrated"])
        self.assertTrue(delivery["uncertain"])
        self.assertTrue(delivery["unknown_extra_delivery"])
        self.assertTrue(delivery["nominal_allowance_beyond_cutoff"])

    def test_nonfinite_legacy_calibration_exports_as_null_in_strict_json(self):
        self.create_run(application_rate_mm_h=math.inf)
        _, payload = self.download()
        row = payload["runs"][0]
        self.assertIsNone(row["application_rate_mm_h"])
        self.assertIsNone(row["delivery"]["estimated_mm"])
        self.assertFalse(row["delivery"]["calibrated"])

    def test_export_does_not_fetch_weather_recalculate_or_access_hardware(self):
        self.create_run(appendix={"evidence": "preserved"})
        with (
            mock.patch("apps.irrigation.balance.build_smart_decision") as decision,
            mock.patch("apps.weather.services.ensure_recent_weather") as weather,
            mock.patch("apps.irrigation.services._client_for") as hardware,
        ):
            self.download(extended=True)
        decision.assert_not_called()
        weather.assert_not_called()
        hardware.assert_not_called()

    def test_standard_export_never_reads_appendix_column(self):
        self.create_run(appendix={"evidence": "stored"})
        with CaptureQueriesContext(connection) as queries:
            self.download()
        run_queries = [
            query["sql"] for query in queries
            if 'FROM "irrigation_irrigationrun"' in query["sql"]
        ]
        self.assertEqual(len(run_queries), 1)
        self.assertNotIn('"appendix"', run_queries[0])

    def test_history_and_status_defer_appendices(self):
        self.create_run(appendix={"evidence": "stored"})
        for endpoint in ("logs", "valve_status"):
            with self.subTest(endpoint=endpoint):
                with CaptureQueriesContext(connection) as queries:
                    response = self.client.get(reverse(endpoint))
                self.assertEqual(response.status_code, 200)
                run_queries = [
                    query["sql"] for query in queries
                    if '"irrigation_irrigationrun"' in query["sql"]
                ]
                self.assertTrue(run_queries)
                self.assertTrue(all('"appendix"' not in sql for sql in run_queries))
        response = self.client.get(reverse("logs"))
        self.assertContains(response, reverse("logs_export") + '?extended=1')
        self.assertContains(response, "Download logs")
        self.assertContains(response, "Download extended logs")
        self.assertContains(response, "all recorded history for the selected site")


@skipUnless(connection.vendor == "sqlite", "SQLite reader/writer lock regression")
class LogsExportSQLiteLockTests(TransactionTestCase):
    def test_paused_export_does_not_block_controller_database_writes(self):
        site = Site.objects.create(name="Garden", timezone="UTC")
        relay = RelayDevice.objects.create(
            site=site, name="Relay", host="127.0.0.1",
        )
        valve = Valve.objects.create(relay_device=relay, channel=1, name="Lawn")
        IrrigationRun.objects.bulk_create([
            IrrigationRun(
                valve=valve, trigger="MANUAL", status="FINISHED",
                max_duration_seconds=300,
            ) for _ in range(401)
        ])
        run_ids = list(
            IrrigationRun.objects.order_by("-id").values_list("pk", flat=True)
        )
        stream = iter_logs(site, dt.datetime.now(dt.timezone.utc))
        chunks = [next(stream), next(stream)]
        writer = connection.copy()
        try:
            with writer.cursor() as cursor:
                cursor.execute(
                    'UPDATE "irrigation_irrigationrun" '
                    'SET "error_message" = %s WHERE "id" = %s',
                    ["Controller updated while download paused", run_ids[0]],
                )
            writer.commit()
            self.assertEqual(
                IrrigationRun.objects.get(pk=run_ids[0]).error_message,
                "Controller updated while download paused",
            )
            chunks.extend(stream)
        finally:
            stream.close()
            writer.close()
        payload = json.loads("".join(chunks), parse_constant=reject_nonfinite)
        self.assertEqual([row["id"] for row in payload["runs"]], run_ids)
