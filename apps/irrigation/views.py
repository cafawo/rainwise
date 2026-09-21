from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.core.exceptions import ValidationError
from django.db import models
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_POST

from apps.irrigation import balance, group_services
from apps.irrigation.curves import (
    DEFAULT_G,
    DEFAULT_M,
    DEFAULT_MAX_MM,
    DEFAULT_MIN_MM,
    KNOWN_POINTS,
    daily_water_required,
    generate_curve_points,
)
from apps.irrigation.forms import (
    CurveForm,
    LoginForm,
    ScheduleLoadForm,
    ScheduleNewForm,
    RuleEditorForm,
    ValveMemberFormSet,
    mask_from_days,
)
from apps.irrigation.models import (
    CurveSettings,
    GroupedRule,
    GroupedRuleValve,
    RuleOccurrence,
    normalize_rule_mode,
    IrrigationRun,
    Schedule,
    ScheduleRule,
    Site,
    Valve,
)
from apps.irrigation.site_context import store_active_site
from apps.weather.models import WeatherObservation


def _get_active_site(request: HttpRequest) -> Site | None:
    return getattr(request, "active_site", None)


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


def _using_default_sqlite() -> bool:
    postgres_host = (getattr(settings, "POSTGRES_HOST", "") or "").strip()
    sqlite_path = (getattr(settings, "SQLITE_PATH", "") or "").strip()
    return not postgres_host and not sqlite_path


def _smart_status(site):
    if not site:
        return {}
    now = timezone.now()
    temperature = balance.temperature_selection(site, now)
    temperature["latest_valid_time"] = (
        dt.datetime.fromisoformat(temperature["latest_valid_at"])
        if temperature["latest_valid_at"] else None
    )
    warnings = []
    if site.active_schedule_id:
        for rule in GroupedRule.objects.filter(
            schedule_id=site.active_schedule_id, mode="SMART", enabled=True
        ).prefetch_related("members__valve__relay_device"):
            try:
                decision = balance.build_smart_decision(site, list(rule.members.all()), now)
                warnings.extend(
                    warning for warning in decision["warnings"]
                    if not warning.startswith("Operating with fallback")
                )
            except (ValidationError, ValueError) as exc:
                warnings.append(str(exc))
    return {"temperature": temperature, "quality_warnings": list(dict.fromkeys(warnings))}


def _occurrence_cards(site, limit=30):
    if not site:
        return []
    now = timezone.now()
    occurrences = list(
        RuleOccurrence.objects.filter(site=site).select_related("rule")
        .prefetch_related("runs").order_by("-requested_at", "-pk")[:limit]
    )
    for occurrence in occurrences:
        runs = list(occurrence.runs.all())
        occurrence.progress_total = len(runs)
        occurrence.progress_finished = sum(run.status == "FINISHED" for run in runs)
        occurrence.member_names = " → ".join(
            member.get("name", "")
            for member in occurrence.config.get("members", [])
        )
        occurrence.decision_rows = []
        for saved_row in occurrence.decision.get("valves", {}).values():
            row = saved_row.copy()
            estimates = [balance.delivery_estimate(run, cutoff=now) for run in runs
                         if run.valve_id == row["valve_id"]]
            row["execution_estimated_mm"] = sum(
                estimate["estimated_mm"] or 0 for estimate in estimates
            )
            row["execution_unmet_mm"] = max(
                0, row["target_mm"] - row["execution_estimated_mm"]
            )
            row["execution_uncertain"] = any(estimate["uncertain"] for estimate in estimates)
            occurrence.decision_rows.append(row)
    return occurrences


