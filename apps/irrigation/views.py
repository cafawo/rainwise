from __future__ import annotations

import datetime as dt
import random
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.db import transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.irrigation import services
from apps.irrigation.curves import (
    DEFAULT_G,
    DEFAULT_M,
    DEFAULT_MAX_MM,
    DEFAULT_MIN_MM,
    KNOWN_POINTS,
    daily_water_required,
    generate_curve_points,
    percentile,
)
from apps.irrigation.forms import (
    CurveForm,
    LoginForm,
    ScheduleLoadForm,
    ScheduleNewForm,
    ScheduleRuleForm,
)
from apps.irrigation.models import (
    CurveSettings,
    IrrigationRun,
    Schedule,
    ScheduleRule,
    Site,
    Valve,
)
from apps.weather.models import WeatherObservation
from apps.weather.services import ensure_recent_weather


def _get_active_site() -> Site | None:
    return Site.objects.order_by("id").first()


def _ensure_active_schedule(site: Site) -> Schedule:
    if site.active_schedule_id:
        return site.active_schedule
    schedule = Schedule.objects.filter(site=site).order_by("id").first()
    if schedule is None:
        schedule = Schedule.objects.create(site=site, name="Default")
    site.active_schedule = schedule
    site.save(update_fields=["active_schedule"])
    return schedule


def _hex_to_rgba(hex_color: str, alpha: float) -> str:
    hex_color = hex_color.lstrip("#")
    red = int(hex_color[0:2], 16)
    green = int(hex_color[2:4], 16)
    blue = int(hex_color[4:6], 16)
    return f"rgba({red},{green},{blue},{alpha})"


@login_required
def dashboard(request: HttpRequest) -> HttpResponse:
    valves = Valve.objects.select_related("relay_device").order_by("name")
    running_runs = (
        IrrigationRun.objects.filter(status=IrrigationRun.STATUS_RUNNING)
        .select_related("valve")
        .order_by("-actual_start_at")
    )
    running_valve_ids = [run.valve_id for run in running_runs]
    return render(
        request,
        "irrigation/dashboard.html",
        {
            "valves": valves,
            "running_valve_ids": running_valve_ids,
        },
    )


