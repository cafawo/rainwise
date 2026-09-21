from __future__ import annotations

import math
import os

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.core.exceptions import ValidationError
from django.db import models

from apps.irrigation.curves import (
    DEFAULT_G,
    DEFAULT_M,
    DEFAULT_MAX_MM,
    DEFAULT_MIN_MM,
)
from apps.irrigation.timezones import is_valid_timezone_name


RELAY_FLASH_TICKS_PER_SECOND = 10
RELAY_FLASH_MAX_TICKS = 0x7FFF
RELAY_FLASH_MAX_DURATION_SECONDS = (
    RELAY_FLASH_MAX_TICKS // RELAY_FLASH_TICKS_PER_SECOND
)
RELAY_FLASH_MIN_DURATION_SECONDS = 1


def normalize_rule_mode(value: str) -> str:
    """Interpret the retired stored spelling without accepting it as a choice."""
    return "FIXED" if value == "DYNAMIC" else value


def validate_finite(value: float) -> None:
    if not math.isfinite(value):
        raise ValidationError("Enter a finite number.")


def validate_application_rate(value: float) -> None:
    validate_finite(value)
    if value <= 0:
        raise ValidationError("Application rate must be positive.")


class Site(models.Model):
    admission_version = models.PositiveBigIntegerField(default=0, editable=False)
    name = models.CharField(max_length=100)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    timezone = models.CharField(max_length=64, default=settings.TIME_ZONE)
    active_schedule = models.ForeignKey(
        "Schedule",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="active_sites",
    )

    def clean(self) -> None:
        super().clean()
        timezone_name = (self.timezone or "").strip()
        if not is_valid_timezone_name(timezone_name):
            raise ValidationError({"timezone": "Select a valid IANA timezone."})
        self.timezone = timezone_name

    def __str__(self) -> str:
        return self.name


class CurveSettings(models.Model):
    site = models.OneToOneField(
        Site, on_delete=models.CASCADE, related_name="curve_settings"
    )
    min_mm = models.FloatField(default=DEFAULT_MIN_MM, validators=[validate_finite])
    max_mm = models.FloatField(default=DEFAULT_MAX_MM, validators=[validate_finite])
    g = models.FloatField(default=DEFAULT_G, validators=[validate_finite])
    m = models.FloatField(default=DEFAULT_M, validators=[validate_finite])
    coverage_days = models.PositiveSmallIntegerField(
        default=2, validators=[MinValueValidator(1), MaxValueValidator(7)]
    )
    fallback_temperature_c = models.FloatField(
        null=True, blank=True, validators=[validate_finite]
    )
    updated_at = models.DateTimeField(auto_now=True)

    def clean_fields(self, exclude=None):
        if (
            isinstance(self.coverage_days, float)
            and not self.coverage_days.is_integer()
        ):
            raise ValidationError({
                "coverage_days": "Enter a whole number from 1 to 7."
            })
        super().clean_fields(exclude=exclude)

    def clean(self) -> None:
        super().clean()
        errors = {}
        for field in ("min_mm", "max_mm", "g", "m"):
            value = getattr(self, field)
            if value is None or not math.isfinite(value):
                errors[field] = "Enter a finite number."
        if not errors:
            if not 0 <= self.min_mm <= self.max_mm:
                errors["min_mm"] = "Require 0 ≤ minimum ≤ maximum."
            if self.g <= 0:
                errors["g"] = "Growth rate must be positive."
        if (
            not isinstance(self.coverage_days, int)
            or isinstance(self.coverage_days, bool)
            or not 1 <= self.coverage_days <= 7
        ):
            errors["coverage_days"] = "Enter a whole number from 1 to 7."
        if self.fallback_temperature_c is not None and not math.isfinite(
            self.fallback_temperature_c
        ):
            errors["fallback_temperature_c"] = "Enter a finite number."
        if errors:
            raise ValidationError(errors)

    def __str__(self) -> str:
        return f"Curve settings ({self.site.name})"


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
    application_rate_mm_h = models.FloatField(
        null=True, blank=True, validators=[validate_application_rate]
    )
    relay_device = models.ForeignKey(RelayDevice, on_delete=models.CASCADE)
    channel = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(8)]
    )
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    is_active_high = models.BooleanField(default=True)
    default_max_duration_seconds = models.PositiveIntegerField(
        default=1800,
        validators=[
            MinValueValidator(RELAY_FLASH_MIN_DURATION_SECONDS),
            MaxValueValidator(RELAY_FLASH_MAX_DURATION_SECONDS),
        ],
    )
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


