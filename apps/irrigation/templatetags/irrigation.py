"""Presentation helpers for irrigation forms and durations."""
import math

from django import template

register = template.Library()


@register.filter
def with_field_errors(field):
    """Render field errors with Bootstrap styling and accessible descriptions."""
    if not field.errors:
        return field.as_widget()
    widget_attrs = field.field.widget.attrs
    return field.as_widget(attrs={
        "class": f"{widget_attrs.get('class', '')} is-invalid".strip(),
        "aria-invalid": "true",
        "aria-describedby": " ".join(filter(None, (
            widget_attrs.get("aria-describedby"), f"{field.auto_id}_errors",
        ))),
    })


@register.filter
def duration(value):
    if value is None:
        return "N/A"
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return "N/A"
    if not math.isfinite(seconds) or seconds < 0:
        return "N/A"
    hours, remaining = divmod(seconds, 3600)
    minutes, remainder = divmod(remaining, 60)
    parts = []
    if hours:
        parts.append(f"{int(hours)}h")
    if minutes:
        parts.append(f"{int(minutes)}m")
    if remainder or not parts:
        parts.append(f"{remainder:g}s")
    return " ".join(parts)
