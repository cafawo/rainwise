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
from django.db.models import F, Q
from django.utils import timezone

from apps.irrigation import services
from apps.irrigation.models import (
    CurveSettings, GroupedRule, GroupedRuleValve, IrrigationRun,
    RuleOccurrence, ScheduleRule, Site, Valve, normalize_rule_mode,
)

logger = logging.getLogger(__name__)
RESERVED_STATUSES = ("PENDING", "ACTIVE", "STOPPING")
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


def reservation_seconds(rule, members=None):
    members = members_for(rule) if members is None else members
    passes = 2 if rule.mode == "SMART" else 1
    return (passes * sum(m.duration_seconds for m in members),
            passes * len(members) * controller_interval())


def _seconds(value):
    return value.hour * 3600 + value.minute * 60 + value.second


def _windows(rule, duration):
    for weekday in range(7):
        if rule.uses_weekday(weekday):
            start = weekday * 86400 + _seconds(rule.start_time)
            yield start, start + duration


def _overlaps(left, left_duration, right, right_duration):
    week = 7 * 86400
    return any(a < d + offset and c + offset < b
               for a, b in _windows(left, left_duration)
               for c, d in _windows(right, right_duration)
               for offset in (-week, 0, week))


def validate_configuration(rule, members=None, exclude_rule=None):
    """Validate ownership, bounds and automatic reservations without I/O."""
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
        curve = None
        if rule.mode == "SMART" and rule.enabled:
            curve = CurveSettings.objects.filter(site=rule.schedule.site).first()
            if curve is None or curve.fallback_temperature_c is None:
                raise ValidationError(
                    "Configure curve and fallback temperature before enabling Smart."
                )
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
            if rule.mode == "SMART":
                if duration > valve.default_max_duration_seconds:
                    raise ValidationError("Smart duration exceeds the valve's maximum.")
                rate = valve.application_rate_mm_h
                if rule.enabled and (rate is None or not math.isfinite(rate) or rate <= 0):
                    raise ValidationError(
                        "Every Smart valve needs a finite positive application rate."
                    )
        water, handover = reservation_seconds(rule, members)
        duration = water + handover
        if _seconds(rule.start_time) + duration > 86400:
            raise ValidationError("A group reservation cannot cross local midnight.")
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
            raise ValidationError("The reservation overlaps another automatic rule.")
    if grouped:
        for other in ScheduleRule.objects.filter(schedule=rule.schedule, enabled=True):
            if isinstance(exclude_rule, ScheduleRule) and other.pk == exclude_rule.pk:
                continue
            if _overlaps(rule, duration, other, other.max_duration_seconds):
                raise ValidationError("The reservation overlaps another automatic rule.")


def validate_schedule(schedule):
    for rule in GroupedRule.objects.filter(schedule=schedule, enabled=True):
        validate_configuration(rule)


def _reservations(site, exclude=None):
    query = RuleOccurrence.objects.filter(site=site, status__in=RESERVED_STATUSES)
    return query.exclude(pk=exclude.pk) if exclude else query


