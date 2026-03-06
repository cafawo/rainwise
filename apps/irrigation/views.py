from __future__ import annotations

import datetime as dt
import random
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.irrigation import services
from apps.irrigation.forms import ScheduleRuleForm
from apps.irrigation.models import IrrigationRun, ScheduleRule, Valve


@login_required
def dashboard(request: HttpRequest) -> HttpResponse:
    valves = Valve.objects.select_related("relay_device").order_by("name")
    running_runs = (
        IrrigationRun.objects.filter(status=IrrigationRun.STATUS_RUNNING)
        .select_related("valve")
        .order_by("-actual_start_at")
    )
    running_valve_ids = [run.valve_id for run in running_runs]
    selected_valve_id = valves[0].id if valves else None
    return render(
        request,
        "irrigation/dashboard.html",
        {
            "valves": valves,
            "running_valve_ids": running_valve_ids,
            "selected_valve_id": selected_valve_id,
        },
    )


@login_required
@require_POST
def open_valve_view(request: HttpRequest, valve_id: int) -> HttpResponse:
    valve = get_object_or_404(Valve, pk=valve_id)
    if IrrigationRun.objects.filter(
        valve=valve, status=IrrigationRun.STATUS_RUNNING
    ).exists():
        messages.warning(request, "Valve is already running.")
        return redirect("dashboard")

    now = timezone.now()
    max_duration = valve.default_max_duration_seconds
    optimal_duration = max_duration

    run = IrrigationRun.objects.create(
        valve=valve,
        trigger=IrrigationRun.TRIGGER_MANUAL,
        requested_start_at=now,
        planned_start_at=None,
        actual_start_at=None,
        optimal_duration_seconds=optimal_duration,
        max_duration_seconds=max_duration,
        status=IrrigationRun.STATUS_PLANNED,
    )

    try:
        services.open_valve(valve)
    except Exception as exc:  # noqa: BLE001 - surface hardware errors to user
        run.status = IrrigationRun.STATUS_FAILED
        run.stop_reason = IrrigationRun.STOP_ERROR
        run.error_message = str(exc)
        run.save(update_fields=["status", "stop_reason", "error_message"])
        messages.error(request, f"Failed to open valve: {exc}")
        return redirect("dashboard")

    run.status = IrrigationRun.STATUS_RUNNING
    run.actual_start_at = now
    run.save(update_fields=["status", "actual_start_at"])
    messages.success(request, "Valve opened.")
    return redirect("dashboard")


@login_required
@require_POST
def close_valve_view(request: HttpRequest, valve_id: int) -> HttpResponse:
    valve = get_object_or_404(Valve, pk=valve_id)
    now = timezone.now()
    run = (
        IrrigationRun.objects.filter(valve=valve, status=IrrigationRun.STATUS_RUNNING)
        .order_by("-actual_start_at")
        .first()
    )

    try:
        services.close_valve(valve)
    except Exception as exc:  # noqa: BLE001 - surface hardware errors to user
        if run:
            run.status = IrrigationRun.STATUS_FAILED
            run.stop_reason = IrrigationRun.STOP_ERROR
            run.error_message = str(exc)
            run.save(update_fields=["status", "stop_reason", "error_message"])
        messages.error(request, f"Failed to close valve: {exc}")
        return redirect("dashboard")

    if run:
        run.status = IrrigationRun.STATUS_FINISHED
        run.stop_reason = IrrigationRun.STOP_MANUAL
        run.actual_stop_at = now
        run.save(update_fields=["status", "stop_reason", "actual_stop_at"])
    messages.success(request, "Valve closed.")
    return redirect("dashboard")


