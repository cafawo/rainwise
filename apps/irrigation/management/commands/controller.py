from __future__ import annotations

import datetime as dt
import logging
import os
import time
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.utils import timezone

from apps.irrigation import group_services, services
from apps.irrigation.models import (
    IrrigationRun,
    RelayDevice,
    Schedule,
    ScheduleRule,
    Site,
    Valve,
    normalize_rule_mode,
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


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("%s is not a float: %s", name, raw)
        return default


CONTROLLER_INTERVAL_SECONDS = _env_int("CONTROLLER_INTERVAL_SECONDS", 60)
RELAY_POLL_INTERVAL_SECONDS = _env_int("RELAY_POLL_INTERVAL_SECONDS", 60)
WEATHER_REFRESH_HOURS = _env_int("WEATHER_REFRESH_HOURS", 6)
WEATHER_LOOKBACK_DAYS = _env_int("WEATHER_LOOKBACK_DAYS", 30)
WEATHER_RETRY_MINUTES = _env_int("WEATHER_RETRY_MINUTES", 60)
DEFAULT_SITE_LAT = 50.1109
DEFAULT_SITE_LON = 8.6821


class Command(BaseCommand):
    help = "Run the irrigation controller loop."

    def handle(self, *args, **options) -> None:
        last_poll_at: dt.datetime | None = None
        self._ensure_default_site()
        self._ensure_default_schedules()
        self._recovery_pending = True
        try:
            group_services.recover_groups()
            self._recovery_pending = False
        except Exception:
            logger.exception("Startup recovery failed; group execution remains blocked")

        while True:
            loop_started = timezone.now()
            close_old_connections()

            try:
                if self._poll_due(loop_started, last_poll_at):
                    self._poll_relays(loop_started)
                    last_poll_at = loop_started
            except Exception:
                logger.exception("Relay polling failed")
            self._tick(loop_started)

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

            try:
                if normalize_rule_mode(rule.mode) != ScheduleRule.MODE_FIXED:
                    raise ValueError("Unsupported single-valve schedule mode")
                group_services.start_single(
                    rule.valve,
                    rule.max_duration_seconds,
                    IrrigationRun.TRIGGER_SCHEDULED,
                    planned_start_at=planned_start_at,
                    rule=rule,
                )
            except Exception as exc:
                logger.warning("Scheduled start skipped/failed for rule %s: %s", rule.pk, exc)

    def _tick(self, now: dt.datetime) -> None:
        # Planning/weather failures cannot prevent stops or watchdog work.
        # Groups run after the existing stops.
        try:
            self._start_due_runs(now)
        except Exception:
            logger.exception("Single-valve planning failed")
        recently_closed = set()
        try:
            recently_closed = self._stop_running_runs(now)
        except Exception:
            logger.exception("Stopping runs failed")
        try:
            self._watchdog_close(now, recently_closed)
        except Exception:
            logger.exception("Watchdog failed")
        try:
            if getattr(self, "_recovery_pending", False):
                group_services.recover_groups()
                self._recovery_pending = False
            group_services.group_tick()
        except Exception:
            logger.exception("Group planning/execution failed")
        try:
            self._refresh_weather(now)
        except Exception:
            logger.exception("Weather refresh failed")

    def _stop_running_runs(self, now: dt.datetime) -> set[int]:
        runs = IrrigationRun.objects.filter(status=IrrigationRun.STATUS_RUNNING)
        recently_closed: set[int] = set()
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

            if optimal_stop and optimal_stop == max_stop and now >= optimal_stop:
                if self._close_run(run, now, IrrigationRun.STOP_COMPLETED):
                    recently_closed.add(run.valve_id)
                continue
            if now >= max_stop:
                if self._close_run(run, now, IrrigationRun.STOP_FAILSAFE):
                    recently_closed.add(run.valve_id)
                continue
            if optimal_stop and now >= optimal_stop:
                if self._close_run(run, now, IrrigationRun.STOP_COMPLETED):
                    recently_closed.add(run.valve_id)
        return recently_closed

    def _close_run(
        self, run: IrrigationRun, now: dt.datetime, reason: str
    ) -> bool:
        error_message = ""
        try:
            services.close_valve(run.valve)
        except Exception as exc:  # noqa: BLE001 - capture hardware errors
            logger.warning(
                "Best-effort close failed after timed pulse for %s: %s",
                run.valve,
                exc,
            )
            error_message = f"Best-effort close failed after timed pulse: {exc}"

        run.status = IrrigationRun.STATUS_FINISHED
        run.stop_reason = reason
        run.actual_stop_at = now
        update_fields = ["status", "stop_reason", "actual_stop_at"]
        if run.attempt_started_at is None and run.actual_start_at is not None:
            # A pulse already running during upgrade has no new metadata. Keep
            # its recorded timing, but require fresh closure before group work.
            run.attempt_started_at = run.actual_start_at
            update_fields.append("attempt_started_at")
        if error_message:
            run.error_message = error_message
            update_fields.append("error_message")
        run.save(update_fields=update_fields)
        return True

    def _watchdog_close(self, now: dt.datetime, recently_closed: set[int]) -> None:
        running = {
            run.valve_id: run
            for run in IrrigationRun.objects.filter(status=IrrigationRun.STATUS_RUNNING)
        }
        open_valves = Valve.objects.filter(last_known_is_open=True)

        for valve in open_valves:
            if valve.id in recently_closed:
                continue
            run = running.get(valve.id)
            if run and run.actual_start_at:
                max_stop = run.actual_start_at + dt.timedelta(
                    seconds=run.max_duration_seconds
                )
                if now < max_stop:
                    continue

            opening = IrrigationRun.objects.filter(
                valve=valve, status="PLANNED", attempt_started_at__isnull=False,
                attempt_finished_at=None,
                dispatch_state__in=("UNSENT", "SENDING", "LEGACY"),
            ).first()
            if opening:
                age = (now - opening.attempt_started_at).total_seconds()
                allowance = (
                    group_services.command_allowance()
                    + group_services.controller_interval()
                )
                if not opening.cancellation_requested and 0 <= age <= allowance:
                    # The committed attempt explains the open relay even while
                    # the sender awaits acknowledgement. Do not fabricate an
                    # early watchdog closure and later claim full delivery.
                    continue
                IrrigationRun.objects.filter(pk=opening.pk).update(
                    cancellation_requested=True, delivery_uncertain=True,
                )

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
                try:
                    if opening:
                        group_services.mark_sender_interrupted([opening.pk])
                finally:
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

        Site.objects.create(
            name=os.environ.get("DEFAULT_SITE_NAME") or "Home",
            latitude=_env_float("DEFAULT_SITE_LAT", DEFAULT_SITE_LAT),
            longitude=_env_float("DEFAULT_SITE_LON", DEFAULT_SITE_LON),
            timezone=settings.TIME_ZONE,
        )

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
