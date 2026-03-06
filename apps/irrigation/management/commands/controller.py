from __future__ import annotations

import datetime as dt
import logging
import random
import os
import time
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.utils import timezone

from apps.irrigation import services
from apps.irrigation.models import (
    IrrigationRun,
    RelayDevice,
    Schedule,
    ScheduleRule,
    Site,
    Valve,
)
from apps.weather.services import ensure_recent_weather

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


CONTROLLER_INTERVAL_SECONDS = _env_int("CONTROLLER_INTERVAL_SECONDS", 60)
RELAY_POLL_INTERVAL_SECONDS = _env_int("RELAY_POLL_INTERVAL_SECONDS", 60)
WEATHER_REFRESH_HOURS = _env_int("WEATHER_REFRESH_HOURS", 6)
WEATHER_LOOKBACK_DAYS = _env_int("WEATHER_LOOKBACK_DAYS", 2)
WEATHER_RETRY_MINUTES = _env_int("WEATHER_RETRY_MINUTES", 60)


class Command(BaseCommand):
    help = "Run the irrigation controller loop."

    def handle(self, *args, **options) -> None:
        last_poll_at: dt.datetime | None = None
        self._ensure_default_site()
        self._ensure_default_schedules()

        while True:
            loop_started = timezone.now()
            close_old_connections()

            try:
                poll_due = self._poll_due(loop_started, last_poll_at)
                if poll_due:
                    self._poll_relays(loop_started)
                    last_poll_at = loop_started

                self._start_due_runs(loop_started)
                self._stop_running_runs(loop_started)
                self._watchdog_close(loop_started)
                self._refresh_weather(loop_started)
            except Exception:  # noqa: BLE001 - controller must keep running
                logger.exception("Controller loop failed")

            elapsed = (timezone.now() - loop_started).total_seconds()
            sleep_for = max(1, CONTROLLER_INTERVAL_SECONDS - int(elapsed))
            time.sleep(sleep_for)

    def _poll_due(
        self, now: dt.datetime, last_poll_at: dt.datetime | None
    ) -> bool:
        if last_poll_at is None:
            return True
        return (now - last_poll_at).total_seconds() >= RELAY_POLL_INTERVAL_SECONDS

    def _poll_relays(self, now: dt.datetime) -> None:
        devices = RelayDevice.objects.filter(enabled=True).select_related("site")
        for device in devices:
            try:
                raw_states = services.read_device_states(device)
            except Exception as exc:  # noqa: BLE001 - keep polling other devices
                logger.warning("Relay poll failed for %s: %s", device, exc)
                continue

            valves = Valve.objects.filter(relay_device=device)
            updates: list[Valve] = []
            for valve in valves:
                if not (1 <= valve.channel <= 8):
                    continue
                raw_value = raw_states[valve.channel - 1]
                is_open = raw_value == valve.is_active_high
                if valve.last_known_is_open != is_open or valve.last_polled_at is None:
                    valve.last_known_is_open = is_open
                    valve.last_polled_at = now
                    updates.append(valve)

            if updates:
                Valve.objects.bulk_update(
                    updates, ["last_known_is_open", "last_polled_at"]
                )

    def _start_due_runs(self, now: dt.datetime) -> None:
        active_schedule_ids = list(
            Site.objects.exclude(active_schedule__isnull=True).values_list(
                "active_schedule_id", flat=True
            )
        )
        if not active_schedule_ids:
            return

        rules = (
            ScheduleRule.objects.filter(
                enabled=True, schedule_id__in=active_schedule_ids
            )
            .select_related(
                "valve",
                "valve__relay_device",
                "valve__relay_device__site",
            )
        )

        refreshed_sites: set[int] = set()

        for rule in rules:
            site = rule.valve.relay_device.site
            tz_name = site.timezone or settings.TIME_ZONE
            tz = ZoneInfo(tz_name)
            local_now = timezone.localtime(now, tz)

            if not rule.uses_weekday(local_now.weekday()):
                continue

            if (
                local_now.hour != rule.start_time.hour
                or local_now.minute != rule.start_time.minute
            ):
                continue

            planned_start_at = local_now.replace(second=0, microsecond=0)

            if IrrigationRun.objects.filter(
                valve=rule.valve,
                planned_start_at=planned_start_at,
                trigger=IrrigationRun.TRIGGER_SCHEDULED,
            ).exists():
                continue

            if rule.mode == ScheduleRule.MODE_DYNAMIC:
                if site.id not in refreshed_sites:
                    ensure_recent_weather(
                        site,
                        now=now,
                        max_age_hours=WEATHER_REFRESH_HOURS,
                        lookback_days=WEATHER_LOOKBACK_DAYS,
                        min_retry_minutes=WEATHER_RETRY_MINUTES,
                    )
                    refreshed_sites.add(site.id)
                max_duration = max(60, rule.max_duration_seconds)
                optimal_duration = random.randint(60, max_duration)
            else:
                optimal_duration = rule.max_duration_seconds

            run = IrrigationRun.objects.create(
                valve=rule.valve,
                trigger=IrrigationRun.TRIGGER_SCHEDULED,
                requested_start_at=planned_start_at,
                planned_start_at=planned_start_at,
                actual_start_at=None,
                optimal_duration_seconds=optimal_duration,
                max_duration_seconds=rule.max_duration_seconds,
                status=IrrigationRun.STATUS_PLANNED,
            )

            try:
                services.open_valve(rule.valve)
            except Exception as exc:  # noqa: BLE001 - capture hardware errors
                run.status = IrrigationRun.STATUS_FAILED
                run.stop_reason = IrrigationRun.STOP_ERROR
                run.error_message = str(exc)
                run.save(update_fields=["status", "stop_reason", "error_message"])
                continue

            run.status = IrrigationRun.STATUS_RUNNING
            run.actual_start_at = now
            run.save(update_fields=["status", "actual_start_at"])

    def _stop_running_runs(self, now: dt.datetime) -> None:
        runs = IrrigationRun.objects.filter(status=IrrigationRun.STATUS_RUNNING)
        for run in runs.select_related("valve"):
            if not run.actual_start_at:
                continue

            max_stop = run.actual_start_at + dt.timedelta(
                seconds=run.max_duration_seconds
            )
            optimal_stop = None
            if run.optimal_duration_seconds:
                optimal_stop = run.actual_start_at + dt.timedelta(
                    seconds=run.optimal_duration_seconds
                )

            if now >= max_stop:
                self._close_run(run, now, IrrigationRun.STOP_FAILSAFE)
                continue
            if optimal_stop and now >= optimal_stop:
                self._close_run(run, now, IrrigationRun.STOP_COMPLETED)

    def _close_run(
        self, run: IrrigationRun, now: dt.datetime, reason: str
    ) -> None:
        try:
            services.close_valve(run.valve)
        except Exception as exc:  # noqa: BLE001 - capture hardware errors
            run.status = IrrigationRun.STATUS_FAILED
            run.stop_reason = IrrigationRun.STOP_ERROR
            run.error_message = str(exc)
            run.save(update_fields=["status", "stop_reason", "error_message"])
            return

        run.status = IrrigationRun.STATUS_FINISHED
        run.stop_reason = reason
        run.actual_stop_at = now
        run.save(update_fields=["status", "stop_reason", "actual_stop_at"])

    def _watchdog_close(self, now: dt.datetime) -> None:
        running = {
            run.valve_id: run
            for run in IrrigationRun.objects.filter(status=IrrigationRun.STATUS_RUNNING)
        }
        open_valves = Valve.objects.filter(last_known_is_open=True)

        for valve in open_valves:
            run = running.get(valve.id)
            if run and run.actual_start_at:
                max_stop = run.actual_start_at + dt.timedelta(
                    seconds=run.max_duration_seconds
                )
                if now < max_stop:
                    continue

            recent_failsafe = IrrigationRun.objects.filter(
                valve=valve,
                trigger__in=[
                    IrrigationRun.TRIGGER_FAILSAFE,
                    IrrigationRun.TRIGGER_RECOVERY,
                ],
                actual_start_at__gte=now - dt.timedelta(minutes=10),
            ).exists()
            if recent_failsafe:
                continue

            try:
                services.close_valve(valve)
                status = IrrigationRun.STATUS_FINISHED
                stop_reason = IrrigationRun.STOP_FAILSAFE
                error_message = ""
            except Exception as exc:  # noqa: BLE001 - capture hardware errors
                status = IrrigationRun.STATUS_FAILED
                stop_reason = IrrigationRun.STOP_ERROR
                error_message = str(exc)

            IrrigationRun.objects.create(
                valve=valve,
                trigger=IrrigationRun.TRIGGER_RECOVERY,
                requested_start_at=None,
                planned_start_at=None,
                actual_start_at=now,
                optimal_duration_seconds=None,
                max_duration_seconds=valve.default_max_duration_seconds,
                actual_stop_at=now,
                status=status,
                stop_reason=stop_reason,
                error_message=error_message,
            )

    def _refresh_weather(self, now: dt.datetime) -> None:
        for site in Site.objects.all():
            ensure_recent_weather(
                site,
                now=now,
                max_age_hours=WEATHER_REFRESH_HOURS,
                lookback_days=WEATHER_LOOKBACK_DAYS,
                min_retry_minutes=WEATHER_RETRY_MINUTES,
            )

    def _ensure_default_site(self) -> None:
        if Site.objects.exists():
            return

        name = os.environ.get("DEFAULT_SITE_NAME", "Home")
        lat = os.environ.get("DEFAULT_SITE_LAT")
        lon = os.environ.get("DEFAULT_SITE_LON")

        kwargs = {"name": name, "timezone": settings.TIME_ZONE}
        if lat:
            try:
                kwargs["latitude"] = float(lat)
            except ValueError:
                logger.warning("DEFAULT_SITE_LAT is not a float: %s", lat)
        if lon:
            try:
                kwargs["longitude"] = float(lon)
            except ValueError:
                logger.warning("DEFAULT_SITE_LON is not a float: %s", lon)

        Site.objects.create(**kwargs)

    def _ensure_default_schedules(self) -> None:
        for site in Site.objects.all():
            if site.active_schedule_id:
                continue
            schedule = (
                Schedule.objects.filter(site=site).order_by("id").first()
            )
            if schedule is None:
                schedule = Schedule.objects.create(site=site, name="Default")
            site.active_schedule = schedule
            site.save(update_fields=["active_schedule"])