@login_required
def schedule_view(request: HttpRequest) -> HttpResponse:
    rule_list = list(
        ScheduleRule.objects.filter(enabled=True)
        .only("start_time", "max_duration_seconds")
        .order_by("start_time")
    )
    slot_min_time = None
    slot_max_time = None

    if rule_list:
        min_start_seconds = min(
            _time_to_seconds(rule.start_time) for rule in rule_list
        )
        max_end_seconds = max(
            _time_to_seconds(rule.start_time) + rule.max_duration_seconds
            for rule in rule_list
        )
        slot_min_time = _seconds_to_time_str(_floor_to_hour(min_start_seconds))
        slot_max_time = _seconds_to_time_str(_ceil_to_hour(max_end_seconds))

    return render(
        request,
        "irrigation/schedule.html",
        {
            "slot_min_time": slot_min_time,
            "slot_max_time": slot_max_time,
        },
    )


@login_required
def logs_view(request: HttpRequest) -> HttpResponse:
    runs = list(
        IrrigationRun.objects.select_related("valve")
        .order_by("-id")[:200]
    )
    for run in runs:
        run.duration_minutes_display = "-"
        if run.actual_start_at and run.actual_stop_at:
            start = timezone.localtime(run.actual_start_at)
            stop = timezone.localtime(run.actual_stop_at)
            minutes = round((stop - start).total_seconds() / 60.0, 1)
            run.duration_minutes_display = f"{minutes:g}"
    return render(request, "irrigation/logs.html", {"runs": runs})


