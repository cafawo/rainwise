from __future__ import annotations

from django import forms
from django.contrib.auth.forms import AuthenticationForm

from apps.irrigation.models import Schedule, ScheduleRule, Valve


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
            "Fixed uses the max duration. Dynamic picks a random duration up to max."
        )
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


def _rule_label(rule: ScheduleRule) -> str:
    days = rule.days_display()
    mode = rule.get_mode_display()
    suffix = " · disabled" if not rule.enabled else ""
    return (
        f"{rule.valve.name} · {days} · {rule.start_time} · {mode} · "
        f"max {rule.max_duration_seconds}s{suffix}"
    )


class ScheduleSaveForm(forms.Form):
    name = forms.CharField(
        required=False,
        max_length=100,
        widget=forms.TextInput(
            attrs={"class": "form-control", "placeholder": "e.g. Summer schedule"}
        ),
    )
    overwrite_schedule = forms.ModelChoiceField(
        queryset=Schedule.objects.none(),
        required=False,
        empty_label="Create new schedule",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    rule_ids = forms.MultipleChoiceField(
        required=False,
        widget=forms.CheckboxSelectMultiple(attrs={"class": "form-check-input"}),
    )

    def __init__(self, *args, **kwargs) -> None:
        schedules = kwargs.pop("schedules", Schedule.objects.none())
        rules = kwargs.pop("rules", [])
        super().__init__(*args, **kwargs)
        self._schedules = schedules
        self.fields["overwrite_schedule"].queryset = schedules
        self.fields["rule_ids"].choices = [
            (str(rule.id), _rule_label(rule)) for rule in rules
        ]
        self.fields["rule_ids"].initial = [
            str(rule.id) for rule in rules if rule.enabled
        ]

    def clean(self) -> dict:
        cleaned = super().clean()
        name = (cleaned.get("name") or "").strip()
        overwrite = cleaned.get("overwrite_schedule")
        rule_ids = cleaned.get("rule_ids") or []

        if not overwrite and not name:
            self.add_error("name", "Enter a name or choose a schedule to overwrite.")
        if not rule_ids:
            self.add_error("rule_ids", "Select at least one rule to save.")

        if not overwrite and name:
            if self._schedules.filter(name__iexact=name).exists():
                self.add_error("name", "A schedule with this name already exists.")

        if overwrite and name:
            conflict = (
                self._schedules.filter(name__iexact=name)
                .exclude(id=overwrite.id)
                .exists()
            )
            if conflict:
                self.add_error("name", "A schedule with this name already exists.")

        cleaned["name"] = name
        return cleaned


class ScheduleLoadForm(forms.Form):
    schedule = forms.ModelChoiceField(
        queryset=Schedule.objects.none(),
        widget=forms.Select(attrs={"class": "form-select"}),
    )

    def __init__(self, *args, **kwargs) -> None:
        schedules = kwargs.pop("schedules", Schedule.objects.none())
        super().__init__(*args, **kwargs)
        self.fields["schedule"].queryset = schedules
