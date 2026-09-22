"""Rule validation, immediate controls and controller-owned in-memory sequences."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import datetime as dt
import logging
import math
import os
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.irrigation import services
from apps.irrigation.models import (
    GroupedRule, GroupedRuleValve, IrrigationRun, RelayDevice,
    ScheduleRule, Valve, get_curve_settings, normalize_rule_mode,
)

logger = logging.getLogger(__name__)
UTC = dt.timezone.utc
RETRIED_OPENING = (
    "Opening succeeded after a transport retry; physical delivery is uncertain."
)


def controller_interval() -> int:
    try:
        return max(1, int(os.environ.get("CONTROLLER_INTERVAL_SECONDS", "60")))
    except ValueError:
        return 60


def command_allowance() -> float:
    return 3 * services.MODBUS_TIMEOUT_SECONDS * (services.MODBUS_RETRIES + 1)


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


def _open_run(run):
    """Use the unchanged timed relay command and record the actual attempt."""
    started = timezone.now()
    returned = None
    try:
        retried = services.open_valve_for(
            run.valve, run.optimal_duration_seconds,
        ) is True
        returned = timezone.now()
        IrrigationRun.objects.filter(pk=run.pk).update(
            status="RUNNING", actual_start_at=started,
            attempt_finished_at=returned, delivery_uncertain=retried,
            error_message=RETRIED_OPENING if retried else "",
        )
    except Exception as exc:
        try:
            IrrigationRun.objects.filter(pk=run.pk).update(
                status="FAILED", stop_reason="ERROR", error_message=str(exc),
                attempt_finished_at=returned or timezone.now(),
                delivery_uncertain=True,
            )
        except Exception:
            logger.exception("Could not record opening result for run %s", run.pk)
        raise
    run.refresh_from_db()
    return run


def _new_run(valve, duration, trigger, *, planned_start_at=None, maximum=None):
    rate = valve.application_rate_mm_h if valve.has_valid_application_rate else None
    return IrrigationRun.objects.create(
        valve=valve, trigger=trigger, requested_start_at=timezone.now(),
        planned_start_at=planned_start_at, status="PLANNED",
        optimal_duration_seconds=duration,
        max_duration_seconds=maximum or duration,
        application_rate_mm_h=rate, attempt_started_at=timezone.now(),
        delivery_uncertain=True,
    )


def start_single(valve, duration, trigger, planned_start_at=None, rule=None):
    """Preserve immediate Fixed/manual starts, including the selected tick minute."""
    services._duration_to_flash_ticks(duration)
    if rule and normalize_rule_mode(rule.mode) != "FIXED":
        raise ValidationError("Unsupported single-valve rule mode.")
    valve.refresh_from_db()
    if not valve.relay_device.enabled:
        raise ValidationError("Relay device is disabled.")
    if planned_start_at is not None:
        existing = IrrigationRun.objects.filter(
            valve=valve, planned_start_at=planned_start_at, trigger="SCHEDULED",
        ).first()
        if existing:
            return existing
    if trigger == "MANUAL" and rule is None and IrrigationRun.objects.filter(
        valve=valve, status="RUNNING",
    ).exists():
        raise ValidationError("Valve is already running.")
    run = _new_run(valve, duration, trigger, planned_start_at=planned_start_at)
    return _open_run(run)


def close_member(valve):
    """Close this valve immediately, using the released service-layer behavior."""
    runs = IrrigationRun.objects.filter(valve=valve, status="RUNNING")
    try:
        services.close_valve(valve)
    except Exception as exc:
        runs.update(status="FAILED", stop_reason="ERROR", error_message=str(exc))
        raise
    stopped = timezone.now()
    with transaction.atomic():
        runs.update(
            status="FINISHED", actual_stop_at=stopped,
            closure_confirmed_at=stopped, stop_reason="MANUAL_STOP",
        )
        Valve.objects.filter(pk=valve.pk).update(
            last_known_is_open=False, last_polled_at=stopped,
        )


def _remaining_day_seconds(site, start):
    tz = ZoneInfo(site.timezone)
    midnight = dt.datetime.combine(
        start.astimezone(tz).date() + dt.timedelta(days=1), dt.time(), tz,
    ).astimezone(UTC)
    return (midnight - start.astimezone(UTC)).total_seconds()


def _group_pulse_end(run):
    returned = run.attempt_finished_at or run.actual_start_at
    if returned is None:
        # An interrupted attempt remains uncertain; this is a bookkeeping bound,
        # not a claim that a closing response was received.
        returned = run.attempt_started_at + dt.timedelta(seconds=command_allowance())
    return returned + dt.timedelta(seconds=run.optimal_duration_seconds)


def _finish_group_runs(now):
    """Complete attempted pulse logs at their relay deadline, without replay."""
    runs = IrrigationRun.objects.filter(
        trigger=IrrigationRun.TRIGGER_GROUP,
        attempt_started_at__isnull=False, actual_stop_at=None,
    )
    for run in runs:
        end = _group_pulse_end(run)
        if now < end:
            continue
        updates = {"actual_stop_at": end}
        if run.status == "RUNNING":
            updates.update(status="FINISHED", stop_reason="COMPLETED")
        elif run.status == "PLANNED":
            updates.update(
                status="FAILED", stop_reason="ERROR", delivery_uncertain=True,
                error_message="Opening interrupted; remaining sequence abandoned.",
            )
        IrrigationRun.objects.filter(pk=run.pk).update(**updates)


def _site_is_watering(site_id):
    return IrrigationRun.objects.filter(
        valve__relay_device__site_id=site_id, actual_stop_at=None,
    ).filter(
        Q(status="RUNNING")
        | Q(trigger=IrrigationRun.TRIGGER_GROUP, attempt_started_at__isnull=False)
    ).exists()


@dataclass
class ActiveGroup:
    rule_id: int
    schedule_id: int
    site_id: int
    scheduled_at: dt.datetime
    deadline: dt.datetime
    valves: dict
    pulses: deque
    current: IrrigationRun | None = None
    ready_at: dict = field(default_factory=dict)
    stopped: bool = False


class GroupRunner:
    """One controller owns these transient sequences; restart discards them."""

    def __init__(self):
        self._active = {}
        self._considered = {}

    @property
    def active_sites(self):
        return set(self._active)

    def tick(self, now=None):
        now = timezone.now() if now is None else now
        _finish_group_runs(now)
        for group in list(self._active.values()):
            try:
                self._advance(group, now)
            except Exception:
                logger.exception("Group %s stopped after an execution error", group.rule_id)
                self._stop(group)

        rules = list(GroupedRule.objects.filter(
            enabled=True, schedule__active_sites__isnull=False,
        ).select_related("schedule__site"))
        current_ids = {rule.pk for rule in rules}
        self._considered = {
            pk: day for pk, day in self._considered.items() if pk in current_ids
        }
        for rule in rules:
            # Use the admitted instant for Smart water credit, not the old tick.
            admitted = timezone.now()
            local = admitted.astimezone(ZoneInfo(rule.schedule.site.timezone))
            if (not rule.uses_weekday(local.weekday())
                    or (local.hour, local.minute)
                    != (rule.start_time.hour, rule.start_time.minute)
                    or self._considered.get(rule.pk) == local.date()):
                continue
            self._considered[rule.pk] = local.date()
            scheduled = dt.datetime.combine(
                local.date(), rule.start_time.replace(second=0, microsecond=0),
                local.tzinfo,
            ).replace(fold=0).astimezone(UTC)
            # A repeated local time refers to its first occurrence. Never catch up.
            if not 0 <= (admitted - scheduled).total_seconds() < 60:
                continue
            try:
                self._start(rule, scheduled, admitted)
            except (ValidationError, ValueError) as exc:
                logger.info("Group %s skipped: %s", rule.pk, exc)
            except Exception:
                logger.exception("Could not start group %s", rule.pk)
                group = self._active.get(rule.schedule.site_id)
                if group:
                    self._stop(group)

    def _start(self, rule, scheduled, now):
        from apps.irrigation.balance import build_smart_decision
        from apps.irrigation.sequence import plan_sequence

        site = rule.schedule.site
        if site.pk in self._active or _site_is_watering(site.pk):
            logger.info("Group %s skipped: watering is already active", rule.pk)
            return
        members = members_for(rule)
        # Attempted logs also prevent restarting a sequence within its start minute.
        if IrrigationRun.objects.filter(
            trigger=IrrigationRun.TRIGGER_GROUP, planned_start_at=scheduled,
            valve__relay_device__site_id=site.pk,
        ).exists():
            return
        validate_configuration(rule, members)
        available = _remaining_day_seconds(site, scheduled)
        peak = reservation_details(rule, members, available_seconds=available)
        deadline = scheduled + dt.timedelta(seconds=peak["total_seconds"])
        if rule.mode == "SMART":
            decision = build_smart_decision(
                site, members, now, available_seconds=_remaining_day_seconds(site, now),
                controller_interval_seconds=controller_interval(),
                command_allowance_seconds=command_allowance(),
            )
            sequence = decision["sequence"]
        else:
            sequence = plan_sequence(
                [{"valve_id": member.valve_id, "order": member.order,
                  "total_seconds": member.duration_seconds,
                  "run_cap_seconds": member.duration_seconds} for member in members],
                smart=False, available_seconds=available,
                controller_interval_seconds=controller_interval(),
                command_allowance_seconds=command_allowance(),
            )
        if not sequence["pulses"]:
            logger.info("Group %s needs no watering", rule.pk)
            return
        group = ActiveGroup(
            rule.pk, rule.schedule_id, site.pk, scheduled, deadline,
            {member.valve_id: member.valve for member in members},
            deque(sequence["pulses"]),
        )
        self._active[site.pk] = group
        self._advance(group, now)

    def _stop(self, group):
        """Abandon future pulses; try early close once, otherwise use relay timeout."""
        if group.stopped:
            return
        group.stopped = True
        group.pulses.clear()
        if group.current:
            current = IrrigationRun.objects.filter(pk=group.current.pk).first()
            if current is None or current.actual_stop_at is None:
                try:
                    close_member(group.valves[group.current.valve_id])
                except Exception:
                    logger.exception("Early group close failed; relay timeout remains active")

    def _advance(self, group, now):
        if not group.stopped and not GroupedRule.objects.filter(
            pk=group.rule_id, enabled=True, schedule_id=group.schedule_id,
            schedule__site__active_schedule_id=group.schedule_id,
        ).exists():
            self._stop(group)
        if group.current:
            run = group.current
            try:
                run.refresh_from_db()
            except IrrigationRun.DoesNotExist:
                self._stop(group)
                self._active.pop(group.site_id, None)
                return
            if run.actual_stop_at is None:
                return
            if (run.status == "FAILED" or run.stop_reason == "MANUAL_STOP"
                    or run.delivery_uncertain):
                group.pulses.clear()
            group.ready_at[run.valve_id] = run.actual_stop_at + dt.timedelta(
                seconds=run.optimal_duration_seconds,
            )
            group.current = None
        if not group.pulses:
            self._active.pop(group.site_id, None)
            return
        pulse = group.pulses[0]
        valve = group.valves[pulse["valve_id"]]
        if now < group.ready_at.get(valve.pk, now):
            return
        if _site_is_watering(group.site_id):
            self._stop(group)
            return
        if not RelayDevice.objects.filter(pk=valve.relay_device_id, enabled=True).exists():
            self._stop(group)
            return
        now = timezone.now()
        duration = pulse["duration_seconds"]
        if now + dt.timedelta(seconds=duration + command_allowance()) > group.deadline:
            logger.info("Group %s stopped at its reservation deadline", group.rule_id)
            self._stop(group)
            return
        group.pulses.popleft()
        group.current = _new_run(
            valve, duration, IrrigationRun.TRIGGER_GROUP,
            planned_start_at=group.scheduled_at,
        )
        _open_run(group.current)