@login_required
def dashboard(request: HttpRequest) -> HttpResponse:
    site = _get_active_site(request)
    valves = (
        Valve.objects.filter(relay_device__site=site)
        .select_related("relay_device")
        .order_by("name")
        if site
        else Valve.objects.none()
    )
    running_runs = (
        IrrigationRun.objects.filter(
            status=IrrigationRun.STATUS_RUNNING,
            valve__relay_device__site=site,
        )
        .select_related("valve")
        .order_by("-actual_start_at")
        if site
        else IrrigationRun.objects.none()
    )
    running_valve_ids = [run.valve_id for run in running_runs]
    return render(
        request,
        "irrigation/dashboard.html",
        {
            "valves": valves,
            "running_valve_ids": running_valve_ids,
            "show_default_sqlite_warning": _using_default_sqlite(),
            "occurrences": _occurrence_cards(site, limit=10),
            **_smart_status(site),
        },
    )


@login_required
def curve_view(request: HttpRequest) -> HttpResponse:
    default_params = {
        "min_mm": DEFAULT_MIN_MM, "max_mm": DEFAULT_MAX_MM,
        "g": DEFAULT_G, "m": DEFAULT_M,
    }
    site = _get_active_site(request)
    settings_obj = CurveSettings.objects.filter(site=site).first() if site else None
    stored_params = {
        **default_params, "coverage_days": 2, "fallback_temperature_c": None,
    }
    if settings_obj:
        stored_params.update({key: getattr(settings_obj, key) for key in stored_params})
    user_params = stored_params.copy()
    if request.method == "POST":
        data = request.POST.copy()
        if "reset_defaults" in data:
            data.update({**stored_params, **default_params})
        elif "coverage_days" not in data:
            data["coverage_days"] = stored_params["coverage_days"]
        form = CurveForm(data)
        if form.is_valid():
            if not site:
                form.add_error(None, "No site configured to store curve settings.")
            else:
                try:
                    with group_services.site_admission(site):
                        candidate, _ = CurveSettings.objects.update_or_create(
                            site=site, defaults=form.cleaned_data
                        )
                        candidate.full_clean()
                        for schedule in Schedule.objects.filter(site=site):
                            group_services.validate_schedule(schedule)
                    user_params = form.cleaned_data
                    messages.success(request, "Curve saved.")
                except (ValidationError, RuntimeError) as exc:
                    form.add_error(None, str(exc))
    else:
        form = CurveForm(initial=stored_params)

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

    status = _smart_status(site)
    selected = status.get("temperature", {}).get("temperature_c")
    p90_point = None
    if selected is not None:
        p90_point = {"x": round(selected, 2), "y": round(daily_water_required(
            selected, user_params["min_mm"], user_params["max_mm"],
            user_params["g"], user_params["m"],
        ), 3)}
    capacity_rows = []
    if site:
        for member in GroupedRuleValve.objects.filter(
            rule__schedule=site.active_schedule, rule__mode="SMART"
        ).select_related("valve", "rule"):
            rate = member.valve.application_rate_mm_h
            capacity = 2 * member.duration_seconds * rate / 3600 if rate else None
            capacity_rows.append({
                "valve": member.valve, "cap": member.duration_seconds, "capacity": capacity,
                "cannot_sustain": capacity is not None and capacity < user_params["max_mm"],
                "cannot_cover": (
                    capacity is not None and capacity
                    < user_params["coverage_days"] * user_params["max_mm"]
                ),
            })

    return render(
        request,
        "irrigation/curve.html",
        {
            "form": form,
            "known_points": KNOWN_POINTS,
            "default_curve": default_curve,
            "user_curve": user_curve,
            "p90_point": p90_point,
            "capacity_rows": capacity_rows,
            **status,
        },
    )


@login_required
@require_POST
def open_valve_view(request: HttpRequest, valve_id: int) -> HttpResponse:
    site = _get_active_site(request)
    valve = get_object_or_404(Valve, pk=valve_id, relay_device__site=site)
    try:
        group_services.start_single(
            valve, valve.default_max_duration_seconds, IrrigationRun.TRIGGER_MANUAL
        )
        messages.success(request, "Valve opened.")
    except Exception as exc:
        messages.error(request, f"Failed to open valve: {exc}")
    return redirect("dashboard")


