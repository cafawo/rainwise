"""Durable group planning and the shared, short per-site admission boundary.

Only the controller advances groups. Hardware calls never run inside a database
transaction; the committed attempt record is the recovery boundary.
"""
from __future__ import annotations

from contextlib import contextmanager
import datetime as dt
import logging
import math
import os
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Case, F, Q, TextField, Value, When
from django.utils import timezone

from apps.irrigation import services
from apps.irrigation.models import (
    GroupedRule, GroupedRuleValve, IrrigationRun,
    RuleOccurrence, ScheduleRule, Site, Valve, get_curve_settings,
    normalize_rule_mode,
)

logger = logging.getLogger(__name__)
RESERVED_STATUSES = ("PENDING", "ACTIVE", "STOPPING")
MISSING_RATE_SKIP = "Skipped in Smart: enter a valid watering rate for this valve."
SENDER_INTERRUPTED = (
    "Closure attempted while opening awaited acknowledgement; physical delivery is uncertain."
)
SENDER_CANCELLED = (
    "Opening cancelled before acknowledgement; physical delivery is uncertain."
)
UTC = dt.timezone.utc


def controller_interval() -> int:
    try:
        return max(1, int(os.environ.get("CONTROLLER_INTERVAL_SECONDS", "60")))
    except ValueError:
        return 60


def command_allowance() -> float:
    # Connection plus header/body response timeouts, for every configured try.
    return 3 * services.MODBUS_TIMEOUT_SECONDS * (services.MODBUS_RETRIES + 1)


@contextmanager
def site_admission(site):
    """An UPDATE obtains the write lock on both SQLite and PostgreSQL.

    It must be the first query in the transaction, avoiding SQLite's deferred
    read-to-write upgrade. This is admission serialization, not a hardware lock.
    """
    with transaction.atomic():
        Site.objects.filter(pk=site.pk).update(
            admission_version=F("admission_version") + 1
        )
        yield


def members_for(rule):
    return list(GroupedRuleValve.objects.filter(rule=rule).select_related(
        "valve__relay_device"
    ).order_by("order"))


def reservation_details(rule, members=None, *, available_seconds=None):
    """Calculate the peak envelope with the same sequence used by admission.

    Rates missing from Smart cannot size a peak. They remain explicit omissions,
    and an entirely unavailable rule is a start marker rather than a fake dose.
    The pure planner checks integer bounds before allocating any pulse list.
    """
    try:
        return _calculate_reservation(rule, members, available_seconds)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc


def _calculate_reservation(rule, members, available_seconds):
    from apps.irrigation.sequence import plan_sequence, target_seconds

    members = members_for(rule) if members is None else list(members)
    if rule.mode not in ("FIXED", "SMART"):
        raise ValidationError("Unsupported group mode.")
    if available_seconds is None:
        available_seconds = 86400 - _seconds(rule.start_time)
    smart = rule.mode == "SMART"
    curve = get_curve_settings(rule.schedule.site) if smart else None
    if curve is not None:
        curve.full_clean()
    plans = []
    unavailable = []
    for member in members:
        if smart and not member.valve.has_valid_application_rate:
            unavailable.append(member.valve)
            continue
        seconds = (
            target_seconds(
                curve.coverage_days * curve.max_mm,
                member.valve.application_rate_mm_h,
                available_seconds=available_seconds,
            ) if smart else member.duration_seconds
        )
        plans.append({
            "valve_id": member.valve_id, "order": member.order,
            "total_seconds": seconds, "run_cap_seconds": member.duration_seconds,
        })
    result = plan_sequence(
        plans, smart=smart, available_seconds=available_seconds,
        controller_interval_seconds=controller_interval(),
        command_allowance_seconds=command_allowance(),
    )
    return {
        **result,
        "total_seconds": result["reserved_seconds"],
        "available": not smart or len(unavailable) < len(members),
        "unavailable_valve_ids": [valve.pk for valve in unavailable],
        "unavailable_valves": [valve.name for valve in unavailable],
    }


def reservation_seconds(rule, members=None):
    """Compatibility pair: sequence elapsed time and scheduling allowance."""
    details = reservation_details(rule, members)
    return details["elapsed_seconds"], details["scheduling_allowance_seconds"]


def _seconds(value):
    return value.hour * 3600 + value.minute * 60 + value.second


def _windows(rule, duration):
    for weekday in range(7):
        if rule.uses_weekday(weekday):
            start = weekday * 86400 + _seconds(rule.start_time)
            yield start, start + duration


def _overlaps(left, left_duration, right, right_duration):
    if left_duration <= 0 or right_duration <= 0:
        return False
    week = 7 * 86400
    return any(a < d + offset and c + offset < b
               for a, b in _windows(left, left_duration)
               for c, d in _windows(right, right_duration)
               for offset in (-week, 0, week))