@login_required
def curve_view(request: HttpRequest) -> HttpResponse:
    default_params = {
        "min_mm": DEFAULT_MIN_MM,
        "max_mm": DEFAULT_MAX_MM,
        "g": DEFAULT_G,
        "m": DEFAULT_M,
    }
    site = _get_active_site()
    settings_obj = None
    if site:
        settings_obj, _ = CurveSettings.objects.get_or_create(
            site=site, defaults=default_params
        )
        stored_params = {
            "min_mm": settings_obj.min_mm,
            "max_mm": settings_obj.max_mm,
            "g": settings_obj.g,
            "m": settings_obj.m,
        }
    else:
        stored_params = default_params

    if request.method == "POST":
        if "reset_defaults" in request.POST:
            form = CurveForm(initial=default_params)
            user_params = default_params
            if settings_obj:
                settings_obj.min_mm = default_params["min_mm"]
                settings_obj.max_mm = default_params["max_mm"]
                settings_obj.g = default_params["g"]
                settings_obj.m = default_params["m"]
                settings_obj.save(
                    update_fields=["min_mm", "max_mm", "g", "m", "updated_at"]
                )
                messages.success(request, "Curve reset to defaults.")
            else:
                messages.error(request, "No site configured to store curve settings.")
        else:
            form = CurveForm(request.POST)
            if form.is_valid():
                user_params = form.cleaned_data
                if site:
                    CurveSettings.objects.update_or_create(
                        site=site,
                        defaults=user_params,
                    )
                    messages.success(request, "Curve saved.")
                else:
                    messages.error(request, "No site configured to store curve settings.")
            else:
                user_params = stored_params
    else:
        form = CurveForm(initial=stored_params)
        user_params = stored_params

    default_curve = generate_curve_points(
        0,
        40,
        1,
        min_mm=default_params["min_mm"],
        max_mm=default_params["max_mm"],
        g=default_params["g"],
        m=default_params["m"],
    )
    user_curve = generate_curve_points(
        0,
        40,
        1,
        min_mm=user_params["min_mm"],
        max_mm=user_params["max_mm"],
        g=user_params["g"],
        m=user_params["m"],
    )

    p90_point = None
    site = _get_active_site()
    if site:
        cutoff = timezone.now() - dt.timedelta(hours=24)
        temps = list(
            WeatherObservation.objects.filter(
                site=site,
                timestamp__gte=cutoff,
                temperature_c__isnull=False,
            ).values_list("temperature_c", flat=True)
        )
        p90_temp = percentile(temps, 0.9)
        if p90_temp is not None:
            p90_point = {
                "x": round(p90_temp, 2),
                "y": round(
                    daily_water_required(
                        p90_temp,
                        user_params["min_mm"],
                        user_params["max_mm"],
                        user_params["g"],
                        user_params["m"],
                    ),
                    3,
                ),
            }

    return render(
        request,
        "irrigation/curve.html",
        {
            "form": form,
            "known_points": KNOWN_POINTS,
            "default_curve": default_curve,
            "user_curve": user_curve,
            "p90_point": p90_point,
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
    site = _get_active_site()
    if not site:
        messages.warning(request, "Create a site in the admin to use schedules.")
        return redirect("dashboard")

    active_schedule = _ensure_active_schedule(site)
    schedules = Schedule.objects.filter(site=site).order_by("name")
    rule_list = list(
        ScheduleRule.objects.filter(schedule=active_schedule)
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
            "active_schedule": active_schedule,
            "schedules": schedules,
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
    site = _get_active_site()
    if not site:
        messages.warning(request, "Create a site in the admin to add schedule rules.")
        return redirect("dashboard")
    active_schedule = _ensure_active_schedule(site)

    if request.method == "POST":
        form = ScheduleRuleForm(request.POST, site=site)
        if form.is_valid():
            rule = form.save(commit=False)
            rule.schedule = active_schedule
            rule.save()
            messages.success(request, "Schedule rule created.")
            return redirect("schedule")
    else:
        form = ScheduleRuleForm(site=site)
    return render(request, "irrigation/schedule_form.html", {"form": form})


@login_required
def schedule_edit(request: HttpRequest, rule_id: int) -> HttpResponse:
    rule = get_object_or_404(ScheduleRule, pk=rule_id)
    site = rule.schedule.site
    if request.method == "POST":
        form = ScheduleRuleForm(request.POST, instance=rule, site=site)
        if form.is_valid():
            form.save()
            messages.success(request, "Schedule rule updated.")
            return redirect("schedule")
    else:
        form = ScheduleRuleForm(instance=rule, site=site)
    return render(
        request,
        "irrigation/schedule_form.html",
        {"form": form, "rule": rule},
    )


@login_required
def schedule_copy(request: HttpRequest, rule_id: int) -> HttpResponse:
    rule = get_object_or_404(ScheduleRule, pk=rule_id)
    site = rule.schedule.site
    if request.method == "POST":
        form = ScheduleRuleForm(request.POST, site=site)
        if form.is_valid():
            new_rule = form.save(commit=False)
            new_rule.schedule = rule.schedule
            new_rule.save()
            messages.success(request, "Schedule rule copied.")
            return redirect("schedule")
    else:
        selected_days = [
            str(idx)
            for idx in range(7)
            if rule.days_of_week_mask & (1 << idx)
        ]
        form = ScheduleRuleForm(
            site=site,
            initial={
                "valve": rule.valve_id,
                "enabled": rule.enabled,
                "days_of_week": selected_days,
                "start_time": rule.start_time,
                "mode": rule.mode,
                "max_duration_seconds": rule.max_duration_seconds,
                "note": rule.note,
            },
        )
    return render(request, "irrigation/schedule_form.html", {"form": form})


@login_required
@require_POST
def schedule_delete(request: HttpRequest, rule_id: int) -> HttpResponse:
    rule = get_object_or_404(ScheduleRule, pk=rule_id)
    rule.delete()
    messages.success(request, "Schedule rule deleted.")
    return redirect("schedule")


@login_required
def schedule_new(request: HttpRequest) -> HttpResponse:
    site = _get_active_site()
    if not site:
        messages.warning(request, "Create a site in the admin to add schedules.")
        return redirect("schedule")

    active_schedule = _ensure_active_schedule(site)
    schedules = Schedule.objects.filter(site=site).order_by("name")

    if request.method == "POST":
        form = ScheduleNewForm(request.POST, schedules=schedules)
        if form.is_valid():
            name = form.cleaned_data["name"]
            description = form.cleaned_data.get("description", "")
            copy_current = form.cleaned_data["copy_current"]

            with transaction.atomic():
                new_schedule = Schedule.objects.create(
                    site=site, name=name, description=description
                )
                if copy_current:
                    existing_rules = list(
                        ScheduleRule.objects.filter(schedule=active_schedule)
                        .select_related("valve")
                        .order_by("id")
                    )
                    cloned_rules = [
                        ScheduleRule(
                            schedule=new_schedule,
                            valve=rule.valve,
                            enabled=rule.enabled,
                            days_of_week_mask=rule.days_of_week_mask,
                            start_time=rule.start_time,
                            mode=rule.mode,
                            max_duration_seconds=rule.max_duration_seconds,
                            note=rule.note,
                        )
                        for rule in existing_rules
                    ]
                    if cloned_rules:
                        ScheduleRule.objects.bulk_create(cloned_rules)

                site.active_schedule = new_schedule
                site.save(update_fields=["active_schedule"])

            messages.success(request, "Schedule created.")
            return redirect("schedule")
    else:
        form = ScheduleNewForm(schedules=schedules)

    return render(
        request,
        "irrigation/schedule_new.html",
        {"form": form, "active_schedule": active_schedule},
    )


@login_required
def schedule_load(request: HttpRequest) -> HttpResponse:
    site = _get_active_site()
    if not site:
        messages.warning(request, "Create a site in the admin to load schedules.")
        return redirect("schedule")

    schedules = Schedule.objects.filter(site=site).order_by("name")
    if not schedules.exists():
        messages.warning(request, "Create a schedule before loading.")
        return redirect("schedule")

    if request.method == "POST":
        form = ScheduleLoadForm(request.POST, schedules=schedules)
        if form.is_valid():
            schedule = form.cleaned_data["schedule"]
            site.active_schedule = schedule
            site.save(update_fields=["active_schedule"])
            messages.success(request, f"Loaded schedule: {schedule.name}.")
            return redirect("schedule")
    else:
        form = ScheduleLoadForm(
            schedules=schedules, initial={"schedule": site.active_schedule_id}
        )

    return render(
        request,
        "irrigation/schedule_load.html",
        {"form": form, "active_schedule": site.active_schedule},
    )


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


class RainwiseLoginView(LoginView):
    authentication_form = LoginForm


def _rule_title(rule: ScheduleRule) -> str:
    if rule.mode == ScheduleRule.MODE_FIXED:
        minutes = int(rule.max_duration_seconds / 60)
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

    site = _get_active_site()
    if not site:
        return JsonResponse([], safe=False)
    active_schedule = _ensure_active_schedule(site)

    rules = ScheduleRule.objects.filter(schedule=active_schedule).select_related(
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
            event = {
                "title": _rule_title(rule),
                "start": start_dt.isoformat(),
                "end": end_dt.isoformat(),
                "edit_url": reverse("schedule_edit", args=[rule.id]),
            }
            if not rule.enabled:
                event.update(
                    {
                        "backgroundColor": "#e9ecef",
                        "borderColor": "#ced4da",
                        "textColor": "#6c757d",
                    }
                )
            events.append(event)
        current_date += dt.timedelta(days=1)

    return JsonResponse(events, safe=False)


@login_required
@require_GET
def chart_data(request: HttpRequest) -> JsonResponse:
    valve_id = request.GET.get("valve_id")
    valves = Valve.objects.select_related("relay_device", "relay_device__site").order_by(
        "name"
    )
    if valve_id:
        valves = valves.filter(pk=valve_id)
    valves_list = list(valves)
    if not valves_list:
        return JsonResponse({"labels": [], "datasets": []})

    site = _get_active_site() or valves_list[0].relay_device.site
    tz = ZoneInfo(site.timezone or settings.TIME_ZONE)

    runs = (
        IrrigationRun.objects.filter(
            valve__in=valves_list,
            status=IrrigationRun.STATUS_FINISHED,
            actual_start_at__isnull=False,
            actual_stop_at__isnull=False,
        )
        .order_by("actual_start_at")
        .only("valve_id", "actual_start_at", "actual_stop_at")
    )

    totals_by_valve: dict[int, dict[dt.date, float]] = {
        valve.id: {} for valve in valves_list
    }
    days_set: set[dt.date] = set()

    for run in runs:
        start = timezone.localtime(run.actual_start_at, tz)
        stop = timezone.localtime(run.actual_stop_at, tz)
        duration_minutes = max(0.0, (stop - start).total_seconds() / 60.0)
        day = start.date()
        days_set.add(day)
        valve_totals = totals_by_valve.setdefault(run.valve_id, {})
        valve_totals[day] = valve_totals.get(day, 0.0) + duration_minutes

    if days_set:
        days = sorted(days_set)
        min_day = days[0]
        max_day = days[-1]
    else:
        latest_obs = (
            WeatherObservation.objects.filter(site=site).order_by("-timestamp").first()
        )
        if latest_obs:
            max_day = timezone.localtime(latest_obs.timestamp, tz).date()
        else:
            max_day = timezone.localtime(timezone.now(), tz).date()
        min_day = max_day - dt.timedelta(days=6)
        days = [
            min_day + dt.timedelta(days=offset)
            for offset in range((max_day - min_day).days + 1)
        ]

    labels = [day.isoformat() for day in days]

    precip_by_day: dict[dt.date, float] = {}
    precip_count_by_day: dict[dt.date, int] = {}
    temp_sum_by_day: dict[dt.date, float] = {}
    temp_count_by_day: dict[dt.date, int] = {}

    start_dt = dt.datetime.combine(min_day, dt.time.min).replace(tzinfo=tz)
    end_dt = dt.datetime.combine(max_day + dt.timedelta(days=1), dt.time.min).replace(
        tzinfo=tz
    )
    observations = WeatherObservation.objects.filter(
        site=site, timestamp__gte=start_dt, timestamp__lt=end_dt
    ).only("timestamp", "temperature_c", "precipitation_mm")
    for obs in observations:
        day = timezone.localtime(obs.timestamp, tz).date()
        if obs.precipitation_mm is not None:
            precip_by_day[day] = precip_by_day.get(day, 0.0) + obs.precipitation_mm
            precip_count_by_day[day] = precip_count_by_day.get(day, 0) + 1
        if obs.temperature_c is not None:
            temp_sum_by_day[day] = temp_sum_by_day.get(day, 0.0) + obs.temperature_c
            temp_count_by_day[day] = temp_count_by_day.get(day, 0) + 1

    precip_series = [
        round(precip_by_day[day], 2) if day in precip_count_by_day else None
        for day in days
    ]
    temp_series = [
        round(temp_sum_by_day[day] / temp_count_by_day[day], 2)
        if day in temp_count_by_day
        else None
        for day in days
    ]

    bar_colors = [
        "#198754",
        "#0dcaf0",
        "#6f42c1",
        "#ffc107",
        "#dc3545",
        "#6610f2",
        "#20c997",
        "#6c757d",
    ]
    datasets: list[dict] = []
    for idx, valve in enumerate(valves_list):
        color = bar_colors[idx % len(bar_colors)]
        valve_data = [
            round(totals_by_valve.get(valve.id, {}).get(day, 0.0), 2) for day in days
        ]
        datasets.append(
            {
                "type": "bar",
                "label": f"{valve.name} (min)",
                "data": valve_data,
                "yAxisID": "y",
                "backgroundColor": _hex_to_rgba(color, 0.35),
                "borderColor": color,
                "borderWidth": 1,
                "order": 1,
            }
        )

    datasets.extend(
        [
            {
                "type": "line",
                "label": "Precip (mm)",
                "data": precip_series,
                "yAxisID": "y_precip",
                "borderColor": "#0d6efd",
                "backgroundColor": "rgba(13,110,253,0.15)",
                "tension": 0.2,
                "order": 2,
            },
            {
                "type": "line",
                "label": "Temp (°C)",
                "data": temp_series,
                "yAxisID": "y_temp",
                "borderColor": "#fd7e14",
                "backgroundColor": "rgba(253,126,20,0.15)",
                "tension": 0.2,
                "order": 2,
            },
        ]
    )

    return JsonResponse({"labels": labels, "datasets": datasets})


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
    optimal_duration = rule.max_duration_seconds
    if rule.mode == ScheduleRule.MODE_DYNAMIC:
        ensure_recent_weather(
            rule.valve.relay_device.site,
            now=now,
        )
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
