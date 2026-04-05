# PLANS

Build an MVP Django webapp named **rainwise** to monitor and schedule an irrigation system controlled by a Waveshare Modbus TCP Ethernet relay module (8-channel). Runs locally on macOS for development and is deployable to TrueNAS SCALE via Docker. MVP prioritizes simplicity, safety, and low resource usage.

## Deployment prep (2026-03-06)

- Add a `.dockerignore` to keep images small and avoid copying dev data into images.
- Add a GitHub Actions workflow that builds and publishes GHCR images on tag pushes.
- Build multi-arch images (`amd64` + `arm64`) to run on TrueNAS and Apple Silicon.
- Also publish a `latest` tag on each release tag push.
- Add a TrueNAS YAML template (`docker-compose.truenas.yml`) with placeholders.
- Enforce database selection in Docker (must set Postgres or SQLite path).
- Show a dashboard warning when running on the default SQLite fallback.
- Update README with TrueNAS SCALE Apps deployment steps and GHCR usage.
- Keep local development workflow unchanged.

## Scope (MVP)

### Must-have
- Django webapp with username/password login.
- Dashboard:
  - list valves + last known status
  - manual override open/close
- Scheduling:
  - weekly rules per valve: day-of-week + start time
  - mode: FIXED (runs max duration) or DYNAMIC (random optimal duration, bounded by max)
  - calendar week view (Bootstrap + FullCalendar)
  - schedule by **max duration** (no parallel/group locking in MVP; user ensures no overlap by placing jobs after prior max windows)
- Logging:
  - store each irrigation run with timestamps and durations (planned/actual) and stop reason
- Safety:
  - failsafe max runtime per run/valve
  - watchdog closes valves that appear open unexpectedly or exceed max time
  - recovery behavior after restart
- Weather:
  - fetch historical hourly data (temperature, precipitation, humidity if available) via Open-Meteo
  - store in DB for later use
- Curve:
  - chart showing known points, default curve, and user-parameterized curve
  - allow adjusting curve parameters (min/max/g/m) and reset to defaults
  - show 90th percentile temperature (last 24h) mapped onto the curve when weather data exists
  - persist curve parameters per site
- Dashboard charts:
  - grouped bars for accumulated irrigation minutes per valve per day (based on IrrigationRun)

### Explicitly out of scope (for MVP)
- Group/parallel locking / resource constraints
- Advanced irrigation optimization (beyond random optimal duration simulation)
- Real-time websockets; low-frequency updates (30–60 seconds) are enough

---

## Technical approach

### Local dev (primary workflow)
- Run Django with `python manage.py runserver`
- Run controller in a second terminal: `python manage.py controller`

### Docker deployment (later)
- Provide Dockerfile + compose files early, but Docker is not required for local dev.
- Production topology (compose):
  - `web`: Django + gunicorn
  - `controller`: `python manage.py controller`
  - use external Postgres if `POSTGRES_HOST` is provided

No Redis/Celery for MVP.

### Resource constraints (non-negotiable)
- Default controller cadence: **30 seconds**
- Default relay polling cadence: **30 seconds**
- Avoid unnecessary DB writes; only write valve status if changed.
- Use short network timeouts to avoid hanging (Modbus + weather).

---

## Repository layout (rainwise)

Use an `apps/` folder for Django apps (future additions like smarthome integration).

- `rainwise/` (repo root)
  - `manage.py`
  - `rainwise/` (Django project package: settings/urls/wsgi/asgi)
  - `apps/`
    - `irrigation/`
    - `weather/`
  - `templates/` (base + auth + pages)
  - `static/` (minimal local static; use CDN for Bootstrap/FullCalendar/Chart.js)
  - `docker/` (entrypoint scripts)
  - `Dockerfile`
  - `docker-compose.yml`
  - `.env.example`
  - `README.md`

Django settings must include apps using dotted paths:
- `apps.irrigation`
- `apps.weather`

---

## Dependencies (requirements.txt)

Keep minimal:
- Django
- gunicorn (for Docker/prod)
- whitenoise (static in production)
- python-dotenv (dev convenience)
- psycopg[binary] (optional Postgres)
- requests (Open-Meteo)
- pyModbusTCP (Modbus TCP client)

Avoid heavy frontend build tooling.

---

## Environment variables

Document in `.env.example` and `README.md`.

### Django
- `DJANGO_SECRET_KEY` (required in production)
- `DJANGO_DEBUG` (`true`/`false`)
- `DJANGO_ALLOWED_HOSTS` (comma-separated)
- `DJANGO_TIME_ZONE` (default `Europe/Berlin`)

### Database selection (priority order)
1) If `POSTGRES_HOST` is set: use Postgres (requires `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`).
2) Else if `SQLITE_PATH` is set: use SQLite at that path.
3) Else: use Django default SQLite at `BASE_DIR / "db.sqlite3"`.