def validate_configuration(rule, members=None, exclude_rule=None):
    """Validate ownership, bounds and automatic reservations without I/O.

    Existing Smart members may lose their rate and remain configured. Execution
    skips those members; the editor separately checks newly selected members.
    """
    grouped = isinstance(rule, GroupedRule)
    if grouped:
        rule.full_clean()
        members = members_for(rule) if members is None else list(members)
        if not members:
            raise ValidationError("Select at least one valve.")
        valve_ids = [m.valve_id for m in members]
        orders = [m.order for m in members]
        if len(set(valve_ids)) != len(valve_ids) or len(set(orders)) != len(orders):
            raise ValidationError("Valves and their order must be unique.")
        if rule.mode not in ("FIXED", "SMART"):
            raise ValidationError("Unsupported rule mode.")
        if rule.mode == "SMART" and rule.enabled:
            curve = get_curve_settings(rule.schedule.site)
            curve.full_clean()
        for member in members:
            valve = member.valve
            if valve.relay_device.site_id != rule.schedule.site_id:
                raise ValidationError("All valves must belong to the schedule's site.")
            minimum = 1 if rule.mode == "SMART" else 60
            duration = member.duration_seconds
            if (isinstance(duration, bool) or not isinstance(duration, int)
                    or not minimum <= duration <= 3276):
                raise ValidationError(f"Duration must be {minimum}–3276 whole seconds.")
        duration = reservation_details(rule, members)["total_seconds"]
        if rule.enabled and rule.mode == "SMART":
            other = GroupedRuleValve.objects.filter(
                rule__schedule=rule.schedule, rule__enabled=True,
                rule__mode="SMART", valve_id__in=valve_ids,
            )
            if rule.pk:
                other = other.exclude(rule=rule)
            if other.exists():
                raise ValidationError(
                    "A valve may belong to only one enabled Smart rule per schedule."
                )
    else:
        if normalize_rule_mode(rule.mode) != "FIXED":
            raise ValidationError("Unsupported single-valve rule mode.")
        duration = rule.max_duration_seconds
    if not rule.enabled:
        return
    for other in GroupedRule.objects.filter(schedule=rule.schedule, enabled=True):
        if grouped and other.pk == rule.pk:
            continue
        if isinstance(exclude_rule, GroupedRule) and other.pk == exclude_rule.pk:
            continue
        other_duration = sum(reservation_seconds(other))
        if _overlaps(rule, duration, other, other_duration):
            raise ValidationError(
                f"This reservation overlaps {other} at "
                f"{other.start_time:%H:%M}. Change the start time or watering settings."
            )
    if grouped:
        for other in ScheduleRule.objects.filter(schedule=rule.schedule, enabled=True):
            if isinstance(exclude_rule, ScheduleRule) and other.pk == exclude_rule.pk:
                continue
            if _overlaps(rule, duration, other, other.max_duration_seconds):
                raise ValidationError(
                    f"This reservation overlaps {other} at "
                    f"{other.start_time:%H:%M}. Change the start time or watering settings."
                )


def validate_schedule(schedule):
    for rule in GroupedRule.objects.filter(schedule=schedule, enabled=True):
        validate_configuration(rule)


def _reservations(site, exclude=None):
    query = RuleOccurrence.objects.filter(site=site, status__in=RESERVED_STATUSES)
    return query.exclude(pk=exclude.pk) if exclude else query


def _unresolved_runs(site):
    return IrrigationRun.objects.filter(valve__relay_device__site=site).filter(
        Q(status="RUNNING") |
        Q(dispatch_state="SENDING") |
        Q(attempt_started_at__isnull=False, closure_confirmed_at__isnull=True)
    )


def _group_conflict(site, occurrence=None):
    runs = _unresolved_runs(site)
    if occurrence:
        runs = runs.exclude(occurrence=occurrence)
    return (_reservations(site, occurrence).exists() or runs.exists()
            or Valve.objects.filter(relay_device__site=site,
                                    last_known_is_open=True).exclude(
                pk__in=IrrigationRun.objects.filter(
                    occurrence=occurrence, status="RUNNING"
                ).values("valve_id")
                if occurrence else []
            ).exists())


def assert_configuration_editable(rule):
    site = rule.schedule.site
    if isinstance(rule, GroupedRule):
        if RuleOccurrence.objects.filter(rule=rule, status__in=RESERVED_STATUSES).exists():
            raise ValidationError(
                "Stop the rule and wait for confirmed closure before editing it."
            )
    elif (_reservations(site).exists()
          or _unresolved_runs(site).filter(valve=rule.valve).exists()):
        raise ValidationError(
            "Stop watering and wait for confirmed closure before converting this rule."
        )


def cancel_occurrence(occurrence, reason="Stopped by user"):
    with site_admission(occurrence.site):
        changed = RuleOccurrence.objects.filter(
            pk=occurrence.pk, status__in=RESERVED_STATUSES
        ).update(cancellation_requested=True, status="STOPPING", outcome=reason)
        if changed:
            IrrigationRun.objects.filter(occurrence=occurrence).update(
                cancellation_requested=True
            )
            IrrigationRun.objects.filter(
                occurrence=occurrence, status="PLANNED", attempt_started_at=None,
            ).update(status="FAILED", stop_reason="MANUAL_STOP", error_message=reason)


def cancel_rule(rule, reason="Rule disabled or deleted"):
    for occurrence in RuleOccurrence.objects.filter(rule=rule, status__in=RESERVED_STATUSES):
        cancel_occurrence(occurrence, reason)


def cancel_site_groups(site, reason="Active schedule changed"):
    for occurrence in _reservations(site):
        cancel_occurrence(occurrence, reason)


def mark_sender_interrupted(run_ids):
    """Record a close racing transmission before its eventual acknowledgement."""
    IrrigationRun.objects.filter(
        pk__in=run_ids, dispatch_state="SENDING", sender_interrupted=False,
    ).update(
        sender_interrupted=True, delivery_uncertain=True,
        error_message=SENDER_INTERRUPTED,
    )


def _confirmed_closed(run, *, close=False):
    """Read afresh, preserving the first safe closure used for Smart rests."""
    run.refresh_from_db()
    sender_outstanding = run.dispatch_state == "SENDING"
    if close:
        try:
            try:
                mark_sender_interrupted([run.pk])
            finally:
                services.close_valve(run.valve)
        except Exception as exc:
            # A timed pulse can already have expired even if this redundant
            # command fails. Only a fresh closed-state read can resolve it.
            logger.warning("Recovery close failed for run %s: %s", run.pk, exc)
    try:
        if services.read_valve_state(run.valve):
            return False
    except Exception as exc:
        logger.warning("Closure not confirmed for run %s: %s", run.pk, exc)
        return False
    # A surviving web process may not have transmitted yet. Its acknowledgement
    # is the barrier: a closed read before that barrier cannot release ownership.
    if sender_outstanding:
        return False
    now = timezone.now()
    updates = {}
    if run.closure_confirmed_at is None:
        updates["closure_confirmed_at"] = now
    if run.status in ("RUNNING", "PLANNED"):
        updates.update(status="FINISHED", actual_stop_at=now,
                       stop_reason="MANUAL_STOP" if close else "COMPLETED")
    if updates and not IrrigationRun.objects.filter(pk=run.pk).exclude(
        dispatch_state="SENDING"
    ).update(**updates):
        return False
    Valve.objects.filter(pk=run.valve_id, last_known_is_open=True).update(
        last_known_is_open=False, last_polled_at=now
    )
    return True