@login_required
@require_POST
def close_valve_view(request: HttpRequest, valve_id: int) -> HttpResponse:
    site = _get_active_site(request)
    valve = get_object_or_404(Valve, pk=valve_id, relay_device__site=site)
    try:
        group_services.close_member(valve)
        messages.success(request, "Closure requested. Any active rule is stopping.")
    except Exception as exc:
        messages.error(request, f"Failed to close valve: {exc}")
    return redirect("dashboard")


@login_required
def schedule_view(request: HttpRequest) -> HttpResponse:
    site = _get_active_site(request)
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
    group_list = list(
        GroupedRule.objects.filter(schedule=active_schedule)
        .prefetch_related("members__valve")
    )
    rule_list.extend(group_list)
    slot_min_time = None
    slot_max_time = None

    if rule_list:
        min_start_seconds = min(
            _time_to_seconds(rule.start_time) for rule in rule_list
        )
        max_end_seconds = max(
            _time_to_seconds(rule.start_time) + (sum(group_services.reservation_seconds(rule))
                                                   if isinstance(rule, GroupedRule)
                                                   else rule.max_duration_seconds)
            for rule in rule_list
        )
        slot_min_time = _seconds_to_time_str(_floor_to_hour(min_start_seconds))
        slot_max_time = _seconds_to_time_str(_ceil_to_hour(max_end_seconds))

    return render(
        request,
        "irrigation/schedule.html",
        {
            "active_schedule": active_schedule,
            "calendar_timezone": site.timezone or settings.TIME_ZONE,
            "schedules": schedules,
            "slot_min_time": slot_min_time,
            "slot_max_time": slot_max_time,
            "rule_cards": [_rule_card(rule) for rule in rule_list],
        },
    )


@login_required
def logs_view(request: HttpRequest) -> HttpResponse:
    site = _get_active_site(request)
    runs = list(
        IrrigationRun.objects.filter(valve__relay_device__site=site)
        .select_related("valve")
        .order_by("-id")[:200]
        if site
        else []
    )
    for run in runs:
        run.delivery = balance.delivery_estimate(run)
        run.duration_minutes_display = "-"
        if run.actual_start_at and run.actual_stop_at:
            start = timezone.localtime(run.actual_start_at)
            stop = timezone.localtime(run.actual_stop_at)
            minutes = round((stop - start).total_seconds() / 60.0, 1)
            run.duration_minutes_display = f"{minutes:g}"
    return render(request, "irrigation/logs.html", {
        "runs": runs, "occurrences": _occurrence_cards(site),
    })


def _editor_data(request):
    """Accept the old single-valve POST fields through the shared editor."""
    if request.method != "POST":
        return None
    data = request.POST.copy()
    if "members-TOTAL_FORMS" not in data and "valve" in data:
        data.update({
            "members-TOTAL_FORMS": "1", "members-INITIAL_FORMS": "1",
            "members-MIN_NUM_FORMS": "1", "members-MAX_NUM_FORMS": "1000",
            "members-0-valve": data.get("valve"),
            "members-0-duration_seconds": data.get("max_duration_seconds"),
            "members-0-ORDER": "1",
        })
    return data


def _rule_urls(rule):
    prefix = "group" if isinstance(rule, GroupedRule) else "schedule"
    return {action + "_url": reverse(prefix + "_" + action, args=[rule.pk])
            for action in ("edit", "copy", "delete", "run", "stop", "preview")
            if prefix == "group" or action not in {"stop", "preview"}}