Notes:
- Local dev default is `db.sqlite3` in the repo (convenient).
- In Docker/TrueNAS, using default path may lose data unless the path is on a mounted volume. Recommend setting `SQLITE_PATH=/data/db.sqlite3` and mounting `/data`, or use Postgres.

### Postgres (external)
- `POSTGRES_HOST`
- `POSTGRES_PORT` (default `5432`)
- `POSTGRES_DB`
- `POSTGRES_USER`
- `POSTGRES_PASSWORD`
- `POSTGRES_SSLMODE` (optional)

### Superuser bootstrap (Docker convenience)
- `DJANGO_SUPERUSER_USERNAME`
- `DJANGO_SUPERUSER_PASSWORD`
- `DJANGO_SUPERUSER_EMAIL` (optional)

If username/password are provided, create/update the superuser on startup (Docker entrypoint). For local dev, user may create it manually.

### Controller (low resource defaults)
- `CONTROLLER_INTERVAL_SECONDS` (default **30**)
- `RELAY_POLL_INTERVAL_SECONDS` (default **30**)
- `WEATHER_REFRESH_HOURS` (default `6`)
- `WEATHER_LOOKBACK_DAYS` (default `30`)
- `WEATHER_RETRY_MINUTES` (default `60`)

### Site/Weather location
- `DEFAULT_SITE_NAME` (default `Home`)
- `DEFAULT_SITE_LAT`
- `DEFAULT_SITE_LON`

### Timezone policy
- Store all instant-based timestamps (`DateTimeField`) in UTC.
- Use the site timezone for client-facing display, calendar rendering, and local-date logic.
- Keep wall-clock schedule rules (`start_time`) as local site time.
- default generated site timezone `Europe/Berlin`

### Modbus
- `MODBUS_DEFAULT_PORT` (default `502`)
- `MODBUS_DEFAULT_UNIT_ID` (default `1`)
- `RELAY_SIMULATOR` (`true` to run without hardware)
- `MODBUS_TIMEOUT_SECONDS` (default `2.0`)
- `MODBUS_RETRIES` (default `1`)

---

## Data model (MVP)

### `Site`
- `name`
- `latitude`, `longitude`
- `timezone` (default Europe/Berlin)
- `active_schedule` (FK to current Schedule)

### `RelayDevice`
- `site` (FK)
- `name`
- `host` (IP/DNS)
- `port` (default 502)
- `unit_id` (default 1)
- `enabled`

### `Valve`
- `relay_device` (FK)
- `channel` (1..8)
- `name`, `description`
- `is_active_high` (default True; maps relay ON to “valve open”)
- `default_max_duration_seconds` (failsafe ceiling)
- status fields:
  - `last_known_is_open` (bool)
  - `last_polled_at` (datetime nullable)

Unique constraint: `(relay_device, channel)`.

### `Schedule`
- `site` (FK)
- `name`
- `description` (optional)
- `created_at`

### `ScheduleRule`
Weekly plan.
- `schedule` (FK)
- `valve` (FK)
- `enabled` (bool)
- `days_of_week_mask` (int bitmask)
- `start_time` (TimeField)
- `mode` (`FIXED` / `DYNAMIC`)
- `max_duration_seconds` (required; used for both modes)
- `note` (optional)

### `IrrigationRun`
Audit log + chart source.
- `valve` (FK)
- `trigger` (`SCHEDULED` / `MANUAL` / `FAILSAFE` / `RECOVERY`)
- `requested_start_at` (nullable)
- `planned_start_at` (nullable)  # scheduled minute
- `actual_start_at` (nullable)
- `optimal_duration_seconds` (nullable)
- `max_duration_seconds` (required)
- `actual_stop_at` (nullable)
- `status` (`PLANNED` / `RUNNING` / `FINISHED` / `FAILED`)
- `stop_reason` (`COMPLETED` / `MANUAL_STOP` / `FAILSAFE_TIMEOUT` / `ERROR`)
- `error_message` (text nullable)

Idempotency requirement:
- Never create two scheduled runs for the same `(valve, planned_start_at)`.

### Weather
`WeatherObservation`
- `site` (FK)
- `timestamp` (hourly)
- `temperature_c` (nullable)
- `precipitation_mm` (nullable)
- `humidity_percent` (nullable)
Unique constraint: `(site, timestamp)`.

`WeatherImportLog`
- `site` (FK)
- `date`
- `imported_at`
- `status` / `error_message` (optional)

---

## Service layer

Create `apps/irrigation/services.py`:

- `open_valve(valve: Valve) -> None`
- `close_valve(valve: Valve) -> None`
- `read_valve_state(valve: Valve) -> bool`
- `read_device_states(device: RelayDevice) -> list[bool]`

Implementation:
- Real mode uses `pyModbusTCP`.
- Simulator mode (`RELAY_SIMULATOR=true`) uses a DB-backed simulated coil state so UI/tests work without hardware.

Rules:
- Use conservative timeouts and minimal retries.
- Exceptions are caught and logged to IrrigationRun when relevant.