def _send_claimed(run):
    """Transmit one already committed logical attempt; never replay it."""
    admission_site = run.occurrence.site if run.occurrence_id else run.valve.relay_device.site
    with site_admission(admission_site):
        run.refresh_from_db()
        occurrence = run.occurrence
        if run.dispatch_state != "UNSENT":
            return run
        if (occurrence and occurrence.mode == "SMART"
                and not run.valve.has_valid_application_rate):
            IrrigationRun.objects.filter(
                pk=run.pk, status="PLANNED", actual_start_at=None,
            ).update(
                attempt_started_at=None, attempt_finished_at=None,
                delivery_uncertain=False, dispatch_state="DONE",
            )
            _skip_uncalibrated_pulses(occurrence, valve_id=run.valve_id)
            run.refresh_from_db()
            return run
        now = timezone.now()
        reason = None
        if (run.cancellation_requested
                or (occurrence and occurrence.cancellation_requested)):
            reason = "Cancelled before command"
        elif (run.attempt_started_at is None or now > run.attempt_started_at
              + dt.timedelta(seconds=command_allowance() + controller_interval())):
            reason = "Opening dispatch window expired before command"
        elif not run.valve.relay_device.enabled:
            reason = "Relay disabled before command"
        elif occurrence:
            rule = GroupedRule.objects.filter(pk=occurrence.rule_id).first()
            member = GroupedRuleValve.objects.filter(
                rule_id=occurrence.rule_id, valve=run.valve,
            ).first()
            site = Site.objects.get(pk=occurrence.site_id)
            if (rule is None or not rule.enabled
                    or rule.mode != occurrence.mode
                    or rule.schedule.site_id != occurrence.site_id
                    or run.valve.relay_device.site_id != occurrence.site_id
                    or occurrence.config.get("schedule_id") != rule.schedule_id
                    or occurrence.config.get("timezone", site.timezone) != site.timezone
                    or site.active_schedule_id != rule.schedule_id
                    or member is None
                    or run.optimal_duration_seconds > member.duration_seconds):
                reason = "Configuration changed before command"
            elif now + dt.timedelta(
                seconds=run.optimal_duration_seconds + command_allowance()
            ) > occurrence.reservation_end:
                reason = "Reservation deadline exhausted before command"
        if reason:
            IrrigationRun.objects.filter(pk=run.pk).update(
                status="FAILED", attempt_started_at=None,
                attempt_finished_at=now, delivery_uncertain=False,
                closure_confirmed_at=now, dispatch_state="DONE",
                error_message=reason, stop_reason="MANUAL_STOP",
            )
            if occurrence:
                cancel_occurrence(occurrence, reason + "; remaining target unmet")
            run.refresh_from_db()
            return run
        claimed = IrrigationRun.objects.filter(
            pk=run.pk, status="PLANNED", dispatch_state="UNSENT",
            cancellation_requested=False,
        ).update(dispatch_state="SENDING", closure_confirmed_at=None)
        if not claimed:
            run.refresh_from_db()
            return run
    # Use the actual sending time, after admission database work. Recovery keeps
    # the SENDING claim until this call acknowledges, even if the process pauses.
    now = timezone.now()
    if occurrence and now + dt.timedelta(
        seconds=run.optimal_duration_seconds + command_allowance()
    ) > occurrence.reservation_end:
        IrrigationRun.objects.filter(pk=run.pk).update(
            dispatch_state="DONE", status="FAILED", attempt_started_at=None,
            attempt_finished_at=now, delivery_uncertain=False,
            closure_confirmed_at=now,
            error_message="Reservation deadline exhausted before transmission",
        )
        cancel_occurrence(occurrence, "Reservation deadline exhausted; target unmet")
        run.refresh_from_db()
        return run
    try:
        services.open_valve_for(run.valve, run.optimal_duration_seconds)
    except Exception as exc:
        try:
            IrrigationRun.objects.filter(pk=run.pk).update(
                status="FAILED", attempt_finished_at=timezone.now(),
                delivery_uncertain=True, stop_reason="ERROR", error_message=str(exc),
                dispatch_state="DONE", closure_confirmed_at=None,
            )
            if occurrence:
                cancel_occurrence(
                    occurrence, "Ambiguous opening; remaining pulses cancelled"
                )
            _confirmed_closed(run, close=True)
        except Exception:
            # The sender cannot durably acknowledge, so ownership stays held.
            # Still attempt closure even when recording the failed call fails.
            try:
                services.close_valve(run.valve)
            except Exception:
                logger.exception("Emergency close after failed-call persistence failed")
            raise
        raise
    finished = timezone.now()
    try:
        IrrigationRun.objects.filter(pk=run.pk).update(
            status="RUNNING", actual_start_at=now, attempt_finished_at=finished,
            # Cancellation is persisted before every close of an outstanding
            # sender. Preserve it even if recording the close itself failed.
            delivery_uncertain=Case(
                When(Q(sender_interrupted=True) | Q(cancellation_requested=True),
                     then=True),
                default=False,
            ),
            error_message=Case(
                When(sender_interrupted=True, then=Value(SENDER_INTERRUPTED)),
                When(cancellation_requested=True, then=Value(SENDER_CANCELLED)),
                default=F("error_message"), output_field=TextField(),
            ),
            dispatch_state="DONE",
            closure_confirmed_at=None,
        )
        run.refresh_from_db()
        if occurrence:
            occurrence.refresh_from_db()
        if run.cancellation_requested or (occurrence and occurrence.cancellation_requested):
            _confirmed_closed(run, close=True)
    except Exception:
        # The durable pre-command claim remains uncertain if saving the result
        # failed. Best effort closure does not erase that recovery record.
        try:
            services.close_valve(run.valve)
        except Exception:
            logger.exception("Emergency close after result persistence failed")
        raise
    return run


