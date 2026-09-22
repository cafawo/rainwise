from __future__ import annotations

import math

from django import forms
from django.contrib.auth.forms import AuthenticationForm

from apps.irrigation.models import (
    RELAY_FLASH_MAX_DURATION_SECONDS,
    DEFAULT_FALLBACK_TEMPERATURE_C,
    Schedule,
    GroupedRule,
    Valve,
)


DAY_CHOICES = [
    ("0", "Mon"),
    ("1", "Tue"),
    ("2", "Wed"),
    ("3", "Thu"),
    ("4", "Fri"),
    ("5", "Sat"),
    ("6", "Sun"),
]


def mask_from_days(days: list[str]) -> int:
    mask = 0
    for day in days:
        mask |= 1 << int(day)
    return mask


class RuleEditorForm(forms.Form):
    mode = forms.ChoiceField(choices=GroupedRule.MODE_CHOICES, initial="FIXED")
    enabled = forms.BooleanField(required=False, initial=True)
    days_of_week = forms.MultipleChoiceField(
        choices=DAY_CHOICES, widget=forms.CheckboxSelectMultiple,
        help_text="Smart starts with every day selected.",
        error_messages={"required": "Select at least one weekday."},
    )
    start_time = forms.TimeField(
        widget=forms.TimeInput(attrs={"type": "time"}),
        help_text="Local start time for the entire sequence.",
        error_messages={"required": "Enter a start time."},
    )
    note = forms.CharField(max_length=255, required=False, label="Name / note")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if name not in {"enabled", "days_of_week"}:
                field.widget.attrs["class"] = "form-select" if name == "mode" else "form-control"
        self.fields["enabled"].widget.attrs["class"] = "form-check-input"
        self.fields["days_of_week"].widget.attrs["class"] = "form-check-input"


class ValveSelect(forms.Select):
    def __init__(self, *args, mode="FIXED", retained_ids=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.mode = mode
        self.retained_ids = set(retained_ids)

    def create_option(self, name, value, label, selected, index, **kwargs):
        option = super().create_option(name, value, label, selected, index, **kwargs)
        if value:
            valve = value.instance
            allowed = (
                valve.has_valid_application_rate or valve.pk in self.retained_ids
            )
            option["attrs"]["data-smart-allowed"] = "true" if allowed else "false"
            option["attrs"]["data-default-seconds"] = valve.default_max_duration_seconds
            option["attrs"]["data-rate"] = (
                valve.application_rate_mm_h if valve.has_valid_application_rate else ""
            )
            if self.mode == "SMART" and not allowed and not selected:
                option["attrs"].update(disabled=True, hidden=True)
        return option


class SmartValveChoiceField(forms.ModelChoiceField):
    def __init__(self, *args, mode="FIXED", retained_ids=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.mode = mode
        self.retained_ids = set(retained_ids)

    def label_from_instance(self, valve):
        if valve.has_valid_application_rate:
            return f"{valve.name} ({valve.application_rate_mm_h:g} mm/hour)"
        suffix = "N/A — skipped in Smart" if valve.pk in self.retained_ids else "N/A"
        return f"{valve.name} (watering rate: {suffix})"

    def clean(self, value):
        valve = super().clean(value)
        if (valve and self.mode == "SMART"
                and not valve.has_valid_application_rate
                and valve.pk not in self.retained_ids):
            raise forms.ValidationError(
                f"{valve.name}: enter a measured watering rate before selecting "
                "this valve for Smart."
            )
        return valve


class ValveMemberForm(forms.Form):
    valve = forms.ModelChoiceField(queryset=Valve.objects.none())
    duration_seconds = forms.IntegerField(required=False)

    def __init__(
        self, *args, site=None, mode="FIXED", retained_ids=(), **kwargs
    ):
        super().__init__(*args, **kwargs)
        self.mode = mode
        self.fields["valve"] = SmartValveChoiceField(
            queryset=Valve.objects.filter(relay_device__site=site)
            .select_related("relay_device").order_by("name"),
            mode=mode, retained_ids=retained_ids,
            widget=ValveSelect(mode=mode, retained_ids=retained_ids),
            error_messages={
                "required": "Select a valve or remove this row.",
                "invalid_choice": "Select a valve at this site or remove this row.",
            },
        )
        minimum = 1 if mode == "SMART" else 60
        label = "Run time before a break" if mode == "SMART" else "Runtime"
        self.fields["duration_seconds"] = forms.IntegerField(
            required=False, min_value=minimum,
            max_value=RELAY_FLASH_MAX_DURATION_SECONDS, label=label,
            widget=forms.TextInput(attrs={"class": "form-control", "inputmode": "numeric"}),
            error_messages={
                "invalid": f"Enter a whole number of seconds ({minimum}–3276).",
                "min_value": f"Enter {minimum}–3276 seconds.",
                "max_value": f"Enter {minimum}–3276 seconds.",
            },
        )
        self.fields["valve"].widget.attrs["class"] = "form-select"

    def clean(self):
        cleaned = super().clean()
        if "duration_seconds" in self.errors:
            return cleaned
        duration = cleaned.get("duration_seconds")
        if duration is None:
            valve = cleaned.get("valve")
            if self.mode == "SMART" and valve:
                duration = valve.default_max_duration_seconds
                try:
                    self.fields["duration_seconds"].run_validators(duration)
                except forms.ValidationError:
                    self.add_error(
                        "duration_seconds",
                        "The valve default is outside 1–3276 seconds. Enter a valid override.",
                    )
                else:
                    cleaned["duration_seconds"] = duration
            elif self.mode != "SMART":
                self.add_error("duration_seconds", "Enter a runtime of 60–3276 seconds.")
        return cleaned


class BaseValveMemberFormSet(forms.BaseFormSet):
    default_error_messages = {
        "too_few_forms": "Select at least one valve. Add a complete valve row before saving.",
        "missing_management_form": "The valve list is incomplete. Reload the editor and try again.",
    }
    ordering_widget = forms.HiddenInput

    def add_fields(self, form, index):
        super().add_fields(form, index)
        # Every deliberately added row must be completed or explicitly removed.
        form.empty_permitted = False
        if not form.is_bound and index is not None:
            form.fields["ORDER"].initial = index + 1
        form.fields["ORDER"].error_messages["invalid"] = (
            "The valve order could not be read. Reload the editor and try again."
        )

    def clean(self):
        seen = set()
        for form in self.forms:
            values = form.cleaned_data
            if values.get("DELETE"):
                continue
            valve = values.get("valve")
            if valve is None:
                continue
            if valve.pk in seen:
                form.add_error(
                    "valve", "This valve is already selected. Choose another valve or remove this row."
                )
            seen.add(valve.pk)


ValveMemberFormSet = forms.formset_factory(
    ValveMemberForm, formset=BaseValveMemberFormSet,
    can_order=True, can_delete=True, extra=0, min_num=1, validate_min=True,
)


class LoginForm(AuthenticationForm):
    def __init__(self, request=None, *args, **kwargs) -> None:
        super().__init__(request, *args, **kwargs)
        self.fields["username"].widget.attrs.update(
            {
                "class": "form-control",
                "placeholder": "e.g. admin",
                "autocomplete": "username",
            }
        )
        self.fields["password"].widget.attrs.update(
            {
                "class": "form-control",
                "placeholder": "Your password",
                "autocomplete": "current-password",
            }
        )


class ScheduleNewForm(forms.Form):
    name = forms.CharField(
        max_length=100,
        widget=forms.TextInput(
            attrs={"class": "form-control", "placeholder": "e.g. Summer schedule"}
        ),
    )
    description = forms.CharField(
        required=False,
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 3,
                "placeholder": "Optional description",
            }
        ),
    )
    copy_current = forms.BooleanField(
        required=False,
        initial=False,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )

    def __init__(self, *args, **kwargs) -> None:
        schedules = kwargs.pop("schedules", Schedule.objects.none())
        super().__init__(*args, **kwargs)
        self._schedules = schedules

    def clean_name(self) -> str:
        name = (self.cleaned_data.get("name") or "").strip()
        if not name:
            raise forms.ValidationError("Enter a schedule name.")
        if self._schedules.filter(name__iexact=name).exists():
            raise forms.ValidationError("A schedule with this name already exists.")
        return name


