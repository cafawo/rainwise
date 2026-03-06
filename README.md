# rainwise

Rainwise is a Django MVP for monitoring and scheduling an irrigation system backed by a Waveshare Modbus TCP Ethernet relay (8-channel). It prioritizes safety, low resource usage, and simple deployment on macOS (dev) and TrueNAS SCALE via Docker.

## Features (MVP)

- Dashboard with valve status and manual open/close.
- Weekly schedules with fixed or dynamic durations.
- Multiple schedules with an active schedule switch.
- Controller loop that enforces planned stops and hard failsafe stops.
- Weather import (Open-Meteo) stored as hourly observations.
- Charts for accumulated irrigation per valve/day (grouped bars) on the Dashboard.
- Dashboard chart overlays precipitation and temperature on separate axes.
- Logs page with recent irrigation runs.
- Curve page to visualize and tune the daily water requirement curve.

## Local Development

1. Create and activate a virtual environment.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Run migrations and create a superuser:

```bash
python manage.py migrate
python manage.py createsuperuser
```

4. Run the web app and controller in separate terminals:

```bash
python manage.py runserver
```

```bash
python manage.py controller
```

## Docker (SQLite default)

```bash
docker compose up --build
```

- `web` runs Gunicorn.
- `controller` runs `python manage.py controller`.
- SQLite data is stored under `./data` (mapped to `/data` inside containers). Set `SQLITE_PATH=/data/db.sqlite3` in `.env` to persist.

## Docker (External Postgres)

If you already have a Postgres server, set `POSTGRES_HOST`, `POSTGRES_DB`,
`POSTGRES_USER`, and `POSTGRES_PASSWORD` in `.env` and run:

```bash
docker compose up --build
```

Rainwise does not start or manage a Postgres container.

## Environment Variables

All variables are documented in `.env.example`. Key ones:

- `DJANGO_SECRET_KEY` (required in production)
- `DJANGO_DEBUG` (`true` / `false`)
- `DJANGO_ALLOWED_HOSTS` (comma-separated)
- `DJANGO_TIME_ZONE` (default `Europe/Berlin`)
- `SQLITE_PATH` (optional)
- `POSTGRES_HOST`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` (optional)
- `POSTGRES_PORT` (default `5432`)
- `POSTGRES_SSLMODE` (optional)

Database selection order:
1. If `POSTGRES_HOST` is set, Postgres is used (with required credentials).
2. Else if `SQLITE_PATH` is set, SQLite uses that path.
3. Else Django uses the default `db.sqlite3` in the project root.

Controller:

- `CONTROLLER_INTERVAL_SECONDS` (default `60`)
- `RELAY_POLL_INTERVAL_SECONDS` (default `60`)
- `WEATHER_REFRESH_HOURS` (default `6`)
- `WEATHER_LOOKBACK_DAYS` (default `2`)
- `WEATHER_RETRY_MINUTES` (default `60`)

Modbus:

- `MODBUS_DEFAULT_PORT` (default `502`)
- `MODBUS_DEFAULT_UNIT_ID` (default `1`)
- `MODBUS_TIMEOUT_SECONDS` (default `2.0`)
- `MODBUS_RETRIES` (default `1`)
- `RELAY_SIMULATOR` (`true` to run without hardware)

Superuser bootstrap (Docker entrypoint):

- `DJANGO_SUPERUSER_USERNAME`
- `DJANGO_SUPERUSER_PASSWORD`
- `DJANGO_SUPERUSER_EMAIL` (optional)

## Data Setup

On controller startup, if no `Site` exists, Rainwise will create one using:

- `DEFAULT_SITE_NAME` (fallback: `Home`)
- `DEFAULT_SITE_LAT` / `DEFAULT_SITE_LON` (optional)
- `DJANGO_TIME_ZONE`

Use the Django admin to create or edit:

1. `Site` with timezone and lat/lon.
2. `RelayDevice` with Modbus host/port/unit.
3. `Valve` entries mapped to relay channels.
4. `ScheduleRule` entries for weekly scheduling.

## Safety Notes

- All valve openings have a planned stop and a hard max stop.
- A watchdog closes valves that appear open unexpectedly.
- The controller is designed for 60s cadence (no busy loops).

## Hardware Access

Hardware I/O is isolated in `apps/irrigation/services.py` and is never performed in HTTP request/response code. Use `RELAY_SIMULATOR=true` for local/dev without hardware.