def start_single(valve, duration, trigger, planned_start_at=None, rule=None):
    services._duration_to_flash_ticks(duration)
    site = valve.relay_device.site
    with site_admission(site):
        site.refresh_from_db()
        valve.refresh_from_db()
        valve.relay_device.refresh_from_db()
        if valve.relay_device.site_id != site.pk:
            raise ValidationError("Valve ownership changed before admission.")
        if not valve.relay_device.enabled:
            raise ValidationError("Relay device is disabled.")
        now = timezone.now()
        if trigger == "SCHEDULED" and rule is not None:
            current_rule = ScheduleRule.objects.filter(pk=rule.pk).select_related(
                "schedule", "valve__relay_device",
            ).first()
            local = now.astimezone(ZoneInfo(site.timezone))
            due_minute = local.replace(second=0, microsecond=0).astimezone(UTC)
            if (current_rule is None or not current_rule.enabled
                    or normalize_rule_mode(current_rule.mode) != "FIXED"
                    or current_rule.schedule.site_id != site.pk
                    or current_rule.schedule_id != site.active_schedule_id
                    or current_rule.valve_id != valve.pk
                    or current_rule.max_duration_seconds != duration
                    or not current_rule.uses_weekday(local.weekday())
                    or (current_rule.start_time.hour, current_rule.start_time.minute)
                    != (local.hour, local.minute)
                    or planned_start_at is None
                    or planned_start_at.astimezone(UTC) != due_minute):
                raise ValidationError(
                    "Scheduled rule changed or is no longer due; start skipped."
                )
            rule = current_rule
        if rule and normalize_rule_mode(rule.mode) != "FIXED":
            raise ValidationError("Unsupported single-valve rule mode.")
        if planned_start_at is not None:
            existing = IrrigationRun.objects.filter(
                valve=valve, planned_start_at=planned_start_at,
                trigger="SCHEDULED", occurrence=None,
            ).first()
            if existing:
                return existing
        conflict = None
        if _reservations(site).exists():
            conflict = "Stop the active group and wait for closure before starting another valve."
        elif _unresolved_runs(site).filter(valve=valve).exists():
            conflict = "Valve is already running or awaiting confirmed closure."
        elif _unresolved_runs(site).exclude(status="RUNNING").exists():
            # Unrelated legacy running pulses retain their original coexistence.
            conflict = "An opening or uncertain closure is awaiting recovery."
        if conflict:
            if planned_start_at is None:
                raise ValidationError(conflict)
            return IrrigationRun.objects.create(
                valve=valve, trigger=trigger, requested_start_at=timezone.now(),
                planned_start_at=planned_start_at, status="FAILED",
                optimal_duration_seconds=duration, max_duration_seconds=duration,
                stop_reason="ERROR", error_message="Skipped: " + conflict,
            )
        rate = valve.application_rate_mm_h
        if rate is not None and (not math.isfinite(rate) or rate <= 0):
            rate = None
        run = IrrigationRun.objects.create(
            valve=valve, trigger=trigger, requested_start_at=now,
            planned_start_at=planned_start_at, status="PLANNED",
            optimal_duration_seconds=duration, max_duration_seconds=duration,
            application_rate_mm_h=rate, attempt_started_at=now,
            delivery_uncertain=True, dispatch_state="UNSENT",
        )
    return _send_claimed(run)


def close_member(valve):
    site = valve.relay_device.site
    with site_admission(site):
        for occurrence in _reservations(site):
            if any(m["valve_id"] == valve.pk for m in occurrence.config.get("members", [])):
                cancel_occurrence(occurrence)
        runs = list(_unresolved_runs(site).filter(valve=valve))
        IrrigationRun.objects.filter(pk__in=[r.pk for r in runs]).update(
            cancellation_requested=True
        )
    try:
        mark_sender_interrupted([run.pk for run in runs])
    finally:
        services.close_valve(valve)
    now = timezone.now()
    # An in-flight opener must acknowledge cancellation after its call returns.
    for run in runs:
        if run.attempt_started_at and not run.attempt_finished_at:
            continue
        IrrigationRun.objects.filter(pk=run.pk).update(
            status="FINISHED", actual_stop_at=now, stop_reason="MANUAL_STOP",
            attempt_started_at=run.attempt_started_at or run.actual_start_at,
        )


def _snapshot(rule, members, *, include_reservation=True, available_seconds=None):
    config = {
        "rule_id": rule.pk, "schedule_id": rule.schedule_id, "note": rule.note,
        "enabled": rule.enabled,
        "days_of_week_mask": rule.days_of_week_mask,
        "start_time": rule.start_time.isoformat(),
        "timezone": rule.schedule.site.timezone,
        "mode": rule.mode,
        "sequence_policy": (
            "equal_previous_run_v1" if rule.mode == "SMART" else "one_pass_v1"
        ),
        "controller_interval_seconds": controller_interval(),
        "command_allowance_seconds": command_allowance(),
        "members": [{"valve_id": m.valve_id, "name": m.valve.name,
                     "order": m.order, "duration_seconds": m.duration_seconds,
                     "application_rate_mm_h": (
                         m.valve.application_rate_mm_h
                         if m.valve.has_valid_application_rate else None
                     )}
                    for m in members],
    }
    if rule.mode == "SMART":
        curve = get_curve_settings(rule.schedule.site)
        config["curve"] = {
            name: (getattr(curve, name) if math.isfinite(getattr(curve, name))
                   else None) for name in (
                "min_mm", "max_mm", "g", "m", "coverage_days",
                "fallback_temperature_c",
            )
        }
    if include_reservation:
        envelope = reservation_details(
            rule, members, available_seconds=available_seconds,
        )
        config.update({name: envelope[name] for name in (
            "watering_seconds", "break_seconds", "scheduling_allowance_seconds",
            "reserved_seconds", "pulse_count", "repeat_count",
            "unavailable_valve_ids", "unavailable_valves",
        )})
        config["peak_pulse_count"] = envelope["pulse_count"]
    return config


