from django import forms
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.urls import reverse
from django.utils.html import format_html

from apps.irrigation import group_services, models
from apps.irrigation.timezones import site_timezone_choices


class SiteAdminForm(forms.ModelForm):
    timezone = forms.ChoiceField(choices=site_timezone_choices())

    class Meta:
        model = models.Site
        exclude = ("admission_version",)

    def clean_active_schedule(self):
        schedule = self.cleaned_data.get("active_schedule")
        if schedule:
            if self.instance.pk and schedule.site_id != self.instance.pk:
                raise ValidationError("The active schedule must belong to this site.")
            group_services.validate_schedule(schedule)
        return schedule


@admin.register(models.Site)
class SiteAdmin(admin.ModelAdmin):
    form = SiteAdminForm
    list_display = ("name", "timezone", "latitude", "longitude", "active_schedule")

    def save_model(self, request, obj, form, change):
        if change:
            with group_services.site_admission(obj):
                previous = models.Site.objects.get(pk=obj.pk)
                if previous.active_schedule_id != obj.active_schedule_id:
                    group_services.cancel_site_groups(obj, "Active schedule changed")
                # Preserve the admission counter increment from this transaction.
                obj.admission_version = previous.admission_version
                super().save_model(request, obj, form, change)
        else:
            super().save_model(request, obj, form, change)



def _has_unresolved_runs(query):
    return query.filter(
        Q(status="RUNNING")
        | Q(attempt_started_at__isnull=False, closure_confirmed_at__isnull=True)
    ).exists()


def _validate_reservation_change(candidate, site):
    """Check the proposed values without persisting a failed admin form.

    The enclosing admin transaction retains admission ownership through the
    actual save. Only the inner probe is rolled back, including on success.
    """
    candidate.full_clean()
    with group_services.site_admission(site):
        with transaction.atomic():
            candidate.save()
            for schedule in models.Schedule.objects.filter(site=site):
                group_services.validate_schedule(schedule)
            transaction.set_rollback(True)


class RelayDeviceAdminForm(forms.ModelForm):
    class Meta:
        model = models.RelayDevice
        fields = "__all__"

    def clean(self):
        cleaned = super().clean()
        if self.instance.pk and any(
            field in self.changed_data for field in ("host", "port", "unit_id", "site")
        ):
            original = models.RelayDevice.objects.select_related("site").get(
                pk=self.instance.pk
            )
            # Admin keeps its surrounding transaction until save completes.
            with group_services.site_admission(original.site):
                if _has_unresolved_runs(models.IrrigationRun.objects.filter(
                    valve__relay_device=original
                )):
                    raise ValidationError(
                        "Stop watering and wait for confirmed closure before "
                        "changing relay hardware identity."
                    )
        return cleaned


class ValveAdminForm(forms.ModelForm):
    class Meta:
        model = models.Valve
        exclude = ("last_known_is_open", "last_polled_at")
        labels = {
            "default_max_duration_seconds": "Manual runtime / new rule default (seconds)",
            "application_rate_mm_h": "Measured watering rate (mm/hour)",
        }

    def clean(self):
        cleaned = super().clean()
        if self.instance.pk and any(
            field in self.changed_data
            for field in ("relay_device", "channel", "is_active_high")
        ):
            original = models.Valve.objects.select_related(
                "relay_device__site"
            ).get(pk=self.instance.pk)
            with group_services.site_admission(original.relay_device.site):
                if _has_unresolved_runs(models.IrrigationRun.objects.filter(
                    valve=original
                )):
                    raise ValidationError(
                        "Stop watering and wait for confirmed closure before "
                        "changing valve hardware identity."
                    )
        if (
            not self.errors and self.instance.pk
            and "application_rate_mm_h" in self.changed_data
        ):
            candidate = models.Valve.objects.select_related(
                "relay_device__site"
            ).get(pk=self.instance.pk)
            site = candidate.relay_device.site
            for name, value in cleaned.items():
                setattr(candidate, name, value)
            _validate_reservation_change(candidate, site)
        return cleaned


@admin.register(models.RelayDevice)
class RelayDeviceAdmin(admin.ModelAdmin):
    form = RelayDeviceAdminForm
    list_display = ("name", "host", "port", "unit_id", "enabled")
    list_filter = ("enabled",)

    def save_model(self, request, obj, form, change):
        with group_services.site_admission(obj.site):
            if not obj.enabled and change:
                occurrences = models.RuleOccurrence.objects.filter(
                    site=obj.site, status__in=group_services.RESERVED_STATUSES
                ).filter(
                    Q(runs__valve__relay_device=obj)
                    | Q(rule__members__valve__relay_device=obj)
                ).distinct()
                for occurrence in occurrences:
                    group_services.cancel_occurrence(occurrence, "Relay disabled")
            super().save_model(request, obj, form, change)


