from django.contrib import admin

from apps.irrigation import models


@admin.register(models.Site)
class SiteAdmin(admin.ModelAdmin):
    list_display = ("name", "timezone", "latitude", "longitude")


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
        "valve",
        "enabled",
        "start_time",
        "mode",
        "fixed_duration_seconds",
        "max_duration_seconds",
    )
    list_filter = ("enabled", "mode")


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