def _deadline(site, start, seconds):
    end = start + dt.timedelta(seconds=seconds)
    tz = ZoneInfo(site.timezone)
    next_midnight = dt.datetime.combine(
        start.astimezone(tz).date() + dt.timedelta(days=1), dt.time(), tz
    ).astimezone(UTC)
    if end > next_midnight:
        raise ValidationError("The group reservation cannot fit before local midnight.")
    return end


def _remaining_day_seconds(site, start):
    tz = ZoneInfo(site.timezone)
    midnight = dt.datetime.combine(
        start.astimezone(tz).date() + dt.timedelta(days=1), dt.time(), tz,
    ).astimezone(UTC)
    return (midnight - start.astimezone(UTC)).total_seconds()


def request_fixed_group(rule):
    site = rule.schedule.site
    with site_admission(site):
        rule.refresh_from_db()
        site.refresh_from_db()
        existing = RuleOccurrence.objects.filter(
            rule=rule, source="MANUAL", status__in=RESERVED_STATUSES
        ).first()
        if existing:
            return existing
        if rule.mode != "FIXED" or not rule.enabled or site.active_schedule_id != rule.schedule_id:
            raise ValidationError("Run now requires an enabled Fixed rule in the active schedule.")
        validate_configuration(rule)
        if _group_conflict(site):
            raise ValidationError(
                "Watering is active or awaiting closure; stop it before Run now."
            )
        members = members_for(rule)
        if any(not m.valve.relay_device.enabled for m in members):
            raise ValidationError("A relay device is disabled.")
        now = timezone.now()
        config = _snapshot(
            rule, members, available_seconds=_remaining_day_seconds(site, now),
        )
        end = _deadline(site, now, config["reserved_seconds"])
        return RuleOccurrence.objects.create(
            site=site, rule=rule, mode="FIXED", config=config,
            source="MANUAL", requested_at=now, reservation_end=end, status="PENDING",
        )


def _plan_occurrence(rule, scheduled_at=None, pending=None, now=None):
    from apps.irrigation.balance import build_smart_decision
    from apps.irrigation.sequence import plan_sequence

    site = rule.schedule.site
    with site_admission(site):
        now = timezone.now() if now is None else now
        site.refresh_from_db()
        rule.refresh_from_db()
        local_date = (
            scheduled_at.astimezone(ZoneInfo(site.timezone)).date()
            if scheduled_at else None
        )
        if scheduled_at and RuleOccurrence.objects.filter(
            rule=rule, scheduled_local_date=local_date
        ).exists():
            return None
        members = members_for(rule)
        config = pending.config if pending else _snapshot(
            rule, members, include_reservation=False,
        )
        occurrence = pending or RuleOccurrence.objects.create(
            site=site, rule=rule, mode=rule.mode, config=config,
            scheduled_local_date=local_date, scheduled_at=scheduled_at,
            source="SCHEDULED", requested_at=now, status="PENDING",
        )
        if pending:
            occurrence.refresh_from_db()
            if occurrence.status != "PENDING" or occurrence.cancellation_requested:
                return occurrence
        try:
            if (not rule.enabled or site.active_schedule_id != rule.schedule_id
                    or rule.schedule.site_id != site.pk):
                raise ValidationError("Rule is disabled or its schedule is inactive.")
            if scheduled_at:
                local_due = scheduled_at.astimezone(ZoneInfo(site.timezone))
                if (not rule.uses_weekday(local_due.weekday())
                        or (rule.start_time.hour, rule.start_time.minute)
                        != (local_due.hour, local_due.minute)):
                    raise ValidationError("Scheduled rule changed before admission; skipped.")
            validate_configuration(rule, members)
            if pending:
                saved_members = config["members"]
                current_by_id = {member.valve_id: member for member in members}
                if (rule.mode != occurrence.mode
                        or set(current_by_id) != {
                            member["valve_id"] for member in saved_members
                        }):
                    raise ValidationError("Requested rule membership or mode changed.")
                # A queued request already has its immutable configuration.
                # Later edits can reduce admissible limits, never rewrite it.
                members = []
                for saved in saved_members:
                    valve = current_by_id[saved["valve_id"]].valve
                    valve.application_rate_mm_h = saved["application_rate_mm_h"]
                    members.append(GroupedRuleValve(
                        rule=rule, valve=valve, order=saved["order"],
                        duration_seconds=saved["duration_seconds"],
                    ))
            if any(not m.valve.relay_device.enabled for m in members):
                raise ValidationError("A relay device is disabled.")
            if _group_conflict(site, occurrence):
                raise ValidationError(
                    "Skipped: conflicting active or uncertain watering at admission."
                )
            if scheduled_at and not 0 <= (now - scheduled_at).total_seconds() < 60:
                raise ValidationError("Scheduled minute has elapsed; no catch-up.")
            reservation_start = scheduled_at or now
            available = _remaining_day_seconds(site, reservation_start)
            if not pending:
                config = _snapshot(rule, members, available_seconds=available)
                occurrence.config = config
            end = occurrence.reservation_end or _deadline(
                site, reservation_start, config.get(
                    "reserved_seconds",
                    config.get("watering_seconds", 0)
                    + config.get("handover_seconds", 0),
                ),
            )
            decision = build_smart_decision(
                site, members, now, available_seconds=available,
                controller_interval_seconds=controller_interval(),
                command_allowance_seconds=command_allowance(),
            ) if rule.mode == "SMART" else {}
            if rule.mode == "SMART":
                sequence = decision["sequence"]
            else:
                sequence = plan_sequence(
                    [{"valve_id": m.valve_id, "order": m.order,
                      "total_seconds": m.duration_seconds,
                      "run_cap_seconds": m.duration_seconds} for m in members],
                    smart=False, available_seconds=available,
                    controller_interval_seconds=controller_interval(),
                    command_allowance_seconds=command_allowance(),
                )
            # This immutable finite budget belongs to this decision only. Rate
            # restoration or later settings cannot append work to the sequence.
            occurrence.config = {**config, "pulse_budget": sequence["pulse_count"]}
            by_id = {member.valve_id: member for member in members}
            plans = []
            for pulse in sequence["pulses"]:
                member = by_id[pulse["valve_id"]]
                plans.append(IrrigationRun(
                    valve=member.valve, occurrence=occurrence,
                    pass_number=pulse["pass_number"], member_order=member.order,
                    trigger=occurrence.source, requested_start_at=occurrence.requested_at,
                    planned_start_at=scheduled_at, status="PLANNED",
                    optimal_duration_seconds=pulse["duration_seconds"],
                    max_duration_seconds=member.duration_seconds,
                    application_rate_mm_h=(member.valve.application_rate_mm_h
                        if member.valve.has_valid_application_rate else None),
                ))
            IrrigationRun.objects.bulk_create(plans)
            occurrence.decision = decision
            occurrence.decision_at = now
            occurrence.reservation_end = end
            skipped = any(
                row.get("skipped") for row in decision.get("valves", {}).values()
            )
            if plans:
                occurrence.status = "ACTIVE"
                occurrence.outcome = (
                    "Planned; some Smart valves skipped because they need a watering rate"
                    if skipped else "Planned"
                )
            elif skipped:
                occurrence.status = "SKIPPED"
                occurrence.outcome = (
                    "No pulses planned; unavailable Smart valves need a watering rate"
                )
            else:
                occurrence.status = "ZERO"
                occurrence.outcome = "No whole-second watering dose planned"
        except (ValidationError, ValueError) as exc:
            occurrence.status = "SKIPPED"
            occurrence.outcome = str(exc)
            occurrence.decision_at = now
        occurrence.save()
        return occurrence


