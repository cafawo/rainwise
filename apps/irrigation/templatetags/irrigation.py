"""Presentation helpers for durations stored in seconds."""
import math

from django import template

register = template.Library()


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
