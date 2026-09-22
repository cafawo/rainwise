"""Cached-weather Smart decisions and conservative, calendar-day water credit.

These helpers never perform network or hardware I/O. Their JSON-compatible
results are shared by previews and the controller's immutable decision records.
"""
from __future__ import annotations

import datetime as dt
import math
import os
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db.models import Q

from apps.irrigation.curves import daily_water_required, percentile
from apps.irrigation.models import IrrigationRun, get_curve_settings
from apps.irrigation.sequence import plan_sequence, target_seconds
from apps.weather.models import WeatherObservation


def _finite(value) -> bool:
    return value is not None and math.isfinite(value)


def _utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        raise ValueError("Decision timestamps must be timezone-aware.")
    return value.astimezone(dt.timezone.utc)


def _hour_boundary(value: dt.datetime, tz: ZoneInfo) -> bool:
    local = value.astimezone(tz)
    return local.minute == local.second == local.microsecond == 0


def validate_coverage_days(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 7:
        raise ValueError("Coverage days must be a whole number from 1 to 7.")


def accounting_windows(site, at: dt.datetime, coverage_days: int) -> dict:
    """Return UTC boundaries, with calendar subtraction performed locally."""
    validate_coverage_days(coverage_days)
    at = _utc(at)
    local = at.astimezone(ZoneInfo(site.timezone))
    rain_end_local = local.replace(minute=0, second=0, microsecond=0)
    rain_start_local = rain_end_local - dt.timedelta(days=coverage_days)
    irrigation_start_local = dt.datetime.combine(
        local.date() - dt.timedelta(days=coverage_days - 1),
        dt.time.min,
        tzinfo=local.tzinfo,
    )
    return {
        "rain_start": _utc(rain_start_local),
        "rain_end": _utc(rain_end_local),
        "irrigation_start": _utc(irrigation_start_local),
        "irrigation_end": at,
    }


def temperature_selection(
    site, at: dt.datetime, settings=None, *, include_evidence=False,
) -> dict:
    at = _utc(at)
    if settings is None:
        settings = get_curve_settings(site)
    rows = WeatherObservation.objects.filter(
        site=site, timestamp__gt=at - dt.timedelta(hours=24), timestamp__lte=at,
        retrieved_at__lte=at,
    ).order_by("timestamp")
    site_tz = ZoneInfo(site.timezone)
    valid = [row for row in rows if (
        row.retrieved_at >= row.timestamp and _finite(row.temperature_c)
        and _hour_boundary(row.timestamp, site_tz)
    )]
    latest_row = valid[-1] if valid else None
    if latest_row is None:
        older = WeatherObservation.objects.filter(
            site=site, timestamp__lte=at - dt.timedelta(hours=24),
            retrieved_at__lte=at, temperature_c__isnull=False,
        ).order_by("-timestamp")
        latest_row = next((row for row in older.iterator() if (
            row.retrieved_at >= row.timestamp and _finite(row.temperature_c)
            and _hour_boundary(row.timestamp, site_tz)
        )), None)
    latest = latest_row.timestamp if latest_row else None
    try:
        refresh_hours = max(1, int(os.environ.get("WEATHER_REFRESH_HOURS", "6")))
    except ValueError:
        refresh_hours = 6
    reason = ""
    if len(valid) < 18:
        reason = (
            f"Only {len(valid)} trusted finite hourly temperatures in the last "
            "24 hours; 18 required."
        )
    elif at - latest > dt.timedelta(hours=refresh_hours):
        reason = "Latest valid temperature is older than the weather refresh interval."
    fallback = settings.fallback_temperature_c
    selected = fallback if reason else percentile(
        [row.temperature_c for row in valid], 0.9
    )
    if not _finite(selected):
        selected = None
        reason = (reason + " Configure a finite fallback temperature.").strip()
    result = {
        "temperature_c": selected,
        "source": "fallback" if reason else "weather",
        "fallback": bool(reason),
        "reason": reason,
        "latest_valid_at": latest.isoformat() if latest else None,
        "valid_hours": len(valid),
    }
    if include_evidence:
        result.update({
            "start_at": (at - dt.timedelta(hours=24)).isoformat(),
            "cutoff_at": at.isoformat(),
            "parameters": {
                "lookback_hours": 24,
                "percentile": 0.9,
                "minimum_valid_hours": 18,
                "freshness_hours": refresh_hours,
            },
            "fallback_temperature_c": fallback if _finite(fallback) else None,
            "samples": [{
                "timestamp": row.timestamp.isoformat(),
                "retrieved_at": row.retrieved_at.isoformat(),
                "temperature_c": row.temperature_c,
            } for row in valid],
            "latest_valid_sample": {
                "timestamp": latest_row.timestamp.isoformat(),
                "retrieved_at": latest_row.retrieved_at.isoformat(),
                "temperature_c": latest_row.temperature_c,
            } if latest_row else None,
        })
    return result


def rain_credit(
    site, at: dt.datetime, coverage_days: int, *, include_evidence=False,
) -> dict:
    windows = accounting_windows(site, at, coverage_days)
    start, end = windows["rain_start"], windows["rain_end"]
    rows = WeatherObservation.objects.filter(
        site=site, timestamp__gt=start, timestamp__lte=end,
        retrieved_at__lte=at,
    )
    expected = set()
    boundary = start + dt.timedelta(hours=1)
    while boundary <= end:
        expected.add(boundary)
        boundary += dt.timedelta(hours=1)
    known = {}
    samples = []
    for row in rows:
        if (
            row.timestamp in expected
            and row.retrieved_at >= row.timestamp
            and _finite(row.precipitation_mm)
            and row.precipitation_mm >= 0
        ):
            known[row.timestamp] = row.precipitation_mm
            if include_evidence:
                samples.append({
                    "timestamp": row.timestamp.isoformat(),
                    "retrieved_at": row.retrieved_at.isoformat(),
                    "precipitation_mm": row.precipitation_mm,
                })
    credit = math.fsum(known.values())
    if not math.isfinite(credit):
        raise ValueError("Rain credit must be finite.")
    result = {
        "credit_mm": credit,
        "start_at": start.isoformat(),
        "cutoff_at": end.isoformat(),
        "known_hours": len(known),
        "expected_hours": len(expected),
        "missing_hours": len(expected - known.keys()),
        "warning": bool(expected - known.keys()),
    }
    if include_evidence:
        result["samples"] = sorted(samples, key=lambda sample: sample["timestamp"])
    return result


def delivery_estimate(run, cutoff: dt.datetime | None = None) -> dict:
    """Estimate the commanded interval, never the bookkeeping interval.

    An ambiguous retry can extend delivery through the command-call interval.
    A missing attempt end additionally leaves unknown extra delivery visible.
    Already-attempted ambiguous commands retain their full nominal allowance,
    even when that conservative interval extends beyond a decision cutoff.
    """
    rate = run.application_rate_mm_h
    start = run.actual_start_at or run.attempt_started_at
    attempted = start is not None
    calibrated = _finite(rate) and rate > 0
    duration = run.optimal_duration_seconds or run.max_duration_seconds
    uncertain = bool(run.delivery_uncertain or (
        run.attempt_started_at and not run.actual_start_at
    ))
    result = {
        "estimated_mm": None,
        "nominal_mm": duration * rate / 3600 if calibrated and attempted else None,
        "start_at": start.isoformat() if start else None,
        "end_at": None,
        "uncertain": uncertain,
        "unknown_extra_delivery": bool(uncertain and not run.attempt_finished_at),
        "nominal_allowance_beyond_cutoff": False,
        "calibrated": bool(calibrated),
    }
    if not attempted:
        return result
    start = _utc(start)
    if uncertain:
        start = min(start, _utc(run.attempt_started_at or start))
        end = _utc(run.attempt_finished_at or start) + dt.timedelta(seconds=duration)
    else:
        end = start + dt.timedelta(seconds=duration)
        for closed_at in (run.actual_stop_at, run.closure_confirmed_at):
            if closed_at is not None:
                end = min(end, _utc(closed_at))
    if cutoff is not None:
        cutoff = _utc(cutoff)
        if uncertain and start < cutoff:
            result["nominal_allowance_beyond_cutoff"] = end > cutoff
        else:
            end = min(end, cutoff)
    end = max(start, end)
    result["start_at"] = start.isoformat()
    result["end_at"] = end.isoformat()
    if calibrated:
        result["estimated_mm"] = (end - start).total_seconds() * rate / 3600
    return result


def irrigation_credit(
    valve, at: dt.datetime, coverage_days: int, *, include_evidence=False,
) -> dict:
    site = valve.relay_device.site
    windows = accounting_windows(site, at, coverage_days)
    start, end = windows["irrigation_start"], windows["irrigation_end"]
    # The indexed start bounds also include ambiguous attempts without an actual start.
    rows = IrrigationRun.objects.defer("appendix").filter(valve=valve).filter(
        Q(actual_start_at__lt=end) | Q(attempt_started_at__lt=end)
    ).filter(
        Q(actual_start_at__gte=start - dt.timedelta(days=1))
        | Q(attempt_started_at__gte=start - dt.timedelta(days=1))
    )
    credit = 0.0
    uncertain = False
    unknown_extra = False
    nominal_beyond_cutoff = False
    uncalibrated = 0
    known = 0
    contributions = []
    for run in rows:
        estimate = delivery_estimate(run, cutoff=end)
        if not estimate["start_at"]:
            continue
        delivery_start = max(start, dt.datetime.fromisoformat(estimate["start_at"]))
        delivery_end = dt.datetime.fromisoformat(estimate["end_at"])
        if not estimate["uncertain"]:
            delivery_end = min(end, delivery_end)
        if delivery_end <= delivery_start:
            continue
        uncertain |= estimate["uncertain"]
        unknown_extra |= estimate["unknown_extra_delivery"]
        nominal_beyond_cutoff |= estimate["nominal_allowance_beyond_cutoff"]
        run_credit = (
            (delivery_end - delivery_start).total_seconds()
            * run.application_rate_mm_h / 3600
        ) if estimate["calibrated"] else None
        if include_evidence:
            contributions.append({
                **{
                    key: None if isinstance(value, float) and not _finite(value)
                    else value
                    for key, value in estimate.items()
                },
                "run_id": run.pk,
                "application_rate_mm_h": (
                    run.application_rate_mm_h
                    if _finite(run.application_rate_mm_h) else None
                ),
                "credited_start_at": delivery_start.isoformat(),
                "credited_end_at": delivery_end.isoformat(),
                "credit_mm": run_credit,
            })
        if not estimate["calibrated"]:
            uncalibrated += 1
            continue
        known += 1
        credit += run_credit
    if not math.isfinite(credit):
        raise ValueError("Irrigation credit must be finite.")
    result = {
        "credit_mm": credit,
        "start_at": start.isoformat(),
        "cutoff_at": end.isoformat(),
        "known_runs": known,
        "uncalibrated_runs": uncalibrated,
        "incomplete_history": bool(uncalibrated),
        "uncertain": uncertain,
        "unknown_extra_delivery": unknown_extra,
        "nominal_allowance_beyond_cutoff": nominal_beyond_cutoff,
    }
    if include_evidence:
        result["contributions"] = contributions
    return result


def plan_dose(
    daily_need, coverage_days, rain_mm, irrigation_mm, rate, run_cap,
    *, available_seconds=86400, controller_interval_seconds=60,
    command_allowance_seconds=0,
) -> dict:
    validate_coverage_days(coverage_days)
    if not all(_finite(value) for value in (daily_need, rain_mm, irrigation_mm, rate)):
        raise ValueError("Water-balance inputs must be finite.")
    if min(daily_need, rain_mm, irrigation_mm) < 0 or rate <= 0:
        raise ValueError(
            "Credits/demand must be nonnegative and application rate positive."
        )
    if (
        isinstance(run_cap, bool) or not isinstance(run_cap, int)
        or not 1 <= run_cap <= 3276
    ):
        raise ValueError("Run cap must be a whole number from 1 to 3276 seconds.")
    target = max(0, coverage_days * daily_need - rain_mm - irrigation_mm)
    if not math.isfinite(target):
        raise ValueError("Water-balance result must be finite.")
    seconds = target_seconds(target, rate, available_seconds)
    sequence = plan_sequence(
        [{"valve_id": 0, "order": 0, "total_seconds": seconds,
          "run_cap_seconds": run_cap}],
        available_seconds=available_seconds,
        controller_interval_seconds=controller_interval_seconds,
        command_allowance_seconds=command_allowance_seconds,
    )
    delivered = seconds / 3600 * rate
    if not math.isfinite(delivered):
        raise ValueError("Water-balance result must be finite.")
    return {
        "target_mm": target,
        "planned_mm": target,
        "planned_seconds": seconds,
        "estimated_delivery_mm": delivered,
        "pulse_seconds": [pulse["duration_seconds"] for pulse in sequence["pulses"]],
        "pulse_count": sequence["pulse_count"],
        "unmet_mm": max(0, target - delivered),
    }


def build_smart_decision(
    site, members, decision_at: dt.datetime, *, available_seconds=None,
    controller_interval_seconds=None, command_allowance_seconds=None,
    include_evidence=False,
) -> dict:
    if available_seconds is None:
        local = decision_at.astimezone(ZoneInfo(site.timezone))
        midnight = dt.datetime.combine(
            local.date() + dt.timedelta(days=1), dt.time.min, local.tzinfo
        )
        available_seconds = (_utc(midnight) - _utc(decision_at)).total_seconds()
    if controller_interval_seconds is None or command_allowance_seconds is None:
        from apps.irrigation.group_services import command_allowance, controller_interval

        if controller_interval_seconds is None:
            controller_interval_seconds = controller_interval()
        if command_allowance_seconds is None:
            command_allowance_seconds = command_allowance()
    sequence_options = {
        "available_seconds": available_seconds,
        "controller_interval_seconds": controller_interval_seconds,
        "command_allowance_seconds": command_allowance_seconds,
    }
    settings = get_curve_settings(site)
    settings.full_clean()
    temperature = temperature_selection(
        site, decision_at, settings=settings, include_evidence=include_evidence,
    )
    if temperature["temperature_c"] is None:
        raise ValidationError(temperature["reason"])
    daily_need = daily_water_required(
        temperature["temperature_c"], settings.min_mm, settings.max_mm,
        settings.g, settings.m,
    )
    rain = rain_credit(
        site, decision_at, settings.coverage_days,
        include_evidence=include_evidence,
    )
    decisions = {}
    peak_members = []
    actual_members = []
    warnings = []
    if temperature["fallback"]:
        warnings.append(
            "Operating with fallback temperature "
            f"{temperature['temperature_c']:g} °C. {temperature['reason']}"
        )
    if rain["warning"]:
        warnings.append(
            "Rain history is incomplete; unknown rain supplies no credit "
            "and watering may overwater."
        )
    shared_warnings = warnings.copy()
    for member in members:
        valve = member.valve
        rate = valve.application_rate_mm_h
        if valve.relay_device.site_id != site.pk:
            raise ValidationError("Every valve must belong to the same site.")
        if not valve.has_valid_application_rate:
            reason = (
                f"{valve.name}: skipped in Smart because a finite positive "
                "watering rate is required. Enter a measured watering rate."
            )
            decisions[str(valve.pk)] = {
                "skipped": True,
                "skip_reason": reason,
                "valve_id": valve.pk,
                "valve_name": valve.name,
                "order": member.order,
                "application_rate_mm_h": None,
                "run_cap_seconds": member.duration_seconds,
                "planned_seconds": 0,
                "pulse_seconds": [],
                "pulse_count": 0,
                "peak_seconds": None,
                "peak_mm": None,
                "peak_pulse_count": None,
                "target_mm": None,
                "planned_mm": None,
                "estimated_delivery_mm": None,
                "unmet_mm": None,
                "irrigation": None,
            }
            if include_evidence:
                decisions[str(valve.pk)]["warnings"] = [reason]
            warnings.append(reason)
            continue
        peak_mm = settings.coverage_days * settings.max_mm
        peak_seconds = target_seconds(peak_mm, rate, available_seconds)
        credit = irrigation_credit(
            valve, decision_at, settings.coverage_days,
            include_evidence=include_evidence,
        )
        dose = plan_dose(
            daily_need, settings.coverage_days, rain["credit_mm"],
            credit["credit_mm"], rate, member.duration_seconds,
            **sequence_options,
        )
        dose.update({
            "skipped": False,
            "skip_reason": "",
            "valve_id": valve.pk,
            "valve_name": valve.name,
            "order": member.order,
            "application_rate_mm_h": rate,
            "run_cap_seconds": member.duration_seconds,
            "irrigation": credit,
            "peak_mm": peak_mm,
            "peak_seconds": peak_seconds,
            "peak_pulse_count": (
                (peak_seconds + member.duration_seconds - 1)
                // member.duration_seconds
            ),
        })
        decisions[str(valve.pk)] = dose
        sequence_member = {
            "valve_id": valve.pk, "order": member.order,
            "run_cap_seconds": member.duration_seconds,
        }
        actual_members.append({
            **sequence_member, "total_seconds": dose["planned_seconds"],
        })
        peak_members.append({**sequence_member, "total_seconds": peak_seconds})
        valve_warnings = []
        if credit["incomplete_history"]:
            valve_warnings.append(
                f"{valve.name}: incomplete calibrated irrigation history; "
                "only known delivery is credited."
            )
        if credit["uncertain"]:
            valve_warnings.append(
                f"{valve.name}: uncertain delivery is credited conservatively; "
                "physical volume is unknown."
            )
        if credit["unknown_extra_delivery"]:
            valve_warnings.append(
                f"{valve.name}: an interrupted attempt has explicitly "
                "unknown extra delivery."
            )
        if credit["nominal_allowance_beyond_cutoff"]:
            valve_warnings.append(
                f"{valve.name}: the full conservative allowance for an "
                "uncertain earlier command extends beyond the decision cutoff; "
                "this is nominal credit, not confirmed physical delivery."
            )
        warnings.extend(valve_warnings)
        if include_evidence:
            dose["warnings"] = valve_warnings
    peak_sequence = plan_sequence(peak_members, **sequence_options)
    sequence = plan_sequence(actual_members, **sequence_options)
    result = {
        "decision_at": _utc(decision_at).isoformat(),
        "coverage_days": settings.coverage_days,
        "daily_need_mm": daily_need,
        "temperature": temperature,
        "rain": rain,
        "valves": decisions,
        "sequence": sequence,
        "peak_sequence": peak_sequence,
        "warnings": warnings,
    }
    if include_evidence:
        result.update({
            "calculation_version": 1,
            "settings": {
                field: getattr(settings, field) for field in (
                    "min_mm", "max_mm", "g", "m", "coverage_days",
                    "fallback_temperature_c",
                )
            },
            "sequence_options": sequence_options,
            "shared_warnings": shared_warnings,
        })
    return result