def occurrence_next_eligible_at(occurrence, runs=None):
    """Derive durable rest eligibility without sliding timestamps or writes."""
    if (occurrence.mode != "SMART" or occurrence.status != "ACTIVE"
            or occurrence.config.get("sequence_policy") != "equal_previous_run_v1"):
        return None
    if runs is None:
        runs = list(occurrence.runs.order_by("pass_number", "member_order"))
    else:
        runs = sorted(runs, key=lambda run: (run.pass_number, run.member_order))
    if any(run.attempt_started_at and (
            run.status in ("PLANNED", "RUNNING")
            or run.closure_confirmed_at is None) for run in runs):
        return None
    pending = next((run for run in runs if run.status == "PLANNED"
                    and not run.attempt_started_at), None)
    if pending is None:
        return None
    prior = [run for run in runs if run.valve_id == pending.valve_id
             and run.attempt_started_at and run.closure_confirmed_at]
    if not prior:
        return None
    previous = prior[-1]
    return previous.closure_confirmed_at + dt.timedelta(
        seconds=previous.optimal_duration_seconds,
    )


def _skip_uncalibrated_pulses(occurrence, valve_id=None):
    """Permanently skip unattempted work; never modify a commanded pulse."""
    if occurrence.mode != "SMART":
        return
    pending = IrrigationRun.objects.filter(
        occurrence=occurrence, status="PLANNED", attempt_started_at=None,
    ).select_related("valve")
    unavailable = [
        run.pk for run in pending
        if run.valve_id == valve_id or not run.valve.has_valid_application_rate
    ]
    if not unavailable:
        return
    with site_admission(occurrence.site):
        changed = IrrigationRun.objects.filter(
            pk__in=unavailable, status="PLANNED", attempt_started_at=None,
        ).update(
            status="FAILED", stop_reason="ERROR",
            error_message=MISSING_RATE_SKIP,
        )
        if changed:
            RuleOccurrence.objects.filter(pk=occurrence.pk, status="ACTIVE").update(
                outcome=(
                    "Some Smart valves skipped: enter their watering rates "
                    "for future Smart watering"
                )
            )