class ScheduleLoadForm(forms.Form):
    schedule = forms.ModelChoiceField(
        queryset=Schedule.objects.none(),
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    def __init__(self, *args, **kwargs) -> None:
        schedules = kwargs.pop("schedules", Schedule.objects.none())
        super().__init__(*args, **kwargs)
        self.fields["schedule"].queryset = schedules


class CurveForm(forms.Form):
    min_mm = forms.FloatField(
        label="Minimum daily demand (mm/day)",
        initial=0.0,
        widget=forms.NumberInput(
            attrs={"class": "form-control", "placeholder": "e.g. 0", "step": "0.1"}
        ),
    )
    max_mm = forms.FloatField(
        label="Peak daily demand (mm/day)",
        initial=7.0,
        widget=forms.NumberInput(
            attrs={"class": "form-control", "placeholder": "e.g. 7", "step": "0.1"}
        ),
    )
    g = forms.FloatField(
        label="g",
        initial=0.1852,
        widget=forms.NumberInput(
            attrs={"class": "form-control", "placeholder": "e.g. 0.1852", "step": "0.0001"}
        ),
    )
    m = forms.FloatField(
        label="m",
        initial=25.6653,
        widget=forms.NumberInput(
            attrs={"class": "form-control", "placeholder": "e.g. 25.6653", "step": "0.0001"}
        ),
    )

    coverage_days = forms.IntegerField(
        min_value=1, max_value=7, initial=2, label="Coverage (local days)",
        widget=forms.NumberInput(attrs={"class": "form-control"}),
    )
    fallback_temperature_c = forms.FloatField(
        required=False, initial=DEFAULT_FALLBACK_TEMPERATURE_C,
        label="Fallback temperature (°C)",
        help_text="Defaults to 25 °C. Enter a site-specific value, or leave blank for 25 °C.",
        widget=forms.NumberInput(attrs={"class": "form-control", "step": "0.1"}),
    )

    def clean_fallback_temperature_c(self):
        value = self.cleaned_data.get("fallback_temperature_c")
        return DEFAULT_FALLBACK_TEMPERATURE_C if value is None else value

    def clean(self) -> dict:
        cleaned = super().clean()
        for name in ("min_mm", "max_mm", "g", "m", "fallback_temperature_c"):
            value = cleaned.get(name)
            if value is not None and not math.isfinite(value):
                self.add_error(name, "Enter a finite value.")
        min_mm = cleaned.get("min_mm")
        max_mm = cleaned.get("max_mm")
        g = cleaned.get("g")
        if min_mm is not None and min_mm < 0:
            self.add_error("min_mm", "Min must be 0 or higher.")
        if max_mm is not None and max_mm < 0:
            self.add_error("max_mm", "Max must be 0 or higher.")
        if (
            min_mm is not None
            and max_mm is not None
            and max_mm < min_mm
        ):
            self.add_error("max_mm", "Max must be greater than or equal to min.")
        if g is not None and g <= 0:
            self.add_error("g", "g must be greater than 0.")
        return cleaned
