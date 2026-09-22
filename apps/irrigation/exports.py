"""Read-only, streaming exports of irrigation history."""
from __future__ import annotations

import datetime as dt
import json
import math

from apps.irrigation.balance import delivery_estimate
from apps.irrigation.models import IrrigationRun


RUN_FIELDS = (
    "id", "valve_id", "trigger", "status", "stop_reason", "error_message",
    "requested_start_at", "planned_start_at", "attempt_started_at",
    "attempt_finished_at", "actual_start_at", "actual_stop_at",
    "closure_confirmed_at", "optimal_duration_seconds", "max_duration_seconds",
    "application_rate_mm_h", "delivery_uncertain",
)


def _json_value(value):
    """Keep timestamps in UTC and unavailable legacy numbers explicit."""
    if isinstance(value, dt.datetime):
        return value.astimezone(dt.timezone.utc).isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _encode(value):
    return json.dumps(_json_value(value), ensure_ascii=False, allow_nan=False)


def iter_logs(site, exported_at, *, extended=False):
    """Yield one JSON document without retaining the complete run history."""
    metadata = {
        "schema_version": 1,
        "exported_at": exported_at,
        "site": (
            {"id": site.pk, "name": site.name, "timezone": site.timezone}
            if site else None
        ),
    }
    yield _encode(metadata)[:-1] + ', "runs": ['
    runs = IrrigationRun.objects.filter(valve__relay_device__site=site)
    fields = RUN_FIELDS + (
        "valve__id", "valve__name", "valve__channel", "valve__relay_device_id",
    )
    if extended:
        fields += ("appendix",)
    runs = runs.select_related("valve").only(*fields).order_by("-id")
    separator = ""
    # Finish each database read before yielding to a possibly slow download.
    # A cursor spanning yields can keep SQLite controller writes locked out.
    batch = list(runs[:200])
    while batch:
        for run in batch:
            record = {name: getattr(run, name) for name in RUN_FIELDS}
            record["valve"] = {
                "id": run.valve_id,
                "name": run.valve.name,
                "relay_device_id": run.valve.relay_device_id,
                "channel": run.valve.channel,
            }
            record["delivery"] = delivery_estimate(run, cutoff=exported_at)
            if extended:
                record["appendix"] = run.appendix
            yield separator + _encode(record)
            separator = ", "
        if len(batch) < 200:
            break
        batch = list(runs.filter(pk__lt=batch[-1].pk)[:200])
    yield "]}"
