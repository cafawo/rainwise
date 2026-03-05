from django.contrib import admin

from apps.weather import models


@admin.register(models.WeatherObservation)
class WeatherObservationAdmin(admin.ModelAdmin):
    list_display = ("site", "timestamp", "temperature_c", "precipitation_mm")
    list_filter = ("site",)


@admin.register(models.WeatherImportLog)
class WeatherImportLogAdmin(admin.ModelAdmin):
    list_display = ("site", "date", "status", "imported_at")
    list_filter = ("status", "site")
