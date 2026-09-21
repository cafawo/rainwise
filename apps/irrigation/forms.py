from __future__ import annotations

import math

from django import forms
from django.contrib.auth.forms import AuthenticationForm

from apps.irrigation.models import (
    RELAY_FLASH_MAX_DURATION_SECONDS,
    Schedule,
    ScheduleRule,
    GroupedRule,
    normalize_rule_mode,
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


class ScheduleRuleForm(forms.ModelForm):
    days_of_week = forms.MultipleChoiceField(
        choices=DAY_CHOICES, widget=forms.CheckboxSelectMultiple
    )

    class Meta:
        model = ScheduleRule
        fields = [
            "valve",
            "enabled",
            "days_of_week",
            "start_time",
            "mode",
            "max_duration_seconds",
            "note",
        ]
        widgets = {
            "start_time": forms.TimeInput(attrs={"type": "time"}),
        }

    def __init__(self, *args, **kwargs) -> None:
        site = kwargs.pop("site", None)
        super().__init__(*args, **kwargs)
        if site:
            self.fields["valve"].queryset = Valve.objects.filter(
                relay_device__site=site
            ).order_by("name")
        if self.instance and self.instance.pk:
            self.initial["mode"] = normalize_rule_mode(self.instance.mode)
            selected = [
                str(idx)
                for idx in range(7)
                if self.instance.days_of_week_mask & (1 << idx)
            ]
            self.fields["days_of_week"].initial = selected
        self.fields["valve"].widget.attrs.update({"class": "form-select"})
        self.fields["enabled"].widget.attrs.update({"class": "form-check-input"})
        self.fields["days_of_week"].widget.attrs.update({"class": "form-check-input"})
        self.fields["start_time"].widget.attrs.update(
            {"class": "form-control", "placeholder": "e.g. 06:30"}
        )
        self.fields["start_time"].help_text = "Local time."
        self.fields["mode"].widget.attrs.update({"class": "form-select"})
        self.fields["max_duration_seconds"].widget.attrs.update(
            {"class": "form-control", "placeholder": "e.g. 1800 (=30 minutes)"}
        )
        self.fields["note"].widget.attrs.update(
            {"class": "form-control", "placeholder": "e.g. Front lawn morning"}
        )
        self.fields["days_of_week"].help_text = "Select at least one day."
        self.fields["mode"].help_text = (
            "Fixed runs for the configured duration."
        )
        self.fields["max_duration_seconds"].help_text = (
            "Relay-enforced run duration. "
            f"Maximum {RELAY_FLASH_MAX_DURATION_SECONDS} seconds."
        )

    def clean(self) -> dict:
        cleaned = super().clean()
        days = cleaned.get("days_of_week")
        if not days:
            self.add_error("days_of_week", "Select at least one day.")
        return cleaned

    def save(self, commit: bool = True):
        instance = super().save(commit=False)
        instance.days_of_week_mask = mask_from_days(
            self.cleaned_data.get("days_of_week", [])
        )
        if commit:
            instance.save()
        return instance


class RuleEditorForm(forms.Form):
    mode = forms.ChoiceField(choices=GroupedRule.MODE_CHOICES, initial="FIXED")
    enabled = forms.BooleanField(required=False, initial=True)
    days_of_week = forms.MultipleChoiceField(
        choices=DAY_CHOICES, widget=forms.CheckboxSelectMultiple,
        help_text="Select at least one day. Smart defaults to every day.",
    )
    start_time = forms.TimeField(
        widget=forms.TimeInput(attrs={"type": "time"}),
        help_text="Local start time for the entire sequence.",
    )
    note = forms.CharField(max_length=255, required=False, label="Name / note")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if name not in {"enabled", "days_of_week"}:
                field.widget.attrs["class"] = "form-select" if name == "mode" else "form-control"


class ValveMemberForm(forms.Form):
    valve = forms.ModelChoiceField(queryset=Valve.objects.none())
    duration_seconds = forms.IntegerField(
        min_value=1, max_value=RELAY_FLASH_MAX_DURATION_SECONDS,
        label="Duration (seconds)",
    )

    def __init__(self, *args, site=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["valve"].queryset = Valve.objects.filter(
            relay_device__site=site
        ).select_related("relay_device").order_by("name")
        self.fields["valve"].widget.attrs["class"] = "form-select"
        self.fields["duration_seconds"].widget.attrs["class"] = "form-control"


class BaseValveMemberFormSet(forms.BaseFormSet):
    def clean(self):
        if any(self.errors):
            return
        seen = set()
        count = 0
        for form in self.forms:
            values = form.cleaned_data
            if not values or values.get("DELETE"):
                continue
            valve = values.get("valve")
            if valve is None:
                continue
            if valve.pk in seen:
                raise forms.ValidationError("Select each valve only once.")
            seen.add(valve.pk)
            count += 1
        if not count:
            raise forms.ValidationError("Select at least one valve.")


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
        label="Min (mm)",
        initial=0.0,
        widget=forms.NumberInput(
            attrs={"class": "form-control", "placeholder": "e.g. 0", "step": "0.1"}
        ),
    )
    max_mm = forms.FloatField(
        label="Max (mm)",
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
        required=False, label="Fallback temperature (°C)",
        help_text="Set a finite site-specific value before enabling Smart.",
        widget=forms.NumberInput(attrs={"class": "form-control", "step": "0.1"}),
    )

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