class Schedule(models.Model):
    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="schedules")
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["site", "name"], name="unique_schedule_name"
            )
        ]
        ordering = ["name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.site.name})"


class ScheduleRule(models.Model):
    MODE_FIXED = "FIXED"

    MODE_CHOICES = [
        (MODE_FIXED, "Fixed"),
    ]
    DAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    schedule = models.ForeignKey(
        Schedule, on_delete=models.CASCADE, related_name="rules"
    )
    valve = models.ForeignKey(Valve, on_delete=models.CASCADE)
    enabled = models.BooleanField(default=True)
    days_of_week_mask = models.PositiveIntegerField(default=0)
    start_time = models.TimeField()
    mode = models.CharField(max_length=10, choices=MODE_CHOICES)
    max_duration_seconds = models.PositiveIntegerField(
        validators=[
            MinValueValidator(60),
            MaxValueValidator(RELAY_FLASH_MAX_DURATION_SECONDS),
        ]
    )
    note = models.CharField(max_length=255, blank=True)

    def clean_fields(self, exclude=None):
        self.mode = normalize_rule_mode(self.mode)
        super().clean_fields(exclude=exclude)

    def clean(self) -> None:
        super().clean()
        self.mode = normalize_rule_mode(self.mode)
        if self.schedule_id and self.valve_id:
            valve_site_id = self.valve.relay_device.site_id
            if self.schedule.site_id != valve_site_id:
                raise ValidationError(
                    "Schedule and valve must belong to the same site."
                )

    def save(self, *args, **kwargs):
        mode = normalize_rule_mode(self.mode)
        if mode != self.mode and kwargs.get("update_fields") is not None:
            kwargs["update_fields"] = set(kwargs["update_fields"]) | {"mode"}
        self.mode = mode
        return super().save(*args, **kwargs)

    def get_mode_display(self):
        return dict(self.MODE_CHOICES).get(normalize_rule_mode(self.mode), self.mode)

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


class GroupedRule(models.Model):
    MODE_FIXED = "FIXED"
    MODE_SMART = "SMART"
    MODE_CHOICES = [(MODE_FIXED, "Fixed"), (MODE_SMART, "Smart")]
    DAY_LABELS = ScheduleRule.DAY_LABELS

    schedule = models.ForeignKey(
        Schedule, on_delete=models.CASCADE, related_name="grouped_rules"
    )
    mode = models.CharField(max_length=10, choices=MODE_CHOICES)
    note = models.CharField(max_length=255, blank=True)
    enabled = models.BooleanField(default=True)
    days_of_week_mask = models.PositiveIntegerField(default=127)
    start_time = models.TimeField()

    def clean(self):
        super().clean()
        if not 1 <= self.days_of_week_mask <= 127:
            raise ValidationError({"days_of_week_mask": "Select at least one weekday."})

    uses_weekday = ScheduleRule.uses_weekday
    days_display = ScheduleRule.days_display

    def __str__(self):
        return self.note or f"{self.get_mode_display()} @ {self.start_time}"


class GroupedRuleValve(models.Model):
    rule = models.ForeignKey(
        GroupedRule, on_delete=models.CASCADE, related_name="members"
    )
    valve = models.ForeignKey(Valve, on_delete=models.CASCADE)
    order = models.PositiveSmallIntegerField()
    duration_seconds = models.PositiveIntegerField(
        validators=[
            MinValueValidator(1),
            MaxValueValidator(RELAY_FLASH_MAX_DURATION_SECONDS),
        ]
    )

    class Meta:
        ordering = ["order", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["rule", "valve"], name="unique_group_valve"
            ),
            models.UniqueConstraint(
                fields=["rule", "order"], name="unique_group_order"
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.rule_id and self.valve_id:
            if self.rule.schedule.site_id != self.valve.relay_device.site_id:
                errors["valve"] = "Rule and valve must belong to the same site."
            if self.rule.mode == GroupedRule.MODE_SMART:
                if self.duration_seconds > self.valve.default_max_duration_seconds:
                    errors["duration_seconds"] = (
                        "Smart maximum exceeds the valve limit."
                    )
                rate = self.valve.application_rate_mm_h
                if rate is None or not math.isfinite(rate) or rate <= 0:
                    errors["valve"] = (
                        "Smart requires a positive measured application rate."
                    )
                if self.rule.enabled and GroupedRuleValve.objects.filter(
                    valve_id=self.valve_id,
                    rule__schedule_id=self.rule.schedule_id,
                    rule__enabled=True,
                    rule__mode=GroupedRule.MODE_SMART,
                ).exclude(rule_id=self.rule_id).exists():
                    errors["valve"] = "Valve already belongs to an enabled Smart rule."
            elif (
                self.rule.mode == GroupedRule.MODE_FIXED
                and self.duration_seconds < 60
            ):
                errors["duration_seconds"] = (
                    "Fixed runtime must be at least 60 seconds."
                )
        if errors:
            raise ValidationError(errors)


class RuleOccurrence(models.Model):
    SOURCE_SCHEDULED = "SCHEDULED"
    SOURCE_MANUAL = "MANUAL"
    STATUS_PENDING = "PENDING"
    STATUS_ACTIVE = "ACTIVE"
    STATUS_STOPPING = "STOPPING"
    STATUS_FINISHED = "FINISHED"
    STATUS_SKIPPED = "SKIPPED"
    STATUS_CANCELLED = "CANCELLED"
    STATUS_FAILED = "FAILED"
    STATUS_ZERO = "ZERO"
    STATUS_CHOICES = [(value, value.title()) for value in (
        "PENDING", "ACTIVE", "STOPPING", "FINISHED", "SKIPPED",
        "CANCELLED", "FAILED", "ZERO",
    )]

    site = models.ForeignKey(Site, on_delete=models.CASCADE)
    rule = models.ForeignKey(
        GroupedRule, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="occurrences",
    )
    mode = models.CharField(max_length=10, choices=GroupedRule.MODE_CHOICES)
    config = models.JSONField(default=dict)
    decision = models.JSONField(default=dict)
    scheduled_local_date = models.DateField(null=True, blank=True)
    scheduled_at = models.DateTimeField(null=True, blank=True)
    requested_at = models.DateTimeField()
    decision_at = models.DateTimeField(null=True, blank=True)
    reservation_end = models.DateTimeField(null=True, blank=True)
    source = models.CharField(max_length=10, choices=[
        (SOURCE_SCHEDULED, "Scheduled"), (SOURCE_MANUAL, "Manual"),
    ])
    status = models.CharField(
        max_length=10, choices=STATUS_CHOICES, default=STATUS_PENDING
    )
    cancellation_requested = models.BooleanField(default=False)
    outcome = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["rule", "scheduled_local_date"], name="unique_rule_local_date"
            ),
        ]
        indexes = [
            models.Index(fields=["site", "status"], name="occurrence_site_status")
        ]

    def __str__(self):
        return f"{self.get_mode_display()} {self.requested_at} ({self.status})"


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
    occurrence = models.ForeignKey(
        RuleOccurrence, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="runs",
    )
    pass_number = models.PositiveSmallIntegerField(null=True, blank=True)
    member_order = models.PositiveSmallIntegerField(null=True, blank=True)
    attempt_started_at = models.DateTimeField(null=True, blank=True)
    attempt_finished_at = models.DateTimeField(null=True, blank=True)
    application_rate_mm_h = models.FloatField(null=True, blank=True)
    delivery_uncertain = models.BooleanField(default=False)
    closure_confirmed_at = models.DateTimeField(null=True, blank=True)
    cancellation_requested = models.BooleanField(default=False)
    trigger = models.CharField(max_length=10, choices=TRIGGER_CHOICES)
    requested_start_at = models.DateTimeField(null=True, blank=True)
    planned_start_at = models.DateTimeField(null=True, blank=True)
    actual_start_at = models.DateTimeField(null=True, blank=True)
    optimal_duration_seconds = models.PositiveIntegerField(
        null=True,
        blank=True,
        validators=[
            MinValueValidator(RELAY_FLASH_MIN_DURATION_SECONDS),
            MaxValueValidator(RELAY_FLASH_MAX_DURATION_SECONDS),
        ],
    )
    max_duration_seconds = models.PositiveIntegerField(
        validators=[
            MinValueValidator(RELAY_FLASH_MIN_DURATION_SECONDS),
            MaxValueValidator(RELAY_FLASH_MAX_DURATION_SECONDS),
        ]
    )
    actual_stop_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES)
    stop_reason = models.CharField(
        max_length=20, choices=STOP_CHOICES, null=True, blank=True
    )
    error_message = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["occurrence", "valve", "pass_number"],
                name="unique_occurrence_valve_pass",
            )
        ]
        indexes = [
            models.Index(fields=["status"], name="irrigation__status_idx"),
            models.Index(fields=["planned_start_at"], name="irrigation__planned_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.valve.name} ({self.status})"