def _progress(occurrence, fresh_closed):
    occurrence.refresh_from_db()
    if occurrence.status == "STOPPING":
        attempted = IrrigationRun.objects.filter(
            occurrence=occurrence, attempt_started_at__isnull=False
        )
        if not attempted.filter(closure_confirmed_at__isnull=True).exists():
            RuleOccurrence.objects.filter(pk=occurrence.pk).update(status="CANCELLED")
        return
    if occurrence.status != "ACTIVE":
        return
    current_rule = GroupedRule.objects.filter(pk=occurrence.rule_id).first()
    current_site = Site.objects.get(pk=occurrence.site_id)
    if (current_rule is None or not current_rule.enabled
            or current_site.active_schedule_id != current_rule.schedule_id
            or current_rule.schedule.site_id != occurrence.site_id
            or occurrence.config.get("schedule_id") != current_rule.schedule_id
            or occurrence.config.get("timezone", current_site.timezone)
            != current_site.timezone):
        cancel_occurrence(occurrence, "Rule disabled, deleted, or active schedule changed")
        reconcile_attempts()
        return
    _skip_uncalibrated_pulses(occurrence)
    if any(not member.valve.relay_device.enabled for member in members_for(current_rule)):
        cancel_occurrence(occurrence, "Relay disabled; remaining target unmet")
        reconcile_attempts()
        return
    if _group_conflict(current_site, occurrence):
        cancel_occurrence(occurrence, "Conflicting watering interrupted the group")
        reconcile_attempts()
        return
    runs = list(IrrigationRun.objects.filter(occurrence=occurrence).select_related(
        "valve__relay_device"
    ).order_by("pass_number", "member_order"))
    attempted = [run for run in runs if run.attempt_started_at]
    if any(run.status in ("RUNNING", "PLANNED") for run in attempted):
        return
    if any(run.delivery_uncertain for run in attempted):
        cancel_occurrence(occurrence, "Uncertain delivery; remaining target unmet")
        return
    # A later pass may reopen an earlier valve. Confirm the immediately prior
    # pulse only, after first establishing that no pulse remains active.
    previous = attempted[-1] if attempted else None
    if (previous and previous.pk not in fresh_closed
            and not _confirmed_closed(previous)):
        cancel_occurrence(occurrence, "Closure unconfirmed; remaining target unmet")
        return
    pending = next((r for r in runs if r.status == "PLANNED" and not r.attempt_started_at), None)
    if pending is None:
        skipped = (
            any(run.error_message == MISSING_RATE_SKIP for run in runs)
            or any(row.get("skipped") for row in
                   occurrence.decision.get("valves", {}).values())
        )
        RuleOccurrence.objects.filter(pk=occurrence.pk, status="ACTIVE").update(
            status="SKIPPED" if skipped and not attempted else "FINISHED",
            outcome=(
                "Completed available watering; some Smart valves skipped for missing rates"
                if skipped else "Completed planned watering"
            ),
        )
        return
    eligible = occurrence_next_eligible_at(occurrence)
    if eligible and timezone.now() < eligible:
        if eligible + dt.timedelta(
            seconds=pending.optimal_duration_seconds + command_allowance()
        ) > occurrence.reservation_end:
            cancel_occurrence(occurrence, "Reservation deadline exhausted; remaining target unmet")
        # A rest needs no admission claim and no database write on every tick.
        return
    with site_admission(occurrence.site):
        occurrence.refresh_from_db()
        pending.refresh_from_db()
        if (occurrence.status != "ACTIVE" or occurrence.cancellation_requested
                or pending.attempt_started_at):
            return
        try:
            site = Site.objects.get(pk=occurrence.site_id)
            rule = GroupedRule.objects.filter(pk=occurrence.rule_id).first()
            if (rule is None or not rule.enabled
                    or site.active_schedule_id != rule.schedule_id
                    or rule.schedule.site_id != occurrence.site_id
                    or occurrence.config.get("schedule_id") != rule.schedule_id):
                raise ValidationError("Rule disabled, deleted, or schedule changed")
            validate_configuration(rule)
            if _group_conflict(site, occurrence):
                raise ValidationError("Conflicting watering interrupted the group")
            pending.valve.refresh_from_db()
            pending.valve.relay_device.refresh_from_db()
            if pending.valve.relay_device.site_id != occurrence.site_id:
                raise ValidationError("Valve ownership changed; remaining target unmet")
            if (occurrence.mode == "SMART"
                    and not pending.valve.has_valid_application_rate):
                _skip_uncalibrated_pulses(occurrence)
                return
            if not pending.valve.relay_device.enabled:
                raise ValidationError("Relay disabled")
            member = GroupedRuleValve.objects.filter(rule=rule, valve=pending.valve).first()
            if (rule.mode != occurrence.mode or member is None
                    or pending.optimal_duration_seconds > member.duration_seconds):
                raise ValidationError("Configuration changed; remaining target unmet")
            saved_members = occurrence.config.get("members", [])
            current_members = members_for(rule)
            if [(m.valve_id, m.order) for m in current_members] != [
                (m["valve_id"], m["order"]) for m in saved_members
            ]:
                raise ValidationError("Valve order or membership changed; remaining target unmet")
            budget = occurrence.config.get("pulse_budget")
            if budget is not None and occurrence.runs.count() > budget:
                raise ValidationError("Saved pulse budget exceeded; remaining target unmet")
            now = timezone.now()
            eligible = occurrence_next_eligible_at(occurrence)
            command_start = max(now, eligible) if eligible else now
            command_end = command_start + dt.timedelta(
                seconds=pending.optimal_duration_seconds + command_allowance()
            )
            if command_end > occurrence.reservation_end:
                raise ValidationError("Reservation deadline exhausted; remaining target unmet")
            if eligible and now < eligible:
                # Reservation and cancellation remain durable while the normal
                # controller cadence waits. No blocking sleep or status write.
                return
            claimed = IrrigationRun.objects.filter(
                pk=pending.pk, status="PLANNED", attempt_started_at=None,
                cancellation_requested=False,
            ).update(
                attempt_started_at=now, delivery_uncertain=True,
                dispatch_state="UNSENT",
            )
            if not claimed:
                return
        except ValidationError as exc:
            cancel_occurrence(occurrence, str(exc))
            return
    _send_claimed(pending)


def recover_groups():
    """Called once on controller startup; attempts are never replayed."""
    for occurrence in RuleOccurrence.objects.filter(status__in=RESERVED_STATUSES):
        cancel_occurrence(occurrence, "Controller restarted; unfinished sequence cancelled")
    # Single-valve attempted PLANNED/FAILED rows also need explicit recovery.
    reconcile_attempts(restarting=True)