def _unresolved_runs(site):
    return IrrigationRun.objects.filter(valve__relay_device__site=site).filter(
        Q(status="RUNNING") |
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


def _confirmed_closed(run, *, close=False):
    """Never use the cached state as evidence of a fresh closure."""
    if close:
        try:
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
    now = timezone.now()
    updates = {"closure_confirmed_at": now}
    if run.status in ("RUNNING", "PLANNED"):
        updates.update(status="FINISHED", actual_stop_at=now,
                       stop_reason="MANUAL_STOP" if close else "COMPLETED")
    IrrigationRun.objects.filter(pk=run.pk).update(**updates)
    Valve.objects.filter(pk=run.valve_id, last_known_is_open=True).update(
        last_known_is_open=False, last_polled_at=now
    )
    return True


def _send_claimed(run):
    """Transmit one already committed logical attempt; never replay it."""
    run.refresh_from_db()
    occurrence = run.occurrence
    if run.cancellation_requested or (occurrence and occurrence.cancellation_requested):
        IrrigationRun.objects.filter(pk=run.pk).update(
            status="FAILED", attempt_started_at=None, attempt_finished_at=timezone.now(),
            delivery_uncertain=False, closure_confirmed_at=timezone.now(),
            error_message="Cancelled before command", stop_reason="MANUAL_STOP",
        )
        return run
    now = timezone.now()
    command_end = now + dt.timedelta(
        seconds=run.optimal_duration_seconds + command_allowance()
    )
    if occurrence and command_end > occurrence.reservation_end:
        IrrigationRun.objects.filter(pk=run.pk).update(
            status="FAILED", attempt_started_at=None, attempt_finished_at=now,
            delivery_uncertain=False, closure_confirmed_at=now,
            error_message="Reservation deadline exhausted before command",
        )
        cancel_occurrence(occurrence, "Reservation deadline exhausted; target unmet")
        return run
    try:
        services.open_valve_for(run.valve, run.optimal_duration_seconds)
    except Exception as exc:
        IrrigationRun.objects.filter(pk=run.pk).update(
            status="FAILED", attempt_finished_at=timezone.now(),
            delivery_uncertain=True, stop_reason="ERROR", error_message=str(exc),
        )
        if occurrence:
            cancel_occurrence(occurrence, "Ambiguous opening; remaining pulses cancelled")
        raise
    finished = timezone.now()
    try:
        IrrigationRun.objects.filter(pk=run.pk).update(
            status="RUNNING", actual_start_at=now, attempt_finished_at=finished,
            delivery_uncertain=False,
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
        valve.refresh_from_db()
        valve.relay_device.refresh_from_db()
        if not valve.relay_device.enabled:
            raise ValidationError("Relay device is disabled.")
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
        now = timezone.now()
        rate = valve.application_rate_mm_h
        if rate is not None and (not math.isfinite(rate) or rate <= 0):
            rate = None
        run = IrrigationRun.objects.create(
            valve=valve, trigger=trigger, requested_start_at=now,
            planned_start_at=planned_start_at, status="PLANNED",
            optimal_duration_seconds=duration, max_duration_seconds=duration,
            application_rate_mm_h=rate, attempt_started_at=now,
            delivery_uncertain=True,
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


def _snapshot(rule, members):
    water, handover = reservation_seconds(rule, members)
    config = {
        "rule_id": rule.pk, "schedule_id": rule.schedule_id, "note": rule.note,
        "enabled": rule.enabled,
        "days_of_week_mask": rule.days_of_week_mask,
        "start_time": rule.start_time.isoformat(),
        "timezone": rule.schedule.site.timezone,
        "mode": rule.mode, "watering_seconds": water, "handover_seconds": handover,
        "controller_interval_seconds": controller_interval(),
        "members": [{"valve_id": m.valve_id, "name": m.valve.name,
                     "order": m.order, "duration_seconds": m.duration_seconds,
                     "application_rate_mm_h": m.valve.application_rate_mm_h}
                    for m in members],
    }
    if rule.mode == "SMART":
        curve = CurveSettings.objects.filter(site=rule.schedule.site).first()
        if curve:
            config["curve"] = {
                name: getattr(curve, name) for name in (
                    "min_mm", "max_mm", "g", "m", "coverage_days",
                    "fallback_temperature_c",
                )
            }
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
        config = _snapshot(rule, members)
        end = _deadline(site, now, config["watering_seconds"] + config["handover_seconds"])
        return RuleOccurrence.objects.create(
            site=site, rule=rule, mode="FIXED", config=config,
            source="MANUAL", requested_at=now, reservation_end=end, status="PENDING",
        )


def _plan_occurrence(rule, scheduled_at=None, pending=None, now=None):
    from apps.irrigation.balance import build_smart_decision

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
        config = pending.config if pending else _snapshot(rule, members)
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
            if not rule.enabled or site.active_schedule_id != rule.schedule_id:
                raise ValidationError("Rule is disabled or its schedule is inactive.")
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
            end = occurrence.reservation_end or _deadline(
                site, scheduled_at or now,
                config["watering_seconds"] + config["handover_seconds"],
            )
            decision = build_smart_decision(site, members, now) if rule.mode == "SMART" else {}
            plans = []
            for member in members:
                if rule.mode == "FIXED":
                    seconds = member.duration_seconds
                elif rule.mode == "SMART":
                    seconds = decision["valves"][str(member.valve_id)]["planned_seconds"]
                else:
                    raise ValidationError("Unsupported group mode.")
                for pass_number in range(1, (2 if rule.mode == "SMART" else 1) + 1):
                    duration = min(seconds, member.duration_seconds)
                    seconds -= duration
                    if duration <= 0:
                        continue
                    plans.append(IrrigationRun(
                        valve=member.valve, occurrence=occurrence,
                        pass_number=pass_number, member_order=member.order,
                        trigger=occurrence.source, requested_start_at=occurrence.requested_at,
                        planned_start_at=scheduled_at, status="PLANNED",
                        optimal_duration_seconds=duration,
                        max_duration_seconds=member.duration_seconds,
                        application_rate_mm_h=member.valve.application_rate_mm_h,
                    ))
            IrrigationRun.objects.bulk_create(plans)
            occurrence.decision = decision
            occurrence.decision_at = now
            occurrence.reservation_end = end
            occurrence.status = "ACTIVE" if plans else "ZERO"
            occurrence.outcome = (
                "Planned" if plans else "No whole-second watering dose planned"
            )
        except (ValidationError, ValueError) as exc:
            occurrence.status = "SKIPPED"
            occurrence.outcome = str(exc)
            occurrence.decision_at = now
        occurrence.save()
        return occurrence


def _attempts_today(valve, site, now):
    tz = ZoneInfo(site.timezone)
    date = now.astimezone(tz).date()
    start = dt.datetime.combine(date, dt.time(), tz).astimezone(UTC)
    end = dt.datetime.combine(date + dt.timedelta(days=1), dt.time(), tz).astimezone(UTC)
    return IrrigationRun.objects.filter(
        valve=valve, occurrence__mode="SMART",
        attempt_started_at__gte=start, attempt_started_at__lt=end,
    ).count()


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
            or occurrence.config.get("timezone", current_site.timezone)
            != current_site.timezone):
        cancel_occurrence(occurrence, "Rule disabled, deleted, or active schedule changed")
        reconcile_attempts()
        return
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
        RuleOccurrence.objects.filter(pk=occurrence.pk, status="ACTIVE").update(
            status="FINISHED", outcome="Completed planned watering"
        )
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
            if rule is None or not rule.enabled or site.active_schedule_id != rule.schedule_id:
                raise ValidationError("Rule disabled, deleted, or schedule changed")
            validate_configuration(rule)
            if _group_conflict(site, occurrence):
                raise ValidationError("Conflicting watering interrupted the group")
            pending.valve.refresh_from_db()
            pending.valve.relay_device.refresh_from_db()
            if not pending.valve.relay_device.enabled:
                raise ValidationError("Relay disabled")
            member = GroupedRuleValve.objects.filter(rule=rule, valve=pending.valve).first()
            if (rule.mode != occurrence.mode or member is None
                    or pending.optimal_duration_seconds > member.duration_seconds):
                raise ValidationError("Configuration changed; remaining target unmet")
            now = timezone.now()
            if occurrence.mode == "SMART":
                if pending.optimal_duration_seconds > pending.valve.default_max_duration_seconds:
                    raise ValidationError("Smart valve limit reduced; remaining target unmet")
                if _attempts_today(pending.valve, site, now) >= 2:
                    raise ValidationError("Two Smart attempts already used today; target unmet")
            command_end = now + dt.timedelta(
                seconds=pending.optimal_duration_seconds + command_allowance()
            )
            if command_end > occurrence.reservation_end:
                raise ValidationError("Reservation deadline exhausted; remaining target unmet")
            claimed = IrrigationRun.objects.filter(
                pk=pending.pk, status="PLANNED", attempt_started_at=None,
                cancellation_requested=False,
            ).update(attempt_started_at=now, delivery_uncertain=True)
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


def reconcile_attempts(restarting=False):
    fresh_closed = set()
    query = IrrigationRun.objects.filter(
        attempt_started_at__isnull=False, closure_confirmed_at__isnull=True,
    ).select_related("valve__relay_device", "occurrence")
    for run in query:
        stopping = run.cancellation_requested or (
            run.occurrence and run.occurrence.status == "STOPPING"
        )
        if run.status == "RUNNING" and not stopping:
            if restarting and run.occurrence_id:
                # Startup cancellation should already cover every group.
                stopping = True
            stop_at = run.actual_start_at + dt.timedelta(
                seconds=run.optimal_duration_seconds or run.max_duration_seconds
            ) if run.actual_start_at else None
            if not stopping and (stop_at is None or timezone.now() < stop_at):
                continue
        # Do not mistake another process's in-flight call for a crashed command.
        if (run.attempt_finished_at is None and run.actual_start_at is None
                and not restarting):
            if not run.occurrence:
                age = (timezone.now() - run.attempt_started_at).total_seconds()
                if age <= command_allowance() + controller_interval():
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
                            "config": _snapshot(rule, members_for(rule)),
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
