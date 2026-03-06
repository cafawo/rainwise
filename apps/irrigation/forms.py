from __future__ import annotations

from django import forms
from django.contrib.auth.forms import AuthenticationForm

from apps.irrigation.models import ScheduleRule


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
            "fixed_duration_seconds",
            "max_duration_seconds",
            "note",
        ]
        widgets = {
            "start_time": forms.TimeInput(attrs={"type": "time"}),
        }

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
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
        self.fields["fixed_duration_seconds"].widget.attrs.update(
            {"class": "form-control", "placeholder": "e.g. 1800 (=30 minutes)"}
        )
        self.fields["max_duration_seconds"].widget.attrs.update(
            {"class": "form-control", "placeholder": "e.g. 1800 (=30 minutes)"}
        )
        self.fields["note"].widget.attrs.update(
            {"class": "form-control", "placeholder": "e.g. Front lawn morning"}
        )
        self.fields["days_of_week"].help_text = "Select at least one day."
        self.fields["mode"].help_text = (
            "Fixed uses the exact duration. Dynamic picks a random duration up to max."
        )
        self.fields["fixed_duration_seconds"].help_text = "Required for Fixed mode."
        self.fields["max_duration_seconds"].help_text = "Hard stop for any run."

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