---

## Controller process (management command)

Implement `python manage.py controller` under `apps/irrigation/management/commands/controller.py`.

Loop every `CONTROLLER_INTERVAL_SECONDS` (default 30):
- Sleep (no busy wait).
- `django.db.close_old_connections()` each loop.

Steps each loop:

1) **Poll relay state** (every RELAY_POLL_INTERVAL_SECONDS; default 30)
- For valves due to poll:
  - read coil state (best-effort)
  - update `Valve.last_known_is_open` only if changed
  - set `last_polled_at`

2) **Start due scheduled runs**
- For each enabled ScheduleRule matching local day + time-of-day (minute precision):
  - planned_start_at = that scheduled minute (timezone-aware)
  - idempotency guard:
    - if an IrrigationRun exists for (valve, planned_start_at, trigger=SCHEDULED), skip
  - compute `optimal_duration_seconds`:
    - FIXED: max duration
    - DYNAMIC: refresh recent weather (best-effort) then random in `[min_seconds, max_duration_seconds]` (use min_seconds=60)
  - attempt open:
    - on success: status RUNNING + set `actual_start_at`
    - on failure: status FAILED + record error

3) **Stop runs**
For each RUNNING IrrigationRun:
- If `optimal_duration_seconds` is set and `now >= actual_start_at + optimal_duration_seconds`:
  - close valve
  - mark stop_reason COMPLETED
- If `now >= actual_start_at + max_duration_seconds`:
  - close valve
  - mark stop_reason FAILSAFE_TIMEOUT

Manual override rule (confirmed):
- Manual close ends any RUNNING run with stop_reason MANUAL_STOP.

4) **Watchdog (failsafe closure)**
If a valve appears open but:
- there is no RUNNING run, OR
- it exceeded max runtime
=> close valve and create a FAILSAFE/RECOVERY IrrigationRun entry (minimal but auditable).

5) **Weather import**
Regular refresh per site:
- fetch recent hourly values (lookback window) from Open-Meteo
- request weather timestamps in a UTC-safe format and normalize them before DB writes
- upsert observations

---

## Web UI

### Auth
- Use Django auth.
- Provide Bootstrap login template.
- Require login for app pages.
- Use Django messages for user feedback; render them with Bootstrap alerts (map error -> danger).

### Pages
1) `/` Dashboard
- list valves with:
  - last known state
  - last polled
  - “running now?” derived from RUNNING IrrigationRun
  - Open/Close POST actions

2) `/schedule/`
- Week calendar using FullCalendar (CDN)
- Events derived from ScheduleRule:
  - event length = max duration
  - for DYNAMIC rules, display an “expected/optimal” label (random/estimate)
- CRUD ScheduleRule via Bootstrap forms
- New/Load schedule:
  - New creates a schedule and can copy rules from the active schedule.
  - Load switches the active schedule for the site (no data deletion).

3) Charts (on Dashboard)
- Chart.js chart for accumulated irrigation minutes per valve per day (grouped bars across all valves)
- Overlay precipitation and temperature as lines on separate right-side axes
- Single combined chart (no valve selector)

4) `/logs/`
- Table view of irrigation runs (most recent first)
- Show valve, trigger, planned/start/stop times, duration, status, stop reason

5) `/curve/`
- Chart of daily water requirement vs temperature
- Show known reference points, default curve, and user-adjusted curve
- Form to adjust min/max/g/m and reset defaults

APIs (lightweight JSON):
- `/api/calendar-events/`
- `/api/chart-data/`
- `/api/valve-status/` (optional)

---

## Dockerization (provided early, used later)

### Dockerfile
- python slim base
- install requirements
- copy project
- entrypoint:
  - migrate
  - collectstatic
  - ensure superuser if env vars set
  - exec target command

### docker-compose.yml (SQLite default)
- `web`: gunicorn
- `controller`: `python manage.py controller`

For production with SQLite:
- recommend setting `SQLITE_PATH=/data/db.sqlite3` and mounting `/data`
For production with external Postgres:
- set `POSTGRES_HOST`, `POSTGRES_DB`, `POSTGRES_USER`, and `POSTGRES_PASSWORD`

---

## Acceptance checklist (MVP)

Local:
- `python manage.py runserver` works with default SQLite (`db.sqlite3`).
- `python manage.py controller` runs (30s loop) and is stable.
- Superuser creation documented; manual open/close works; runs logged.
- ScheduleRule CRUD works; calendar renders.
- Controller starts scheduled runs within the minute and stops them reliably.
- Failsafe closure works for max runtime.
- Weather import stores recent hourly data.
- Dashboard charts display accumulated irrigation by valve/day.
- Logs page shows recent irrigation runs.
- New/load schedule switches the active schedule and updates the calendar.

Docker-ready:
- Dockerfile builds.
- compose starts `web` and `controller`.
- README explains persistence and warns about SQLite in containers unless mounted or Postgres is used.
