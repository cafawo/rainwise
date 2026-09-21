from __future__ import annotations

import datetime as dt
import os
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.irrigation.forms import CurveForm
from apps.irrigation.models import (
    CurveSettings, GroupedRule, GroupedRuleValve, IrrigationRun, RelayDevice,
    RuleOccurrence, Schedule, ScheduleRule, Site, Valve,
)


class SharedRuleEditorTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user(username="editor", password="test")
        self.client.force_login(user)
        self.site = Site.objects.create(name="Home", timezone="UTC")
        self.schedule = Schedule.objects.create(site=self.site, name="Default")
        self.site.active_schedule = self.schedule
        self.site.save()
        relay = RelayDevice.objects.create(site=self.site, name="Relay", host="invalid")
        self.a = Valve.objects.create(relay_device=relay, name="Lawn A", channel=1,
                                      application_rate_mm_h=12, default_max_duration_seconds=900)
        self.b = Valve.objects.create(relay_device=relay, name="Lawn B", channel=2,
                                      application_rate_mm_h=12, default_max_duration_seconds=900)
        self.curve = CurveSettings.objects.create(site=self.site, fallback_temperature_c=25)

    def payload(self, mode="FIXED", valves=None, **changes):
        members = valves if valves is not None else [self.a]
        data = {
            "mode": mode, "enabled": "on", "days_of_week": [str(i) for i in range(7)],
            "start_time": "06:30", "note": "Morning",
            "members-TOTAL_FORMS": str(len(members)), "members-INITIAL_FORMS": "0",
        }
        for index, valve in enumerate(members):
            data[f"members-{index}-valve"] = str(valve.pk)
            data[f"members-{index}-duration_seconds"] = "900"
            data[f"members-{index}-ORDER"] = str(index + 1)
        data.update(changes)
        return data

    def group(self, mode="SMART", enabled=True):
        rule = GroupedRule.objects.create(
            schedule=self.schedule, mode=mode, enabled=enabled,
            start_time=dt.time(6, 30), days_of_week_mask=127, note="Morning",
        )
        for order, valve in enumerate((self.a, self.b), 1):
            GroupedRuleValve.objects.create(rule=rule, valve=valve, order=order, duration_seconds=900)
        return rule

    def legacy(self):
        return ScheduleRule.objects.create(
            schedule=self.schedule, valve=self.a, mode="FIXED", enabled=True,
            start_time=dt.time(6, 30), days_of_week_mask=127, max_duration_seconds=900,
        )

    def test_single_fixed_keeps_legacy_storage_and_full_runtime(self):
        response = self.client.post(reverse("schedule_create"), self.payload())
        self.assertEqual(response.status_code, 302)
        rule = ScheduleRule.objects.get()
        self.assertEqual(rule.max_duration_seconds, 900)
        self.assertFalse(GroupedRule.objects.exists())

    def test_fixed_group_preserves_order_and_needs_no_calibration(self):
        self.curve.delete()
        Valve.objects.update(application_rate_mm_h=None)
        data = self.payload(valves=[self.a, self.b], **{"members-0-ORDER": "2", "members-1-ORDER": "1"})
        response = self.client.post(reverse("schedule_create"), data)
        self.assertEqual(response.status_code, 302)
        rule = GroupedRule.objects.get()
        self.assertEqual(list(rule.members.order_by("order").values_list("valve_id", flat=True)), [self.b.pk, self.a.pk])

    def test_member_order_is_hidden_and_arrows_are_available(self):
        rule = self.group()
        response = self.client.get(reverse("group_edit", args=[rule.pk]))
        self.assertContains(response, 'type="hidden" name="members-0-ORDER"')
        self.assertContains(response, 'type="hidden" name="members-1-ORDER"')
        self.assertNotContains(response, "Position")
        self.assertContains(response, 'aria-label="Move valve up"')
        self.assertContains(response, 'aria-label="Move valve down"')

    def test_invalid_hidden_order_links_to_visible_row_without_saving(self):
        response = self.client.post(reverse("schedule_create"), self.payload(
            **{"members-0-ORDER": "invalid"}
        ))
        self.assertContains(response, "The valve order could not be read.")
        self.assertContains(response, 'href="#member-members-0"')
        self.assertNotContains(response, 'href="#id_members-0-ORDER"')
        self.assertContains(response, 'id="id_members-0-ORDER_errors"')
        self.assertFalse(ScheduleRule.objects.exists())

    def test_smart_single_uses_group_storage(self):
        response = self.client.post(reverse("schedule_create"), self.payload("SMART"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(GroupedRule.objects.get().mode, "SMART")
        self.assertFalse(ScheduleRule.objects.exists())

    def test_smart_requires_valid_limits_and_days(self):
        for change, text in [({"days_of_week": []}, "required"),
                             ({"members-0-duration_seconds": "3277"}, "3276")]:
            with self.subTest(change=change):
                response = self.client.post(reverse("schedule_create"), self.payload("SMART", **change))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, text)
        self.assertFalse(GroupedRule.objects.exists())

    def test_duplicates_and_cross_site_valves_rejected(self):
        response = self.client.post(reverse("schedule_create"), self.payload(valves=[self.a, self.a]))
        self.assertContains(response, "already selected")
        other_site = Site.objects.create(name="Other", timezone="UTC")
        relay = RelayDevice.objects.create(site=other_site, name="Other", host="invalid")
        valve = Valve.objects.create(relay_device=relay, name="Other", channel=1)
        response = self.client.post(reverse("schedule_create"), self.payload(valves=[valve]))
        self.assertContains(response, "at this site")
        self.assertFalse(ScheduleRule.objects.exists())

    def test_legacy_conversion_is_atomic_and_preserves_history(self):
        old = self.legacy()
        run = IrrigationRun.objects.create(valve=self.a, trigger="MANUAL", status="FINISHED", max_duration_seconds=90)
        data = self.payload("SMART", [self.a, self.b])
        response = self.client.post(reverse("schedule_edit", args=[old.pk]), data)
        self.assertEqual(response.status_code, 302)
        group = GroupedRule.objects.get()
        self.assertRedirects(response, reverse("group_edit", args=[group.pk]))
        self.assertFalse(ScheduleRule.objects.exists())
        run.refresh_from_db()
        self.assertEqual(run.max_duration_seconds, 90)

    def test_conversion_rejects_active_run_and_keeps_original(self):
        old = self.legacy()
        IrrigationRun.objects.create(valve=self.a, trigger="MANUAL", status="RUNNING", max_duration_seconds=90)
        response = self.client.post(reverse("schedule_edit", args=[old.pk]), self.payload("SMART"))
        self.assertContains(response, "Stop watering")
        self.assertTrue(ScheduleRule.objects.filter(pk=old.pk).exists())
        self.assertFalse(GroupedRule.objects.exists())

    def test_edit_single_fixed_preserves_id(self):
        old = self.legacy()
        response = self.client.post(reverse("schedule_edit", args=[old.pk]), self.payload(note="Updated"))
        self.assertEqual(response.status_code, 302)
        old.refresh_from_db()
        self.assertEqual(old.note, "Updated")

    def test_residual_mode_displays_edits_and_copies_as_fixed(self):
        old = self.legacy()
        ScheduleRule.objects.filter(pk=old.pk).update(mode="DYNAMIC")
        for route in ("schedule_edit", "schedule_copy"):
            response = self.client.get(reverse(route, args=[old.pk]))
            self.assertNotContains(response, "Dynamic")
            self.assertEqual(response.context["form"].initial["mode"], "FIXED")
        response = self.client.post(reverse("schedule_copy", args=[old.pk]), self.payload(start_time="08:00"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ScheduleRule.objects.exclude(pk=old.pk).get().mode, "FIXED")
        response = self.client.post(reverse("schedule_edit", args=[old.pk]), self.payload())
        self.assertEqual(response.status_code, 302)
        old.refresh_from_db()
        self.assertEqual(old.mode, "FIXED")

    def test_dynamic_submission_is_not_a_choice(self):
        response = self.client.post(reverse("schedule_create"), self.payload("DYNAMIC"))
        self.assertContains(response, "valid choice")
        self.assertFalse(ScheduleRule.objects.exists())

    @mock.patch("apps.irrigation.services.open_valve_for")
    def test_unknown_and_misrouted_smart_never_open(self, opening):
        old = self.legacy()
        for mode in ("UNKNOWN", "SMART"):
            ScheduleRule.objects.filter(pk=old.pk).update(mode=mode)
            self.client.post(reverse("schedule_run", args=[old.pk]))
        opening.assert_not_called()
        self.assertFalse(IrrigationRun.objects.exists())

    @mock.patch("apps.irrigation.services.open_valve_for")
    def test_residual_run_now_uses_stored_maximum(self, opening):
        old = self.legacy()
        ScheduleRule.objects.filter(pk=old.pk).update(mode="DYNAMIC")
        self.client.post(reverse("schedule_run", args=[old.pk]))
        self.assertEqual(opening.call_args.args[1], 900)
        self.assertEqual(IrrigationRun.objects.get().optimal_duration_seconds, 900)

    @mock.patch("apps.irrigation.services.open_valve_for")
    def test_fixed_group_run_now_only_requests_controller(self, opening):
        rule = self.group("FIXED")
        now = dt.datetime(2026, 9, 21, 6, 0, tzinfo=dt.timezone.utc)
        with mock.patch("apps.irrigation.group_services.timezone.now", return_value=now):
            self.client.post(reverse("group_run", args=[rule.pk]))
            self.client.post(reverse("group_run", args=[rule.pk]))
        opening.assert_not_called()
        self.assertEqual(RuleOccurrence.objects.count(), 1)
        self.assertEqual(RuleOccurrence.objects.get().status, "PENDING")

    @mock.patch("apps.irrigation.services.open_valve_for")
    def test_smart_preview_never_opens_and_no_run_now_button(self, opening):
        rule = self.group()
        response = self.client.get(reverse("group_edit", args=[rule.pk]))
        self.assertContains(response, "Preview")
        self.assertNotContains(response, ">Run now<")
        response = self.client.get(reverse("group_preview", args=[rule.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "fallback temperature")
        self.assertContains(response, "Lawn A")
        self.client.post(reverse("group_run", args=[rule.pk]))
        opening.assert_not_called()
        self.assertFalse(RuleOccurrence.objects.exists())

    def test_calendar_uses_peak_watering_breaks_and_allowance_at_both_cadences(self):
        rule = self.group()
        for interval in (30, 60):
            with self.subTest(interval=interval), mock.patch.dict(os.environ, {"CONTROLLER_INTERVAL_SECONDS": str(interval)}):
                response = self.client.get(reverse("calendar_events"), {"start": "2026-09-21", "end": "2026-09-22"})
                event, = response.json()
                self.assertEqual(event["watering_seconds"], 8400)
                self.assertEqual(event["break_seconds"], 300)
                from apps.irrigation.group_services import command_allowance
                allowance = 19 * interval + 10 * command_allowance()
                self.assertEqual(event["scheduling_allowance_seconds"], allowance)
                self.assertEqual(event["edit_url"], reverse("group_edit", args=[rule.pk]))
                elapsed = dt.datetime.fromisoformat(event["end"]) - dt.datetime.fromisoformat(event["start"])
                self.assertEqual(elapsed.total_seconds(), 8700 + allowance)
                self.assertIn("Lawn A → Lawn B", event["title"])

    def test_group_window_overlap_rejected_in_both_directions(self):
        self.group("FIXED")
        response = self.client.post(reverse("schedule_create"), self.payload())
        self.assertContains(response, "overlaps")
        self.assertFalse(ScheduleRule.objects.exists())
        GroupedRule.objects.all().delete()
        self.legacy()
        response = self.client.post(reverse("schedule_create"), self.payload(valves=[self.a, self.b]))
        self.assertContains(response, "overlaps")
        self.assertFalse(GroupedRule.objects.exists())

    def test_unrelated_fixed_overlaps_remain_allowed(self):
        self.legacy()
        response = self.client.post(reverse("schedule_create"), self.payload(valves=[self.b]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ScheduleRule.objects.count(), 2)

    def test_groups_cannot_cross_midnight(self):
        response = self.client.post(reverse("schedule_create"), self.payload(valves=[self.a, self.b], start_time="23:50"))
        self.assertContains(response, "midnight")
        self.assertFalse(GroupedRule.objects.exists())

    def occurrence(self, rule):
        return RuleOccurrence.objects.create(
            rule=rule, site=self.site, mode=rule.mode, status="ACTIVE", source="MANUAL",
            requested_at=timezone.now(), config={"members": [{"name": "Lawn A"}]},
        )

    def test_disabling_group_requests_cancellation(self):
        rule = self.group("FIXED")
        occurrence = self.occurrence(rule)
        data = self.payload(valves=[self.a, self.b])
        data.pop("enabled")
        response = self.client.post(reverse("group_edit", args=[rule.pk]), data)
        self.assertEqual(response.status_code, 302)
        occurrence.refresh_from_db()
        self.assertTrue(occurrence.cancellation_requested)
        self.assertEqual(occurrence.status, "STOPPING")

    def test_mode_change_requires_stop_and_delete_preserves_occurrence(self):
        rule = self.group("FIXED")
        occurrence = self.occurrence(rule)
        response = self.client.post(reverse("group_edit", args=[rule.pk]), self.payload("SMART", [self.a, self.b]))
        self.assertContains(response, "Stop the rule")
        self.client.post(reverse("group_delete", args=[rule.pk]))
        occurrence.refresh_from_db()
        self.assertIsNone(occurrence.rule_id)
        self.assertTrue(occurrence.cancellation_requested)
        self.assertEqual(occurrence.config["members"][0]["name"], "Lawn A")

    def test_copy_schedule_preserves_groups_and_normalizes_residual(self):
        rule = self.group("FIXED")
        old = self.legacy()
        old.start_time = dt.time(9)
        old.save()
        ScheduleRule.objects.filter(pk=old.pk).update(mode="DYNAMIC")
        response = self.client.post(reverse("schedule_new"), {"name": "Summer", "copy_current": "on"})
        self.assertEqual(response.status_code, 302)
        copied = Schedule.objects.get(name="Summer")
        group = GroupedRule.objects.get(schedule=copied)
        self.assertEqual(list(group.members.order_by("order").values_list("valve_id", flat=True)), [self.a.pk, self.b.pk])
        self.assertEqual(copied.rules.get().mode, "FIXED")
        self.assertNotEqual(group.pk, rule.pk)

    def test_schedule_switch_cancels_active_occurrence(self):
        occurrence = self.occurrence(self.group("FIXED"))
        other = Schedule.objects.create(site=self.site, name="Other")
        self.client.post(reverse("schedule_load"), {"schedule": other.pk})
        occurrence.refresh_from_db()
        self.assertEqual(occurrence.status, "STOPPING")
        self.site.refresh_from_db()
        self.assertEqual(self.site.active_schedule_id, other.pk)

    def test_site_isolation_for_group_routes(self):
        rule = self.group()
        other = Site.objects.create(name="Other", timezone="UTC")
        self.client.post(reverse("site_select"), {"site_id": other.pk})
        for route in ("group_edit", "group_copy", "group_preview"):
            self.assertEqual(self.client.get(reverse(route, args=[rule.pk])).status_code, 404)
        for route in ("group_run", "group_stop", "group_delete"):
            self.assertEqual(self.client.post(reverse(route, args=[rule.pk])).status_code, 404)

    def test_curve_rejects_invalid_windows_and_nonfinite_settings(self):
        data = {"min_mm": 0, "max_mm": 7, "g": 0.2, "m": 25, "coverage_days": 2, "fallback_temperature_c": 25}
        for change in ({"coverage_days": 1.5}, {"coverage_days": 8}, {"fallback_temperature_c": "NaN"}, {"max_mm": "inf"}):
            with self.subTest(change=change):
                self.assertFalse(CurveForm({**data, **change}).is_valid())

    def test_curve_blank_fallback_uses_default_with_enabled_smart(self):
        self.group()
        response = self.client.post(reverse("curve"), {"min_mm": 0, "max_mm": 7, "g": 0.2, "m": 25, "coverage_days": 2, "fallback_temperature_c": ""})
        self.assertContains(response, "Curve saved.")
        self.curve.refresh_from_db()
        self.assertEqual(self.curve.fallback_temperature_c, 25)

    def test_dashboard_and_logs_show_zero_decision_and_fallback(self):
        rule = self.group()
        occurrence = self.occurrence(rule)
        occurrence.status = "ZERO"
        occurrence.outcome = "No demand"
        occurrence.save()
        for route in ("dashboard", "logs"):
            response = self.client.get(reverse(route))
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "No demand")
        self.assertContains(self.client.get(reverse("dashboard")), "Operating with fallback temperature")

    def test_calendar_skips_dst_gap_and_has_one_fold_event(self):
        self.site.timezone = "Europe/Berlin"
        self.site.save()
        rule = self.group("FIXED")
        rule.start_time = dt.time(2, 30)
        rule.save()
        for start, end, expected in (
            ("2026-03-29", "2026-03-30", 0),
            ("2026-10-25", "2026-10-26", 1),
        ):
            response = self.client.get(
                reverse("calendar_events"), {"start": start, "end": end}
            )
            self.assertEqual(len(response.json()), expected)

    def test_remaining_execution_target_visible_without_rewriting_decision(self):
        rule = self.group()
        occurrence = self.occurrence(rule)
        decision = {"valves": {str(self.a.pk): {
            "valve_id": self.a.pk, "valve_name": self.a.name,
            "target_mm": 4, "capacity_mm": 6, "pulse_seconds": [900, 300],
            "estimated_delivery_mm": 4, "unmet_mm": 0,
            "irrigation": {"credit_mm": 0},
        }}}
        occurrence.decision = decision
        occurrence.status = "CANCELLED"
        occurrence.save()
        now = timezone.now()
        IrrigationRun.objects.create(
            valve=self.a, occurrence=occurrence, pass_number=1,
            trigger="SCHEDULED", status="FINISHED",
            max_duration_seconds=900, optimal_duration_seconds=900,
            actual_start_at=now - dt.timedelta(minutes=20),
            actual_stop_at=now - dt.timedelta(minutes=5),
            application_rate_mm_h=12,
        )
        response = self.client.get(reverse("logs"))
        self.assertContains(response, "remaining target 1.00 mm")
        occurrence.refresh_from_db()
        self.assertEqual(occurrence.decision, decision)

    def test_second_enabled_smart_membership_rejected_outside_overlap(self):
        self.group()
        response = self.client.post(
            reverse("schedule_create"), self.payload("SMART", start_time="12:00")
        )
        self.assertContains(response, "only one enabled Smart rule")
        self.assertEqual(GroupedRule.objects.count(), 1)

    def test_copy_unknown_mode_rolls_back_schedule_and_activation(self):
        rule = self.legacy()
        ScheduleRule.objects.filter(pk=rule.pk).update(mode="UNKNOWN")
        response = self.client.post(
            reverse("schedule_new"), {"name": "Broken", "copy_current": "on"}
        )
        self.assertContains(response, "Unsupported single-valve rule mode")
        self.assertFalse(Schedule.objects.filter(name="Broken").exists())
        self.site.refresh_from_db()
        self.assertEqual(self.site.active_schedule_id, self.schedule.pk)

    def test_admin_rejects_hardware_identity_changes_until_confirmed_closed(self):
        from apps.irrigation.admin import RelayDeviceAdminForm, ValveAdminForm

        now = timezone.now()
        run = IrrigationRun.objects.create(
            valve=self.a, trigger="MANUAL", status="FAILED",
            attempt_started_at=now, max_duration_seconds=900,
        )
        valve_data = {
            "relay_device": self.a.relay_device_id, "channel": 3,
            "name": self.a.name, "is_active_high": "on",
            "default_max_duration_seconds": 800, "application_rate_mm_h": 10,
        }
        form = ValveAdminForm(valve_data, instance=self.a)
        self.assertFalse(form.is_valid())
        self.assertIn("confirmed closure", str(form.errors))
        relay = self.a.relay_device
        relay_data = {
            "site": self.site.pk, "host": "new.invalid", "port": 502,
            "unit_id": 1, "enabled": "on", "name": relay.name,
        }
        form = RelayDeviceAdminForm(relay_data, instance=relay)
        self.assertFalse(form.is_valid())
        self.assertIn("confirmed closure", str(form.errors))
        run.closure_confirmed_at = now
        run.save()
        self.a.refresh_from_db()
        relay.refresh_from_db()
        self.assertTrue(ValveAdminForm(valve_data, instance=self.a).is_valid())
        self.assertTrue(RelayDeviceAdminForm(relay_data, instance=relay).is_valid())

    def test_admin_allows_rate_and_limit_edits_during_watering(self):
        from apps.irrigation.admin import ValveAdminForm

        IrrigationRun.objects.create(
            valve=self.a, trigger="MANUAL", status="RUNNING",
            max_duration_seconds=900,
        )
        form = ValveAdminForm({
            "relay_device": self.a.relay_device_id, "channel": self.a.channel,
            "name": self.a.name, "is_active_high": "on",
            "default_max_duration_seconds": 800, "application_rate_mm_h": 10,
        }, instance=self.a)
        self.assertTrue(form.is_valid(), form.errors)

    @mock.patch("apps.irrigation.services.close_valve")
    def test_admin_disabling_relay_requests_durable_group_cancellation(self, closing):
        user = get_user_model().objects.get(username="editor")
        user.is_staff = user.is_superuser = True
        user.save()
        occurrence = self.occurrence(self.group("FIXED"))
        relay = self.a.relay_device
        response = self.client.post(
            reverse("admin:irrigation_relaydevice_change", args=[relay.pk]),
            {"site": self.site.pk, "name": relay.name, "host": relay.host,
             "port": relay.port, "unit_id": relay.unit_id, "_save": "Save"},
        )
        self.assertEqual(response.status_code, 302)
        occurrence.refresh_from_db()
        self.assertEqual(occurrence.status, "STOPPING")
        self.assertTrue(occurrence.cancellation_requested)
        relay.refresh_from_db()
        self.assertFalse(relay.enabled)
        closing.assert_not_called()

    def test_admin_execution_history_is_read_only(self):
        from django.contrib.admin.sites import AdminSite
        from apps.irrigation.admin import IrrigationRunAdmin, RuleOccurrenceAdmin

        for model, admin_class in (
            (IrrigationRun, IrrigationRunAdmin),
            (RuleOccurrence, RuleOccurrenceAdmin),
        ):
            model_admin = admin_class(model, AdminSite())
            self.assertFalse(model_admin.has_add_permission(None))
            self.assertFalse(model_admin.has_change_permission(None))
            self.assertFalse(model_admin.has_delete_permission(None))

    def test_smart_uses_default_fallback_without_a_settings_page_visit(self):
        self.curve.delete()
        response = self.client.post(reverse("schedule_create"), self.payload("SMART"))
        self.assertEqual(response.status_code, 302)
        response = self.client.get(reverse("group_preview", args=[GroupedRule.objects.get().pk]))
        self.assertContains(response, "fallback temperature 25")
        self.assertEqual(response.context["decision"]["temperature"]["temperature_c"], 25)

    def test_curve_default_override_zero_and_reset_preserve_setting(self):
        self.curve.delete()
        response = self.client.get(reverse("curve"))
        self.assertEqual(response.context["form"].initial["fallback_temperature_c"], 25)
        self.assertContains(response, "Operating with fallback temperature 25.0")
        self.client.post(reverse("curve"), {
            "min_mm": 0, "max_mm": 7, "g": 0.2, "m": 25,
            "coverage_days": 2, "fallback_temperature_c": 0,
        })
        self.assertEqual(CurveSettings.objects.get(site=self.site).fallback_temperature_c, 0)
        response = self.client.post(reverse("curve"), {"reset_defaults": "1"})
        self.assertContains(response, "Operating with fallback temperature 0.0")
        self.assertEqual(CurveSettings.objects.get(site=self.site).fallback_temperature_c, 0)
        self.client.post(reverse("curve"), {
            "min_mm": 0, "max_mm": 6, "g": 0.2, "m": 25,
        })
        self.assertEqual(CurveSettings.objects.get(site=self.site).fallback_temperature_c, 0)

    def test_new_smart_member_needs_rate_even_when_disabled(self):
        for rate in (None, 0, -1, float("inf")):
            for enabled in ("on", ""):
                with self.subTest(rate=rate, enabled=enabled):
                    Valve.objects.filter(pk=self.a.pk).update(application_rate_mm_h=rate)
                    response = self.client.post(
                        reverse("schedule_create"), self.payload("SMART", enabled=enabled)
                    )
                    self.assertContains(response, "enter a measured watering rate")
                    self.assertFalse(GroupedRule.objects.exists())

    def test_smart_selector_retains_missing_member_and_hides_new_missing_valve(self):
        rule = self.group()
        other = Valve.objects.create(
            relay_device=self.a.relay_device, name="Unmeasured", channel=3,
        )
        Valve.objects.filter(pk=self.a.pk).update(application_rate_mm_h=None)
        response = self.client.get(reverse("group_edit", args=[rule.pk]))
        self.assertContains(response, "Lawn A: skipped in Smart")
        self.assertContains(response, "N/A — skipped in Smart")
        options = response.context["members_formset"].forms[0]["valve"].subwidgets
        by_id = {str(option.data["value"]): option.data for option in options}
        self.assertNotIn("disabled", by_id[str(self.a.pk)]["attrs"])
        self.assertTrue(by_id[str(other.pk)]["attrs"]["disabled"])
        self.assertTrue(by_id[str(other.pk)]["attrs"]["hidden"])
        self.assertIn(str(self.b.pk), by_id)
        self.assertContains(response, 'option.disabled = unavailable')

    def test_retaining_missing_member_and_loading_copying_schedule_remain_possible(self):
        rule = self.group()
        Valve.objects.filter(pk=self.a.pk).update(application_rate_mm_h=None)
        response = self.client.post(
            reverse("group_edit", args=[rule.pk]),
            self.payload("SMART", [self.a, self.b], note="Keep membership"),
        )
        self.assertEqual(response.status_code, 302)
        rule.refresh_from_db()
        self.assertEqual(rule.note, "Keep membership")
        self.assertEqual(rule.members.count(), 2)
        self.assertEqual(self.client.post(
            reverse("schedule_load"), {"schedule": self.schedule.pk}
        ).status_code, 302)
        response = self.client.post(reverse("schedule_new"), {
            "name": "Summer without rate", "copy_current": "on",
        })
        self.assertEqual(response.status_code, 302)
        copy = GroupedRule.objects.get(schedule__name="Summer without rate")
        self.assertEqual(list(copy.members.order_by("order").values_list("valve_id", flat=True)), [self.a.pk, self.b.pk])

    def test_new_missing_member_cannot_be_added_to_existing_smart_or_converted_fixed(self):
        rule = self.group()
        other = Valve.objects.create(
            relay_device=self.a.relay_device, name="Unmeasured", channel=3,
        )
        response = self.client.post(
            reverse("group_edit", args=[rule.pk]),
            self.payload("SMART", [self.a, self.b, other]),
        )
        self.assertContains(response, "Unmeasured: enter a measured watering rate")
        rule.mode = "FIXED"
        rule.save()
        Valve.objects.filter(pk=self.a.pk).update(application_rate_mm_h=None)
        response = self.client.post(
            reverse("group_edit", args=[rule.pk]), self.payload("SMART", [self.a, self.b])
        )
        self.assertContains(response, "Lawn A: enter a measured watering rate")

    def test_missing_rate_preview_warns_and_omits_unavailable_peak(self):
        rule = self.group()
        Valve.objects.filter(pk=self.a.pk).update(application_rate_mm_h=None)
        response = self.client.get(reverse("group_preview", args=[rule.pk]))
        self.assertContains(response, "Lawn A: skipped in Smart")
        decision = response.context["decision"]
        self.assertTrue(decision["valves"][str(self.a.pk)]["skipped"])
        self.assertFalse(decision["valves"][str(self.b.pk)]["skipped"])
        self.assertIsNone(decision["valves"][str(self.a.pk)]["target_mm"])
        self.assertContains(response, "N/A")
        self.assertEqual(response.context["reservation"]["watering_seconds"], 4200)
        for route in ("dashboard", "curve"):
            response = self.client.get(reverse(route))
            self.assertContains(response, "Lawn A: skipped in Smart")
            self.assertContains(response, "Enter a measured watering rate")
        Valve.objects.filter(pk=self.a.pk).update(application_rate_mm_h=12)
        for route in ("dashboard", "curve"):
            response = self.client.get(reverse(route))
            self.assertNotContains(response, "Lawn A: skipped in Smart")

    def test_all_missing_rates_render_skipped_decisions_and_preserve_historical_warning(self):
        rule = self.group()
        Valve.objects.update(application_rate_mm_h=None)
        response = self.client.get(reverse("group_preview", args=[rule.pk]))
        self.assertContains(response, "Lawn A: skipped in Smart")
        self.assertContains(response, "Lawn B: skipped in Smart")
        self.assertNotContains(response, "Skip: no dose")
        occurrence = self.occurrence(rule)
        occurrence.status = "SKIPPED"
        occurrence.outcome = "All members are skipped because watering rates are unavailable."
        occurrence.decision = response.context["decision"]
        occurrence.save()
        response = self.client.get(reverse("logs"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Lawn A: skipped in Smart")
        self.assertContains(response, "N/A")
        Valve.objects.update(application_rate_mm_h=12)
        response = self.client.get(reverse("dashboard"))
        self.assertFalse(any("skipped in Smart" in warning for warning in response.context["quality_warnings"]))
        self.assertContains(self.client.get(reverse("logs")), "Lawn A: skipped in Smart")

    def test_runtime_unattempted_skip_reason_shows_in_occurrence_history(self):
        from apps.irrigation import balance

        rule = self.group()
        occurrence = self.occurrence(rule)
        occurrence.decision = balance.build_smart_decision(
            self.site, list(rule.members.select_related("valve")), timezone.now()
        )
        occurrence.save()
        reason = "Lawn A: skipped in Smart. Enter a measured watering rate."
        IrrigationRun.objects.create(
            occurrence=occurrence, valve=self.a, pass_number=1, trigger="SCHEDULED",
            status="FAILED", max_duration_seconds=900, error_message=reason,
        )
        self.assertContains(self.client.get(reverse("dashboard")), reason)
        self.assertContains(self.client.get(reverse("logs")), reason)

    def test_blank_smart_override_resolves_server_side_valve_default(self):
        response = self.client.post(reverse("schedule_create"), self.payload(
            "SMART", **{"members-0-duration_seconds": ""}
        ))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(GroupedRuleValve.objects.get().duration_seconds, 900)

    def test_smart_override_may_be_above_or_below_valve_default(self):
        # Zero peak isolates duration bounds from same-day sequence limits.
        self.curve.max_mm = self.curve.min_mm = 0
        self.curve.save()
        for seconds in (1, 3276):
            with self.subTest(seconds=seconds):
                response = self.client.post(reverse("schedule_create"), self.payload(
                    "SMART", enabled="", **{"members-0-duration_seconds": str(seconds)}
                ))
                self.assertEqual(response.status_code, 302)
                self.assertEqual(GroupedRuleValve.objects.order_by("-pk").first().duration_seconds, seconds)

    def test_invalid_valve_default_requires_explicit_smart_override(self):
        Valve.objects.filter(pk=self.a.pk).update(default_max_duration_seconds=5000)
        response = self.client.post(reverse("schedule_create"), self.payload(
            "SMART", **{"members-0-duration_seconds": ""}
        ))
        self.assertContains(response, "valve default is outside 1–3276")
        self.assertContains(response, 'href="#id_members-0-duration_seconds"')
        self.assertFalse(GroupedRule.objects.exists())

    def test_saved_and_copied_duration_survive_valve_default_changes(self):
        rule = self.group("FIXED")
        Valve.objects.update(default_max_duration_seconds=1200)
        response = self.client.get(reverse("group_edit", args=[rule.pk]))
        self.assertEqual(response.context["members_formset"].forms[0]["duration_seconds"].value(), 900)
        response = self.client.post(reverse("group_copy", args=[rule.pk]), self.payload(
            valves=[self.a, self.b], start_time="12:00"
        ))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(GroupedRule.objects.exclude(pk=rule.pk).get().members.first().duration_seconds, 900)

    def test_invalid_save_has_summary_linked_highlighted_errors_and_preserves_input(self):
        data = self.payload("SMART", **{
            "members-0-duration_seconds": "not a number", "start_time": "",
            "days_of_week": [], "note": "Keep this note",
        })
        response = self.client.post(reverse("schedule_create"), data)
        self.assertContains(response, "Rule was not saved. Correct the highlighted fields.")
        for target in ("id_start_time", "id_days_of_week", "id_members-0-duration_seconds"):
            self.assertContains(response, f'href="#{target}"')
        self.assertContains(response, 'aria-invalid="true"')
        self.assertContains(response, 'is-invalid')
        self.assertContains(response, 'value="not a number"')
        self.assertContains(response, 'value="Keep this note"')
        self.assertContains(response, 'novalidate')
        self.assertContains(response, 'document.getElementById("editorErrors")?.focus()')
        self.assertEqual(response.context["form"]["mode"].value(), "SMART")
        self.assertEqual(response.context["form"]["days_of_week"].value(), [])
        self.assertFalse(GroupedRule.objects.exists())

    def test_blank_added_row_is_not_silently_ignored(self):
        data = self.payload()
        data["members-TOTAL_FORMS"] = "2"
        response = self.client.post(reverse("schedule_create"), data)
        self.assertContains(response, "Select a valve or remove this row.")
        self.assertContains(response, 'href="#id_members-1-valve"')
        self.assertFalse(ScheduleRule.objects.exists())

    def test_deleted_invalid_row_does_not_block_valid_save(self):
        data = self.payload()
        data.update({
            "members-TOTAL_FORMS": "2", "members-1-valve": "invalid",
            "members-1-duration_seconds": "nonsense", "members-1-ORDER": "bad",
            "members-1-DELETE": "on",
        })
        response = self.client.post(reverse("schedule_create"), data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ScheduleRule.objects.get().valve_id, self.a.pk)

    def test_missing_management_data_has_visible_summary_and_no_partial_save(self):
        data = self.payload()
        del data["members-TOTAL_FORMS"]
        response = self.client.post(reverse("schedule_create"), data)
        self.assertContains(response, "Rule was not saved.")
        self.assertContains(response, 'href="#memberRows"')
        self.assertFalse(ScheduleRule.objects.exists())

    def test_invalid_fixed_runtime_is_attached_to_member_field(self):
        response = self.client.post(reverse("schedule_create"), self.payload(
            **{"members-0-duration_seconds": "30"}
        ))
        self.assertContains(response, "Enter 60–3276 seconds.")
        self.assertContains(response, "Runtime (seconds)")
        self.assertNotContains(response, "Maximum per run")
        self.assertContains(response, 'value="30"')
        self.assertFalse(ScheduleRule.objects.exists())

    def test_missing_rate_submission_is_preserved_visibly_in_smart_selector(self):
        Valve.objects.filter(pk=self.a.pk).update(application_rate_mm_h=None)
        response = self.client.post(reverse("schedule_create"), self.payload("SMART"))
        options = response.context["members_formset"].forms[0]["valve"].subwidgets
        option = next(item.data for item in options if str(item.data["value"]) == str(self.a.pk))
        self.assertTrue(option["selected"])
        self.assertNotIn("hidden", option["attrs"])
        self.assertNotIn("disabled", option["attrs"])
        self.assertContains(response, 'href="#id_members-0-valve"')

    def test_all_missing_rates_calendar_is_start_marker_without_fake_duration(self):
        self.group()
        Valve.objects.update(application_rate_mm_h=None)
        event, = self.client.get(reverse("calendar_events"), {
            "start": "2026-09-21", "end": "2026-09-22",
        }).json()
        self.assertNotIn("end", event)
        self.assertFalse(event["available"])
        self.assertIn("duration N/A", event["title"])
        self.assertEqual(event["unavailable_valves"], ["Lawn A", "Lawn B"])

    def test_invalid_peak_renders_actionable_calendar_error_instead_of_500(self):
        self.group()
        Valve.objects.update(application_rate_mm_h=0.000001)
        response = self.client.get(reverse("schedule"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Duration N/A")
        event, = self.client.get(reverse("calendar_events"), {
            "start": "2026-09-21", "end": "2026-09-22",
        }).json()
        self.assertTrue(event["reservation_error"])
        self.assertNotIn("end", event)