def _edit_rule(request, rule=None, copying=False):
    site = _get_active_site(request)
    if not site:
        messages.warning(request, "Create a site before editing rules.")
        return redirect("dashboard")
    schedule = rule.schedule if rule else _ensure_active_schedule(site)
    is_group = isinstance(rule, GroupedRule)
    initial = {"mode": "FIXED", "enabled": True}
    members_initial = []
    if rule:
        initial.update({
            "mode": normalize_rule_mode(rule.mode), "enabled": rule.enabled,
            "days_of_week": [str(i) for i in range(7) if rule.uses_weekday(i)],
            "start_time": rule.start_time, "note": rule.note,
        })
        members_initial = [
            {"valve": member.valve_id, "duration_seconds": member.duration_seconds,
             "ORDER": member.order}
            for member in rule.members.order_by("order")
        ] if is_group else [{
            "valve": rule.valve_id, "duration_seconds": rule.max_duration_seconds,
            "ORDER": 1,
        }]
    data = _editor_data(request)
    form = RuleEditorForm(data, initial=initial)
    members_formset = ValveMemberFormSet(
        data, prefix="members", initial=members_initial if data is None else None,
        form_kwargs={"site": site}
    )
    if request.method == "POST":
        valid_form = form.is_valid()
        valid_members = members_formset.is_valid()
        if valid_form and valid_members:
            values = form.cleaned_data
            selected = [row.cleaned_data for row in members_formset.ordered_forms]
            grouped = is_group or values["mode"] == "SMART" or len(selected) > 1
            attributes = {
                "schedule": schedule, "mode": values["mode"],
                "enabled": values["enabled"], "note": values["note"],
                "days_of_week_mask": mask_from_days(values["days_of_week"]),
                "start_time": values["start_time"],
            }
            try:
                with group_services.site_admission(site):
                    existing = None if copying else rule
                    if grouped:
                        candidate = (
                            GroupedRule.objects.get(pk=existing.pk)
                            if is_group and existing else GroupedRule()
                        )
                        for key, value in attributes.items():
                            setattr(candidate, key, value)
                        members = [GroupedRuleValve(
                            rule=candidate, valve=row["valve"], order=index,
                            duration_seconds=row["duration_seconds"],
                        ) for index, row in enumerate(selected, 1)]
                        if existing:
                            old_members = ([(m.valve_id, m.order) for m in
                                            existing.members.order_by("order")]
                                           if is_group else [(existing.valve_id, 1)])
                            new_members = [(m.valve_id, m.order) for m in members]
                            if (not is_group or old_members != new_members or
                                    normalize_rule_mode(existing.mode) != candidate.mode):
                                group_services.assert_configuration_editable(existing)
                        group_services.validate_configuration(
                            candidate, members=members, exclude_rule=existing
                        )
                        if existing and is_group and not candidate.enabled:
                            group_services.cancel_rule(existing, "Rule disabled")
                        candidate.save()
                        if existing and is_group:
                            candidate.members.all().delete()
                        for member in members:
                            member.rule = candidate
                        GroupedRuleValve.objects.bulk_create(members)
                        if existing and not is_group:
                            existing.delete()
                    else:
                        candidate = (
                            ScheduleRule.objects.get(pk=existing.pk)
                            if existing else ScheduleRule()
                        )
                        attributes.update(
                            valve=selected[0]["valve"],
                            max_duration_seconds=selected[0]["duration_seconds"],
                        )
                        for key, value in attributes.items():
                            setattr(candidate, key, value)
                        candidate.full_clean()
                        group_services.validate_configuration(candidate, exclude_rule=existing)
                        candidate.save()
                messages.success(request, "Schedule rule saved.")
                if existing and not is_group and grouped:
                    return redirect("group_edit", rule_id=candidate.pk)
                return redirect("schedule")
            except (ValidationError, RuntimeError) as exc:
                error = (
                    "; ".join(exc.messages)
                    if isinstance(exc, ValidationError) else str(exc)
                )
                form.add_error(None, error)
    context = {"form": form, "members_formset": members_formset,
               "rule": None if copying else rule, "is_group": is_group,
               "editing_existing": bool(rule and not copying)}
    if rule and not copying:
        context.update(_rule_urls(rule))
        context["smart"] = normalize_rule_mode(rule.mode) == "SMART"
    return render(request, "irrigation/schedule_form.html", context)


@login_required
def schedule_create(request):
    return _edit_rule(request)


@login_required
def schedule_edit(request, rule_id):
    rule = get_object_or_404(ScheduleRule, pk=rule_id, schedule__site=_get_active_site(request))
    return _edit_rule(request, rule)