@admin.register(models.Valve)
class ValveAdmin(admin.ModelAdmin):
    form = ValveAdminForm
    readonly_fields = ("last_known_is_open", "last_polled_at")
    list_display = (
        "name",
        "relay_device",
        "channel",
        "is_active_high",
        "default_max_duration_seconds",
        "application_rate_mm_h",
        "last_known_is_open",
        "last_polled_at",
    )
    list_filter = ("relay_device", "is_active_high")


class RuleConfigurationAdmin(admin.ModelAdmin):
    """Use the common editor for mutations, including cancellation and conflicts."""
    actions = None

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_readonly_fields(self, request, obj=None):
        return [field.name for field in self.model._meta.fields] + ["edit_rule"]

    @admin.display(description="Rule editor")
    def edit_rule(self, obj):
        prefix = "group" if isinstance(obj, models.GroupedRule) else "schedule"
        return format_html(
            '<a href="{}">Open shared rule editor</a>',
            reverse(prefix + "_edit", args=[obj.pk]),
        )


@admin.register(models.ScheduleRule)
class ScheduleRuleAdmin(RuleConfigurationAdmin):
    list_display = (
        "schedule", "valve", "enabled", "start_time", "fixed_mode",
        "runtime",
    )
    list_filter = ("enabled", "schedule")
    exclude = ("mode", "max_duration_seconds")

    @admin.display(description="Runtime (seconds)")
    def runtime(self, obj):
        return obj.max_duration_seconds

    @admin.display(description="Mode")
    def fixed_mode(self, obj):
        return models.normalize_rule_mode(obj.mode).title()

    def get_readonly_fields(self, request, obj=None):
        fields = super().get_readonly_fields(request, obj)
        return [field for field in fields if field not in {"mode", "max_duration_seconds"}] + ["fixed_mode", "runtime"]


@admin.register(models.GroupedRule)
class GroupedRuleAdmin(RuleConfigurationAdmin):
    list_display = ("schedule", "note", "mode", "enabled", "start_time", "ordered_valves")
    list_filter = ("enabled", "mode", "schedule")
    readonly_fields = ("ordered_valves",)

    @admin.display(description="Valves in order")
    def ordered_valves(self, obj):
        members = obj.members.select_related("valve").order_by("order")
        return " → ".join(
            f"{member.valve.name} ({member.duration_seconds}s)" for member in members
        )

    def get_readonly_fields(self, request, obj=None):
        return super().get_readonly_fields(request, obj) + ["ordered_valves"]


@admin.register(models.RuleOccurrence)
class RuleOccurrenceAdmin(admin.ModelAdmin):
    list_display = ("site", "mode", "scheduled_local_date", "requested_at", "status", "outcome")
    list_filter = ("site", "mode", "status")
    actions = None

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(models.Schedule)
class ScheduleAdmin(admin.ModelAdmin):
    list_display = ("name", "site", "created_at", "description")
    list_filter = ("site",)

    def get_readonly_fields(self, request, obj=None):
        return ("site",) if obj else ()

    def has_delete_permission(self, request, obj=None):
        # Schedule configuration deletion must not cascade active groups silently.
        return False


@admin.register(models.IrrigationRun)
class IrrigationRunAdmin(admin.ModelAdmin):
    list_display = (
        "valve",
        "trigger",
        "status",
        "planned_start_at",
        "actual_start_at",
        "actual_stop_at",
        "stop_reason",
    )
    list_filter = ("status", "trigger", "stop_reason")
    actions = None

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False



class CurveSettingsAdminForm(forms.ModelForm):
    fallback_temperature_c = forms.FloatField(
        required=False, initial=models.DEFAULT_FALLBACK_TEMPERATURE_C,
        label="Fallback temperature (°C)",
        help_text="Leave blank for the default of 25 °C.",
    )

    class Meta:
        model = models.CurveSettings
        fields = "__all__"

    def clean_fallback_temperature_c(self):
        value = self.cleaned_data.get("fallback_temperature_c")
        return models.DEFAULT_FALLBACK_TEMPERATURE_C if value is None else value

    def clean(self):
        cleaned = super().clean()
        if self.errors:
            return cleaned
        candidate = (
            models.CurveSettings.objects.get(pk=self.instance.pk)
            if self.instance.pk else models.CurveSettings()
        )
        for name, value in cleaned.items():
            setattr(candidate, name, value)
        if candidate.site_id:
            _validate_reservation_change(candidate, candidate.site)
        return cleaned


@admin.register(models.CurveSettings)
class CurveSettingsAdmin(admin.ModelAdmin):
    form = CurveSettingsAdminForm
    list_display = (
        "site", "min_mm", "max_mm", "g", "m", "coverage_days",
        "fallback_temperature_c", "updated_at",
    )

    def get_readonly_fields(self, request, obj=None):
        return ("site",) if obj else ()
