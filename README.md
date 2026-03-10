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
- Curve page to visualize and tune the daily water requirement curve (saved per site).

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

## Docker (Choose Postgres or SQLite)

For Docker deployments, you must configure a database:
- Postgres (recommended), or
- SQLite with a mounted `/data` volume.

The Docker entrypoint exits if neither `POSTGRES_HOST` nor `SQLITE_PATH` is set.

Postgres example:

```bash
# in .env
POSTGRES_HOST=your-postgres-host
POSTGRES_DB=rainwise
POSTGRES_USER=rainwise
POSTGRES_PASSWORD=change-me
POSTGRES_PORT=5432

docker compose up --build
```

SQLite example:

```bash
# in .env
SQLITE_PATH=/data/db.sqlite3

docker compose up --build
```

If using SQLite, add a volume mapping to `/data` in `docker-compose.yml`
or `docker-compose.truenas.yml` so the database is persisted.

Rainwise does not start or manage a Postgres container.

## GitHub Container Registry (GHCR)

Container images are built and published on tag pushes that match `v*`.

```bash
git tag v0.1.0
git push origin v0.1.0
```

Images publish to `ghcr.io/cafawo/rainwise:<tag>` and are multi-arch
(`linux/amd64` + `linux/arm64`). Packages are private by default in GHCR; make
the package public or configure registry credentials in TrueNAS if needed.
The most recent tag also updates `ghcr.io/cafawo/rainwise:latest`.

## TrueNAS SCALE Apps (Docker)

TrueNAS SCALE 24.10+ uses a Docker-based Apps system. If you want a single App
that runs both containers (web + controller), use the Install via YAML flow and
paste a Docker Compose file that includes both services.

Option A: Install via YAML (single app, recommended)
- Apps > Discover > Custom App > Install via YAML opens an advanced YAML editor
  that accepts Docker Compose configuration.
- Use `docker-compose.truenas.yml` as a starting point (template with placeholders).
- Set environment variables per `.env.example`.
- If using SQLite, mount a dataset to `/data` and set
  `SQLITE_PATH=/data/db.sqlite3`.
- Expose a host port (example: `8888`) mapped to container port `8000`.
  The containers will not start unless Postgres or SQLite is configured.

Option B: Custom App wizard (single image)
- The guided wizard configures a single Docker image. If you need multiple
  services in one app, use Install via YAML with a Compose file instead.

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

The dashboard shows a warning when running on the default SQLite fallback so
you can catch accidental non-persistent setups.

Controller:

- `CONTROLLER_INTERVAL_SECONDS` (default `30`)
- `RELAY_POLL_INTERVAL_SECONDS` (default `30`)
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
- `DEFAULT_SITE_LAT` / `DEFAULT_SITE_LON` (default: `50.1109` / `8.6821`)
- `DJANGO_TIME_ZONE`

Use the Django admin to create or edit:

1. `Site` with timezone and lat/lon.
2. `RelayDevice` with Modbus host/port/unit.
3. `Valve` entries mapped to relay channels.
4. `ScheduleRule` entries for weekly scheduling.

`Valve.is_active_high` (checkbox in admin) controls coil polarity:
- Checked: coil ON means valve OPEN.
- Unchecked: coil ON means valve CLOSED (use this if the relay is inverted).

## Safety Notes

- All valve openings have a planned stop and a hard max stop.
- A watchdog closes valves that appear open unexpectedly.
- The controller is designed for 30s cadence (no busy loops).

## Hardware Access

Hardware I/O is isolated in `apps/irrigation/services.py` and is never performed in HTTP request/response code. Use `RELAY_SIMULATOR=true` for local/dev without hardware.