@login_required
def schedule_copy(request, rule_id):
    rule = get_object_or_404(ScheduleRule, pk=rule_id, schedule__site=_get_active_site(request))
    return _edit_rule(request, rule, copying=True)


@login_required
def group_edit(request, rule_id):
    rule = get_object_or_404(GroupedRule, pk=rule_id, schedule__site=_get_active_site(request))
    return _edit_rule(request, rule)


@login_required
def group_copy(request, rule_id):
    rule = get_object_or_404(GroupedRule, pk=rule_id, schedule__site=_get_active_site(request))
    return _edit_rule(request, rule, copying=True)


@login_required
@require_POST
def schedule_delete(request, rule_id):
    rule = get_object_or_404(ScheduleRule, pk=rule_id, schedule__site=_get_active_site(request))
    with group_services.site_admission(rule.schedule.site):
        rule.delete()
    messages.success(request, "Schedule rule deleted.")
    return redirect("schedule")


@login_required
@require_POST
def group_delete(request, rule_id):
    rule = get_object_or_404(GroupedRule, pk=rule_id, schedule__site=_get_active_site(request))
    with group_services.site_admission(rule.schedule.site):
        group_services.cancel_rule(rule, "Rule deleted")
        rule.delete()
    messages.success(request, "Rule deleted. Any active watering is stopping.")
    return redirect("schedule")


@login_required
@require_POST
def group_stop(request, rule_id):
    rule = get_object_or_404(GroupedRule, pk=rule_id, schedule__site=_get_active_site(request))
    group_services.cancel_rule(rule, "Stopped by user")
    messages.success(request, "Stopping rule; waiting for confirmed closure.")
    return redirect("dashboard")


@login_required
@require_POST
def group_run(request, rule_id):
    rule = get_object_or_404(GroupedRule, pk=rule_id, schedule__site=_get_active_site(request))
    try:
        group_services.request_fixed_group(rule)
        messages.success(request, "Run requested. The controller will execute the sequence.")
    except (ValidationError, RuntimeError) as exc:
        messages.error(request, str(exc))
    return redirect("group_edit", rule_id=rule.pk)


@login_required
def group_preview(request, rule_id):
    rule = get_object_or_404(GroupedRule, pk=rule_id, schedule__site=_get_active_site(request))
    decision = None
    error = None
    try:
        group_services.validate_configuration(rule)
        if rule.mode != "SMART":
            raise ValidationError("Preview is available for Smart rules.")
        decision = balance.build_smart_decision(
            rule.schedule.site, list(rule.members.select_related("valve").order_by("order")),
            timezone.now(),
        )
    except (ValidationError, ValueError, RuntimeError) as exc:
        error = str(exc)
    watering, handover = group_services.reservation_seconds(rule)
    return render(request, "irrigation/preview.html", {
        "rule": rule, "decision": decision, "error": error,
        "decision_rows": list(decision["valves"].values()) if decision else [],
        "rain_cutoff": (
            dt.datetime.fromisoformat(decision["rain"]["cutoff_at"])
            if decision else None
        ),
        "latest_valid_weather": (
            dt.datetime.fromisoformat(decision["temperature"]["latest_valid_at"])
            if decision and decision["temperature"]["latest_valid_at"] else None
        ),
        "watering_seconds": watering, "handover_seconds": handover,
    })


@login_required
def schedule_new(request: HttpRequest) -> HttpResponse:
    site = _get_active_site(request)
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

            try:
                with group_services.site_admission(site):
                    new_schedule = Schedule.objects.create(
                        site=site, name=name, description=description
                    )
                    if copy_current:
                        for source in active_schedule.rules.all():
                            mode = normalize_rule_mode(source.mode)
                            if mode != "FIXED":
                                raise ValidationError("Unsupported single-valve rule mode.")
                            source.pk = None
                            source.schedule = new_schedule
                            source.mode = mode
                            source.save()
                        for source in GroupedRule.objects.filter(schedule=active_schedule):
                            members = list(source.members.order_by("order"))
                            source.pk = None
                            source.schedule = new_schedule
                            source.save()
                            for member in members:
                                member.pk = None
                                member.rule = source
                                member.save()
                    group_services.validate_schedule(new_schedule)
                    group_services.cancel_site_groups(site, "Active schedule changed")
                    site.active_schedule = new_schedule
                    site.save(update_fields=["active_schedule"])
                messages.success(request, "Schedule created.")
                return redirect("schedule")
            except (ValidationError, RuntimeError) as exc:
                form.add_error(None, str(exc))
    else:
        form = ScheduleNewForm(schedules=schedules)

    return render(
        request,
        "irrigation/schedule_new.html",
        {"form": form, "active_schedule": active_schedule},
    )


