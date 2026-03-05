from __future__ import annotations

import os

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


class Site(models.Model):
    name = models.CharField(max_length=100)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    timezone = models.CharField(max_length=64, default=settings.TIME_ZONE)

    def __str__(self) -> str:
        return self.name


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def default_modbus_port() -> int:
    return _env_int("MODBUS_DEFAULT_PORT", 502)


def default_modbus_unit_id() -> int:
    return _env_int("MODBUS_DEFAULT_UNIT_ID", 1)


class RelayDevice(models.Model):
    site = models.ForeignKey(Site, on_delete=models.CASCADE)
    name = models.CharField(max_length=100)
    host = models.CharField(max_length=255)
    port = models.PositiveIntegerField(default=default_modbus_port)
    unit_id = models.PositiveIntegerField(default=default_modbus_unit_id)
    enabled = models.BooleanField(default=True)

    def __str__(self) -> str:
        return f"{self.name} ({self.host})"


class Valve(models.Model):
    relay_device = models.ForeignKey(RelayDevice, on_delete=models.CASCADE)
    channel = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(8)]
    )
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    is_active_high = models.BooleanField(default=True)
    default_max_duration_seconds = models.PositiveIntegerField(default=1800)
    last_known_is_open = models.BooleanField(default=False)
    last_polled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["relay_device", "channel"], name="unique_relay_channel"
            )
        ]

    def __str__(self) -> str:
        return f"{self.name} (Ch {self.channel})"


class ScheduleRule(models.Model):
    MODE_FIXED = "FIXED"
    MODE_DYNAMIC = "DYNAMIC"

    MODE_CHOICES = [
        (MODE_FIXED, "Fixed"),
        (MODE_DYNAMIC, "Dynamic"),
    ]
    DAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    valve = models.ForeignKey(Valve, on_delete=models.CASCADE)
    enabled = models.BooleanField(default=True)
    days_of_week_mask = models.PositiveIntegerField(default=0)
    start_time = models.TimeField()
    mode = models.CharField(max_length=10, choices=MODE_CHOICES)
    fixed_duration_seconds = models.PositiveIntegerField(null=True, blank=True)
    max_duration_seconds = models.PositiveIntegerField(
        validators=[MinValueValidator(60)]
    )
    note = models.CharField(max_length=255, blank=True)

    def clean(self) -> None:
        super().clean()
        if self.mode == self.MODE_FIXED and not self.fixed_duration_seconds:
            raise ValidationError("Fixed duration is required for FIXED mode.")
        if self.mode == self.MODE_DYNAMIC and self.fixed_duration_seconds:
            raise ValidationError("Fixed duration should be empty for DYNAMIC mode.")

    def uses_weekday(self, weekday: int) -> bool:
        return bool(self.days_of_week_mask & (1 << weekday))

    def days_display(self) -> str:
        days = [
            label
            for idx, label in enumerate(self.DAY_LABELS)
            if self.uses_weekday(idx)
        ]
        return ", ".join(days) if days else "-"

    def __str__(self) -> str:
        return f"{self.valve.name} @ {self.start_time}"


class IrrigationRun(models.Model):
    TRIGGER_SCHEDULED = "SCHEDULED"
    TRIGGER_MANUAL = "MANUAL"
    TRIGGER_FAILSAFE = "FAILSAFE"
    TRIGGER_RECOVERY = "RECOVERY"

    TRIGGER_CHOICES = [
        (TRIGGER_SCHEDULED, "Scheduled"),
        (TRIGGER_MANUAL, "Manual"),
        (TRIGGER_FAILSAFE, "Failsafe"),
        (TRIGGER_RECOVERY, "Recovery"),
    ]

    STATUS_PLANNED = "PLANNED"
    STATUS_RUNNING = "RUNNING"
    STATUS_FINISHED = "FINISHED"
    STATUS_FAILED = "FAILED"

    STATUS_CHOICES = [
        (STATUS_PLANNED, "Planned"),
        (STATUS_RUNNING, "Running"),
        (STATUS_FINISHED, "Finished"),
        (STATUS_FAILED, "Failed"),
    ]

    STOP_COMPLETED = "COMPLETED"
    STOP_MANUAL = "MANUAL_STOP"
    STOP_FAILSAFE = "FAILSAFE_TIMEOUT"
    STOP_ERROR = "ERROR"

    STOP_CHOICES = [
        (STOP_COMPLETED, "Completed"),
        (STOP_MANUAL, "Manual stop"),
        (STOP_FAILSAFE, "Failsafe timeout"),
        (STOP_ERROR, "Error"),
    ]

    valve = models.ForeignKey(Valve, on_delete=models.CASCADE)
    trigger = models.CharField(max_length=10, choices=TRIGGER_CHOICES)
    requested_start_at = models.DateTimeField(null=True, blank=True)
    planned_start_at = models.DateTimeField(null=True, blank=True)
    actual_start_at = models.DateTimeField(null=True, blank=True)
    optimal_duration_seconds = models.PositiveIntegerField(null=True, blank=True)
    max_duration_seconds = models.PositiveIntegerField()
    actual_stop_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES)
    stop_reason = models.CharField(
        max_length=20, choices=STOP_CHOICES, null=True, blank=True
    )
    error_message = models.TextField(blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["planned_start_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.valve.name} ({self.status})"