def reconcile_attempts(restarting=False, *, senders_stopped=False):
    """Reconcile outcomes without releasing a surviving sender's ownership.

    Time bounds request cancellation and closure; they never prove that a web
    sender cannot subsequently transmit. Only its acknowledgement, or an
    operator's explicit confirmation that senders have stopped, resolves that.
    """
    fresh_closed = set()
    query = IrrigationRun.objects.filter(
        Q(attempt_started_at__isnull=False, closure_confirmed_at__isnull=True)
        | Q(dispatch_state="SENDING")
    ).select_related("valve__relay_device", "occurrence")
    for run in query:
        now = timezone.now()
        stopping = run.cancellation_requested or (
            run.occurrence and run.occurrence.status == "STOPPING"
        )
        bounded_until = (
            run.attempt_started_at + dt.timedelta(
                seconds=command_allowance() + controller_interval()
            ) if run.attempt_started_at else now
        )
        expired = now > bounded_until
        if run.dispatch_state == "UNSENT":
            if not (restarting or stopping or expired or senders_stopped):
                continue
            with site_admission(run.valve.relay_device.site):
                revoked = IrrigationRun.objects.filter(
                    pk=run.pk, dispatch_state="UNSENT",
                ).update(
                    dispatch_state="DONE", cancellation_requested=True,
                    status="FAILED", stop_reason="MANUAL_STOP",
                    error_message="Opening cancelled before sender dispatch",
                    attempt_started_at=None, attempt_finished_at=now,
                    delivery_uncertain=False, closure_confirmed_at=now,
                )
            if revoked:
                if run.occurrence:
                    cancel_occurrence(run.occurrence, "Unsent opening cancelled")
                continue
            run.refresh_from_db()
        if run.dispatch_state == "SENDING":
            controller_sender = run.occurrence_id is not None or run.trigger == "SCHEDULED"
            orphaned = senders_stopped or (restarting and controller_sender)
            if not (restarting or stopping or expired or orphaned):
                continue
            if (orphaned or not run.cancellation_requested
                    or run.closure_confirmed_at is not None):
                with site_admission(run.valve.relay_device.site):
                    updates = {
                        "cancellation_requested": True,
                        "closure_confirmed_at": None,
                    }
                    if orphaned:
                        # No invented attempt end: extra delivery stays unknown.
                        updates.update(
                            dispatch_state="DONE", delivery_uncertain=True,
                        )
                    IrrigationRun.objects.filter(
                        pk=run.pk, dispatch_state="SENDING",
                    ).update(**updates)
            run.refresh_from_db()
            stopping = True
            if run.dispatch_state == "SENDING":
                _confirmed_closed(run, close=True)
                continue
        if run.status == "RUNNING" and not stopping:
            if restarting and run.occurrence_id:
                # Startup cancellation should already cover every group.
                stopping = True
            stop_at = run.actual_start_at + dt.timedelta(
                seconds=run.optimal_duration_seconds or run.max_duration_seconds
            ) if run.actual_start_at else None
            if not stopping and (stop_at is None or timezone.now() < stop_at):
                continue
        # Legacy claims lack the new dispatch barrier. Deployment must stop old
        # web senders as well as the controller before upgrading that protocol.
        if (run.attempt_finished_at is None and run.actual_start_at is None
                and not restarting and not senders_stopped):
            if not run.occurrence:
                if not expired:
                    continue
        needs_close = (restarting or stopping or run.delivery_uncertain
                       or run.status in ("PLANNED", "FAILED"))
        if _confirmed_closed(run, close=needs_close):
            fresh_closed.add(run.pk)
        elif run.occurrence:
            cancel_occurrence(run.occurrence, "Closure unconfirmed; sequence interrupted")
    return fresh_closed


def group_tick(now=None):
    fresh_closed = reconcile_attempts()
    for occurrence in RuleOccurrence.objects.filter(status__in=("ACTIVE", "STOPPING")):
        try:
            _progress(occurrence, fresh_closed)
        except Exception:
            logger.exception("Group progression failed for occurrence %s", occurrence.pk)
            cancel_occurrence(occurrence, "Execution error; remaining pulses cancelled")
    for pending in RuleOccurrence.objects.filter(status="PENDING").select_related(
        "rule__schedule__site"
    ):
        if pending.rule_id:
            occurrence = _plan_occurrence(pending.rule, pending=pending)
            if occurrence:
                _progress(occurrence, fresh_closed)
        else:
            cancel_occurrence(pending, "Rule deleted")
    # The fresh admitted time, not an old loop timestamp, defines the balance.
    decision_time = timezone.now() if now is None else now
    rules = GroupedRule.objects.filter(
        enabled=True, schedule__active_sites__isnull=False
    ).select_related("schedule__site")
    for rule in rules:
        local = decision_time.astimezone(ZoneInfo(rule.schedule.site.timezone))
        if not rule.uses_weekday(local.weekday()):
            continue
        nominal = dt.datetime.combine(local.date(), rule.start_time, local.tzinfo)
        normalized = nominal.astimezone(UTC).astimezone(local.tzinfo)
        if normalized.replace(tzinfo=None) != nominal.replace(tzinfo=None):
            if local.replace(tzinfo=None) >= nominal.replace(tzinfo=None):
                with site_admission(rule.schedule.site):
                    RuleOccurrence.objects.get_or_create(
                        rule=rule, scheduled_local_date=local.date(), defaults={
                            "site": rule.schedule.site, "mode": rule.mode,
                            "scheduled_at": nominal.astimezone(UTC), "requested_at": decision_time,
                            "source": "SCHEDULED", "status": "SKIPPED",
                            "outcome": "Nonexistent local scheduled time (DST gap)",
                            "config": _snapshot(
                                rule, members_for(rule), include_reservation=False,
                            ),
                        },
                    )
            continue
        if (local.hour, local.minute) != (rule.start_time.hour, rule.start_time.minute):
            continue
        scheduled_at = nominal.replace(second=0, microsecond=0, fold=local.fold).astimezone(UTC)
        try:
            occurrence = _plan_occurrence(rule, scheduled_at=scheduled_at)
            if occurrence:
                _progress(occurrence, fresh_closed)
        except Exception:
            logger.exception("Planning failed for group %s", rule.pk)