@login_required
def schedule_load(request: HttpRequest) -> HttpResponse:
    site = _get_active_site(request)
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
            try:
                with group_services.site_admission(site):
                    group_services.validate_schedule(schedule)
                    if site.active_schedule_id != schedule.pk:
                        group_services.cancel_site_groups(site, "Active schedule changed")
                    site.active_schedule = schedule
                    site.save(update_fields=["active_schedule"])
                messages.success(request, f"Loaded schedule: {schedule.name}.")
                return redirect("schedule")
            except (ValidationError, RuntimeError) as exc:
                form.add_error(None, str(exc))
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


def _rule_card(rule):
    grouped = isinstance(rule, GroupedRule)
    watering, handover = (group_services.reservation_seconds(rule) if grouped
                         else (rule.max_duration_seconds, 0))
    return {"rule": rule, "mode": normalize_rule_mode(rule.mode).title(),
            "members": " → ".join(m.valve.name for m in rule.members.order_by("order"))
            if grouped else rule.valve.name,
            "watering_seconds": watering, "handover_seconds": handover,
            **_rule_urls(rule)}


@login_required
@require_GET
def calendar_events(request: HttpRequest) -> JsonResponse:
    start = _parse_iso_datetime(request.GET.get("start"))
    end = _parse_iso_datetime(request.GET.get("end"))
    if not start or not end:
        return JsonResponse({"error": "Invalid date range."}, status=400)

    site = _get_active_site(request)
    if not site:
        return JsonResponse([], safe=False)
    active_schedule = _ensure_active_schedule(site)

    rules = ScheduleRule.objects.filter(schedule=active_schedule).select_related(
        "valve",
        "valve__relay_device",
        "valve__relay_device__site",
    )
    rules = list(rules) + list(GroupedRule.objects.filter(
        schedule=active_schedule
    ).prefetch_related("members__valve"))
    events: list[dict] = []
    tz = ZoneInfo(site.timezone or settings.TIME_ZONE)
    current_date = start.date()
    while current_date < end.date():
        for rule in rules:
            if not rule.uses_weekday(current_date.weekday()):
                continue
            start_dt = dt.datetime.combine(current_date, rule.start_time, tzinfo=tz)
            # A gap has no corresponding local instant. A fold has one event.
            if (isinstance(rule, GroupedRule) and
                    start_dt.astimezone(dt.timezone.utc).astimezone(tz).replace(tzinfo=None)
                    != start_dt.replace(tzinfo=None)):
                continue
            card = _rule_card(rule)
            seconds = card["watering_seconds"] + card["handover_seconds"]
            grouped = isinstance(rule, GroupedRule)
            if grouped:
                end_dt = (
                    start_dt.astimezone(dt.timezone.utc)
                    + dt.timedelta(seconds=seconds)
                ).astimezone(tz)
            else:
                end_dt = start_dt + dt.timedelta(seconds=seconds)
            event = {
                "id": f"{'group' if grouped else 'single'}-{rule.pk}-{current_date}",
                "title": f"{card['mode']}: {card['members']}" if grouped else rule.valve.name,
                "start": start_dt.isoformat(), "end": end_dt.isoformat(),
                "edit_url": card["edit_url"], "mode": card["mode"],
                "members": card["members"],
                "watering_seconds": card["watering_seconds"],
                "handover_seconds": card["handover_seconds"],
            }
            if not rule.enabled:
                event.update({
                    "backgroundColor": "#e9ecef", "borderColor": "#ced4da",
                    "textColor": "#6c757d",
                })
            events.append(event)
        current_date += dt.timedelta(days=1)

    return JsonResponse(events, safe=False)


