from __future__ import annotations

from django.db import models

from apps.irrigation.models import Site


class WeatherObservation(models.Model):
    site = models.ForeignKey(Site, on_delete=models.CASCADE)
    timestamp = models.DateTimeField()
    temperature_c = models.FloatField(null=True, blank=True)
    precipitation_mm = models.FloatField(null=True, blank=True)
    humidity_percent = models.FloatField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["site", "timestamp"], name="unique_site_timestamp"
            )
        ]

    def __str__(self) -> str:
        return f"{self.site.name} {self.timestamp}"


class WeatherImportLog(models.Model):
    STATUS_SUCCESS = "SUCCESS"
    STATUS_FAILED = "FAILED"

    STATUS_CHOICES = [
        (STATUS_SUCCESS, "Success"),
        (STATUS_FAILED, "Failed"),
    ]

    site = models.ForeignKey(Site, on_delete=models.CASCADE)
    date = models.DateField()
    imported_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES)
    error_message = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["site", "date"], name="unique_site_date"
            )
        ]

    def __str__(self) -> str:
        return f"{self.site.name} {self.date}"
