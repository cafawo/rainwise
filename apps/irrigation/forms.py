from __future__ import annotations

from django import forms

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