@login_required
def schedule_create(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        form = ScheduleRuleForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Schedule rule created.")
            return redirect("schedule")
    else:
        form = ScheduleRuleForm()
    return render(request, "irrigation/schedule_form.html", {"form": form})


@login_required
def schedule_edit(request: HttpRequest, rule_id: int) -> HttpResponse:
    rule = get_object_or_404(ScheduleRule, pk=rule_id)
    if request.method == "POST":
        form = ScheduleRuleForm(request.POST, instance=rule)
        if form.is_valid():
            form.save()
            messages.success(request, "Schedule rule updated.")
            return redirect("schedule")
    else:
        form = ScheduleRuleForm(instance=rule)
    return render(
        request,
        "irrigation/schedule_form.html",
        {"form": form, "rule": rule},
    )


@login_required
@require_POST
def schedule_delete(request: HttpRequest, rule_id: int) -> HttpResponse:
    rule = get_object_or_404(ScheduleRule, pk=rule_id)
    rule.delete()
    messages.success(request, "Schedule rule deleted.")
    return redirect("schedule")


def _parse_iso_datetime(raw: str | None) -> dt.datetime | None:
    if not raw:
        return None
    value = raw.replace("Z", "+00:00")
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError:
        return None


def _time_to_seconds(value: dt.time) -> int:
    return value.hour * 3600 + value.minute * 60 + value.second


def _floor_to_hour(seconds: int) -> int:
    return (seconds // 3600) * 3600


def _ceil_to_hour(seconds: int) -> int:
    return ((seconds + 3599) // 3600) * 3600


def _seconds_to_time_str(seconds: int) -> str:
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _rule_title(rule: ScheduleRule) -> str:
    if rule.mode == ScheduleRule.MODE_FIXED:
        minutes = int((rule.fixed_duration_seconds or 0) / 60)
        return f"{rule.valve.name} (Fixed {minutes}m)"
    minutes = int(rule.max_duration_seconds / 60)
    return f"{rule.valve.name} (Dynamic max {minutes}m)"


@login_required
@require_GET
def calendar_events(request: HttpRequest) -> JsonResponse:
    start = _parse_iso_datetime(request.GET.get("start"))
    end = _parse_iso_datetime(request.GET.get("end"))
    if not start or not end:
        return JsonResponse({"error": "Invalid date range."}, status=400)

    rules = ScheduleRule.objects.filter(enabled=True).select_related(
        "valve",
        "valve__relay_device",
        "valve__relay_device__site",
    )
    events: list[dict] = []

    current_date = start.date()
    end_date = end.date()

    while current_date < end_date:
        for rule in rules:
            site = rule.valve.relay_device.site
            tz_name = site.timezone or settings.TIME_ZONE
            tz = ZoneInfo(tz_name)
            weekday = current_date.weekday()
            if not rule.uses_weekday(weekday):
                continue
            start_dt = dt.datetime.combine(current_date, rule.start_time).replace(tzinfo=tz)
            end_dt = start_dt + dt.timedelta(seconds=rule.max_duration_seconds)
            events.append(
                {
                    "title": _rule_title(rule),
                    "start": start_dt.isoformat(),
                    "end": end_dt.isoformat(),
                    "edit_url": reverse("schedule_edit", args=[rule.id]),
                }
            )
        current_date += dt.timedelta(days=1)

    return JsonResponse(events, safe=False)


@login_required
@require_GET
def chart_data(request: HttpRequest) -> JsonResponse:
    valve_id = request.GET.get("valve_id")
    if not valve_id:
        return JsonResponse({"error": "valve_id is required"}, status=400)

    valve = get_object_or_404(Valve, pk=valve_id)
    tz = ZoneInfo(valve.relay_device.site.timezone or settings.TIME_ZONE)

    runs = (
        IrrigationRun.objects.filter(
            valve=valve,
            status=IrrigationRun.STATUS_FINISHED,
            actual_start_at__isnull=False,
            actual_stop_at__isnull=False,
        )
        .order_by("actual_start_at")
        .only("actual_start_at", "actual_stop_at")
    )

    totals: dict[dt.date, float] = {}
    for run in runs:
        start = timezone.localtime(run.actual_start_at, tz)
        stop = timezone.localtime(run.actual_stop_at, tz)
        duration_minutes = max(0.0, (stop - start).total_seconds() / 60.0)
        day = start.date()
        totals[day] = totals.get(day, 0.0) + duration_minutes

    labels = [day.isoformat() for day in sorted(totals.keys())]
    data = [round(totals[day], 2) for day in sorted(totals.keys())]

    payload = {
        "labels": labels,
        "datasets": [
            {
                "label": valve.name,
                "data": data,
            }
        ],
    }
    return JsonResponse(payload)


@login_required
@require_GET
def valve_status(request: HttpRequest) -> JsonResponse:
    valves = Valve.objects.select_related("relay_device").order_by("name")
    running = {
        run.valve_id
        for run in IrrigationRun.objects.filter(status=IrrigationRun.STATUS_RUNNING)
    }
    payload = []
    for valve in valves:
        payload.append(
            {
                "id": valve.id,
                "name": valve.name,
                "is_open": valve.last_known_is_open,
                "last_polled_at": valve.last_polled_at.isoformat()
                if valve.last_polled_at
                else None,
                "is_running": valve.id in running,
            }
        )
    return JsonResponse(payload, safe=False)


@login_required
@require_POST
def trigger_run_now(request: HttpRequest, rule_id: int) -> HttpResponse:
    rule = get_object_or_404(ScheduleRule, pk=rule_id)
    now = timezone.now()
    optimal_duration = rule.fixed_duration_seconds
    if rule.mode == ScheduleRule.MODE_DYNAMIC:
        optimal_duration = random.randint(60, rule.max_duration_seconds)

    run = IrrigationRun.objects.create(
        valve=rule.valve,
        trigger=IrrigationRun.TRIGGER_MANUAL,
        requested_start_at=now,
        planned_start_at=None,
        actual_start_at=None,
        optimal_duration_seconds=optimal_duration,
        max_duration_seconds=rule.max_duration_seconds,
        status=IrrigationRun.STATUS_PLANNED,
    )

    try:
        services.open_valve(rule.valve)
    except Exception as exc:  # noqa: BLE001 - surface hardware errors to user
        run.status = IrrigationRun.STATUS_FAILED
        run.stop_reason = IrrigationRun.STOP_ERROR
        run.error_message = str(exc)
        run.save(update_fields=["status", "stop_reason", "error_message"])
        messages.error(request, f"Failed to start run: {exc}")
        return redirect("schedule")

    run.status = IrrigationRun.STATUS_RUNNING
    run.actual_start_at = now
    run.save(update_fields=["status", "actual_start_at"])
    messages.success(request, "Run started.")
    return redirect("schedule")