@login_required
@require_GET
def chart_data(request: HttpRequest) -> JsonResponse:
    site = _get_active_site(request)
    if not site:
        return JsonResponse({"labels": [], "datasets": []})
    valve_id = request.GET.get("valve_id")
    valves = Valve.objects.filter(relay_device__site=site).select_related(
        "relay_device", "relay_device__site"
    ).order_by("name")
    if valve_id:
        valves = valves.filter(pk=valve_id)
    valves_list = list(valves)
    if not valves_list:
        return JsonResponse({"labels": [], "datasets": []})

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

    weather_bounds = WeatherObservation.objects.filter(site=site).aggregate(
        earliest=models.Min("timestamp"),
        latest=models.Max("timestamp"),
    )
    weather_min = (
        timezone.localtime(weather_bounds["earliest"], tz).date()
        if weather_bounds["earliest"]
        else None
    )
    weather_max = (
        timezone.localtime(weather_bounds["latest"], tz).date()
        if weather_bounds["latest"]
        else None
    )

    if days_set or weather_min or weather_max:
        min_candidates = [day for day in [min(days_set) if days_set else None, weather_min] if day]
        max_candidates = [day for day in [max(days_set) if days_set else None, weather_max] if day]
        min_day = min(min_candidates) if min_candidates else None
        max_day = max(max_candidates) if max_candidates else None
        if min_day is None or max_day is None:
            max_day = timezone.localtime(timezone.now(), tz).date()
            min_day = max_day - dt.timedelta(days=6)
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
                "stack": "irrigation",
                "showInLegend": False,
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
                "showInLegend": True,
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
                "showInLegend": True,
                "order": 2,
            },
        ]
    )

    return JsonResponse({"labels": labels, "datasets": datasets})


@login_required
@require_GET
def valve_status(request: HttpRequest) -> JsonResponse:
    site = _get_active_site(request)
    valves = (
        Valve.objects.filter(relay_device__site=site)
        .select_related("relay_device")
        .order_by("name")
        if site
        else Valve.objects.none()
    )
    running = {
        run.valve_id
        for run in IrrigationRun.objects.filter(
            status=IrrigationRun.STATUS_RUNNING,
            valve__relay_device__site=site,
        )
    }
    payload = []
    for valve in valves:
        payload.append(
            {
                "id": valve.id,
                "name": valve.name,
                "is_open": valve.last_known_is_open,
                "last_polled_at": timezone.localtime(valve.last_polled_at).isoformat()
                if valve.last_polled_at
                else None,
                "is_running": valve.id in running,
            }
        )
    return JsonResponse(payload, safe=False)


@login_required
@require_POST
def trigger_run_now(request: HttpRequest, rule_id: int) -> HttpResponse:
    rule = get_object_or_404(
        ScheduleRule, pk=rule_id, schedule__site=_get_active_site(request)
    )
    try:
        if normalize_rule_mode(rule.mode) != ScheduleRule.MODE_FIXED:
            raise ValidationError("Unsupported rule mode; no valve was opened.")
        group_services.start_single(
            rule.valve, rule.max_duration_seconds, IrrigationRun.TRIGGER_MANUAL,
            rule=rule,
        )
        messages.success(request, "Run started.")
    except Exception as exc:
        messages.error(request, f"Failed to start run: {exc}")
    return redirect("schedule")


@login_required
@require_POST
def select_site(request: HttpRequest) -> HttpResponse:
    site = get_object_or_404(Site, pk=request.POST.get("site_id"))
    store_active_site(request, site)

    redirect_to = request.POST.get("next") or reverse("dashboard")
    if not url_has_allowed_host_and_scheme(
        redirect_to,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        redirect_to = reverse("dashboard")
    return redirect(redirect_to)
