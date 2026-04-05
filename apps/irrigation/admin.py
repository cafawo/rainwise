from django import forms
from django.contrib import admin

from apps.irrigation import models
from apps.irrigation.timezones import site_timezone_choices


class SiteAdminForm(forms.ModelForm):
    timezone = forms.ChoiceField(choices=site_timezone_choices())

    class Meta:
        model = models.Site
        fields = "__all__"


@admin.register(models.Site)
class SiteAdmin(admin.ModelAdmin):
    form = SiteAdminForm
    list_display = ("name", "timezone", "latitude", "longitude", "active_schedule")


@admin.register(models.RelayDevice)
class RelayDeviceAdmin(admin.ModelAdmin):
    list_display = ("name", "host", "port", "unit_id", "enabled")
    list_filter = ("enabled",)


@admin.register(models.Valve)
class ValveAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "relay_device",
        "channel",
        "is_active_high",
        "default_max_duration_seconds",
        "last_known_is_open",
        "last_polled_at",
    )
    list_filter = ("relay_device", "is_active_high")


@admin.register(models.ScheduleRule)
class ScheduleRuleAdmin(admin.ModelAdmin):
    list_display = (
        "schedule",
        "valve",
        "enabled",
        "start_time",
        "mode",
        "max_duration_seconds",
    )
    list_filter = ("enabled", "mode", "schedule")


@admin.register(models.Schedule)
class ScheduleAdmin(admin.ModelAdmin):
    list_display = ("name", "site", "created_at", "description")
    list_filter = ("site",)


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


@admin.register(models.CurveSettings)
class CurveSettingsAdmin(admin.ModelAdmin):
    list_display = ("site", "min_mm", "max_mm", "g", "m", "updated_at")
