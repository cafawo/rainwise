# rainwise

Rainwise is a Django MVP for monitoring and scheduling an irrigation system backed by a Waveshare Modbus POE ETH Relay / 8-channel Ethernet relay module (SKU 24964). It prioritizes safety, low resource usage, and simple deployment on macOS (dev) and TrueNAS SCALE via Docker.

## Features (MVP)

- Dashboard with valve status and manual open/close.
- Weekly Fixed and Smart rules with one or more ordered valves.
- Fixed runs once per valve; Smart divides the calculated water target into
  bounded runs with automatic breaks, using weather and recorded irrigation.
- Multiple schedules with an active schedule switch.
- Controller loop that enforces planned stops and hard failsafe stops.
- Weather import (Open-Meteo) stored as hourly observations.
- Charts for accumulated irrigation per valve/day (grouped bars) on the Dashboard.
- Dashboard chart overlays precipitation and temperature on separate axes.
- Logs page with recent irrigation runs.
- Curve page to visualize and tune the daily water requirement curve (saved per site).

## Local Development

1. Create and activate the project Conda environment:

```bash
conda env create -f environment.yml
conda activate rainwise
```

2. Copy `.env.example` to `.env`, configure the database, and keep
   `RELAY_SIMULATOR=true` for development without hardware.
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

> Small quirk when updaing the app on TrueNAs: You have to specify the version tag specifically :latest is not recognized by the TrueNAS docker handler.

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

- `CONTROLLER_INTERVAL_SECONDS` (default `60`)
- `RELAY_POLL_INTERVAL_SECONDS` (default `60`)
- `WEATHER_REFRESH_HOURS` (default `6`)
- `WEATHER_LOOKBACK_DAYS` (default `30`)
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
- site timezone from `DJANGO_TIME_ZONE`

Timestamp policy:
- instant-based timestamps are stored in UTC in the database
- client-facing pages and schedule/calendar views use the site timezone
- schedule rule `start_time` is interpreted as local site time
- site timezone values use standard IANA names such as `Europe/Berlin` or `UTC`
- if multiple sites exist, the UI uses one selected active site at a time via the site switcher

Use the Django admin to create or edit:

1. `Site` with timezone and lat/lon.
2. `RelayDevice` with Modbus host/port/unit.
3. `Valve` entries mapped to relay channels.
4. Measured valve application rates before selecting valves for Smart rules.
   Curve settings are available automatically, including a 25 °C fallback
   temperature that can be changed on the curve page.

Create and edit rules from the schedule page. The same editor supports existing
single-valve Fixed rules, Fixed groups, and Smart rules. Only the site's active
schedule executes automatically.

`Valve.is_active_high` (checkbox in admin) controls coil polarity:
- Checked: coil ON means valve OPEN.
- Unchecked: coil ON means valve CLOSED (use this if the relay is inverted).

## Safety Notes

- Rainwise targets the Waveshare Modbus POE ETH Relay / 8-channel Ethernet
  relay module (SKU 24964). See the Waveshare product page and protocol table:
  `https://www.waveshare.com/product/modbus-poe-eth-relay.htm` and
  `https://www.waveshare.com/wiki/Modbus_POE_ETH_Relay`.
- Valve openings use the relay's hardware-side flash-on/flash-off timer, not a
  normal latched relay ON command.
- Flash intervals are `data * 100 ms`; Rainwise accepts integer durations up to
  `3276` seconds.
- Active-high valves open with flash-on addresses `0x0200..0x0207`.
- Active-low valves open with flash-off addresses `0x0400..0x0407`.
- Because the relay receives a bounded pulse, a database outage during an active
  run should not prevent the relay from turning the valve off when the pulse
  expires.
- Manual close and watchdog logic still send normal close commands for early
  cancellation and extra recovery.
- The controller and relay polling default to a 60-second cadence (no busy loops).

## Hardware Access

Hardware I/O is isolated in `apps/irrigation/services.py`; views call services
instead of containing hardware logic. Group sequences are executed only by the
single controller process. Existing individual manual controls use their service
path. Use `RELAY_SIMULATOR=true` for local/dev without hardware.

## Fixed and Smart Rules

Choose a mode, select and order valves at the active site, choose weekdays and
one local start time, and set a duration for each valve. Fixed requires explicit
weekday selection; Smart initially selects all seven days. Excluded days prevent
scheduled execution but remain part of water history. Each Smart valve has its
own target; grouping does not divide water between zones. Overlapping watered
areas need a separate allocation design.

Fixed runs each member once in order, with a **Runtime** of 60–3276 seconds. It needs
neither calibration nor weather. Existing single-valve Fixed rules retain their
IDs, schedule, timing, and manual Run now behavior. A valve's default manual
duration is not an additional ceiling on a Fixed rule.

Smart runs in rounds, preserving valve order until each finite target is met.
Fulfilled valves are skipped and the final pulse can be shorter, to a whole
second. **Run time before a break** limits each uninterrupted run to 1–3276
seconds; it does not cap the total water target. Before repeating a valve, its
off-time must be at least as long as its preceding commanded run, measured from
confirmed closure. Other valves' watering counts toward this break. For example,
A waters 30 minutes, B waters 5, then the controller waits another 25 before A
repeats. No break is added after a valve's final run.

New selections prefill the valve's existing default duration. An optional blank
Smart override resolves to that default when saved; explicit overrides may be
above or below it within the permitted range. **Use valve default** resets an
edited input. Saved rules and copies keep their resolved durations when the valve
default changes. That setting continues to bound individual manual Open commands.
The relay's independent 3276-second command limit is unchanged.

Newly selected Smart valves require a finite positive application rate. Curve
settings include an editable 25 °C fallback. Each occurrence saves a finite pulse
budget; there is no two-run limit or separate daily watering cap. Copies and
schedule changes still credit previous calibrated delivery, including uncertain
attempts. A valve can belong to only one enabled Smart rule per schedule.

An unmeasured valve shows **N/A** for its application rate and cannot be newly
selected for Smart. Clearing the rate of an existing Smart member preserves its
membership and skips that valve with a warning; the calibrated members continue
in their configured order. An occurrence with no calibrated members is recorded
as skipped, rather than zero demand. Fixed and manual watering remain available
without calibration.

The controller rechecks the rate before each Smart pulse. If a rate is cleared
after planning, all unattempted pulses for that valve are skipped without using
an attempt. An already-commanded pulse retains its bounded duration, stop, and
saved rate. Restoring the rate includes the valve at the next scheduled decision;
it does not replay skipped work in an existing occurrence. Current warnings clear
after correction, while historical skip reasons and delivery snapshots remain.

Fixed group **Run now** submits a durable request for the controller; it requires
an enabled rule in the active schedule and a free site. Repeating the request
returns its pending/active occurrence. Smart has **Preview** and **Stop**, and
starts only at its scheduled minute. There is no unscheduled Smart override.

## Calibration and the Rolling Water Balance

Measure each zone with catch containers. Average collected depth in millimetres
divided by watering time in hours gives the valve's application rate in mm/hour.
There is no assumed rate. New calibrated runs snapshot the rate; changing a
calibration does not change historical credit. Old runs without a rate remain
unknown. See [CSU's home lawn irrigation guidance](https://extension.colostate.edu/resource/methods-to-schedule-home-lawn-irrigation/).

The curve returns daily demand in mm/day from temperature in °C. `min_mm` and
`max_mm` are finite and satisfy `0 <= min_mm <= max_mm`; `g` is finite and positive
and `m` is finite. `coverage_days` is an integer from 1 through 7, default 2.
The fallback temperature defaults to 25 °C; review it for the site and change it
on the curve page when needed. Finite overrides, including 0 °C, are supported.
New sites receive the standard curve settings without a settings-page visit.
No Docker or environment variable is needed for the fallback or watering rate.
The curve page shows daily demand and peak watering time per valve; uncalibrated
valves show N/A and a warning that Smart will skip them.

At the actual admitted decision instant, Smart applies today's demand estimate
to the whole coverage window:

```text
target_mm = max(0, coverage_days * daily_need - rain_credit - valve_irrigation_credit)
planned_seconds = floor(target_mm * 3600 / application_rate_mm_h)
peak_seconds = floor(coverage_days * max_mm * 3600 / application_rate_mm_h)
```

Rain credit covers `coverage_days` local-day periods ending at the last completed
provider hourly boundary. At 06:30 with whole-hour weather it ends at 06:00; the
remaining half-hour is not treated as missing. Irrigation credit covers the
preceding `coverage_days - 1` calendar dates plus delivery earlier today. Thus a
two-day decision on Wednesday credits Tuesday and earlier Wednesday watering;
Monday has expired. With a one-day window only earlier irrigation today counts.
Boundaries follow the site's IANA timezone, including 23/25-hour DST dates.
Rain after the saved decision cutoff affects the next decision.

Calibrated Fixed, manual, and Smart delivery all count for that valve. Delivery
is estimated from the commanded duration, shortened by known early closure;
late controller bookkeeping adds no water. Uncertain commands conservatively
credit their nominal amount, including known command/retry time, and show an
uncertainty warning. An interrupted command without an end time also has unknown
extra delivery. Relay retries can restart a timer: these are estimates, not
measurements or an exactly-once physical watering guarantee.

For independent valves calibrated at 12 mm/hour with 900-second run limits,
each full pulse delivers 3 mm. With two coverage days, no rain, no initial
credit, sufficient time in the day, and successful watering:

| Daily demand | Delivery per valve on successive days |
| --- | --- |
| 2 mm | 4, 0, 4, 0 mm |
| 4 mm | 8, 0, 8, 0 mm |
| 7 mm | 14, 0, 14, 0 mm |

For the middle example, day one runs A:3, B:3, A:3, B:3, A:2, B:2 mm, with
the required off-times; neither valve waters on day two. At 2 mm daily demand,
3 mm rain reduces an initially uncredited two-day
target to 1 mm. If demand rises from 2 to 4 mm after a 4 mm watering day, the next
target is 4 mm before rain credit.

`max_mm` is **Peak daily demand**, not a cap on a coverage window's catch-up
watering. At 7 mm/hour, a 7 mm target takes one hour of watering; a two-day peak
at 7 mm/day takes two hours, before breaks and scheduling allowance. A peak
sequence that cannot fit before local midnight is rejected with an explanation;
tiny rates or short caps cannot allocate an unbounded sequence.
Zero-second doses are skipped. Unmet targets remain visible, and there is no
separate rounding remainder. The finite window forgets old deficits/surpluses;
outages, weather changes, and excluded days can interrupt alternating patterns.

## Weather Quality

Smart uses cached weather and never requests weather during actuation. The
controller periodically imports elapsed hourly Open-Meteo model estimates.
Temperature uses the last 24 hours' 90th percentile only with at least 18 finite
trusted hourly values and a newest valid hour no older than the refresh interval.
Otherwise it uses the configured fallback (25 °C by default); the dashboard and
curve page show the reason and latest valid weather time. A failed API call does
not disable a valid fallback decision.

An hour is trusted only when retrieval provenance shows it was fetched at or
after its valid time. Future hours and legacy rows without provenance must be
refreshed before use. Successful import time controls refresh freshness; retries
are throttled. Refreshes backfill the whole rain window and temperature history,
repair missing/untrusted interior hours, and expand when coverage increases.
The existing weather lookback is retained when it is longer.

Only known finite nonnegative precipitation is credited. Missing rain gives zero
known credit and a separate warning; it is not evidence of dry weather and can
lead to overwatering during an outage. Initial or incomplete irrigation history
has its own warning. Current warnings clear after recovery; past decisions keep
their original inputs and warnings. Open-Meteo precipitation represents the
preceding hour, includes snow, and is not a rain-gauge measurement. Total rain
also approximates available root-zone water: runoff and drainage are not modeled.

The curve is a practical heuristic, not a soil/ET model or a universal irrigation
recommendation. Equal-duration breaks are a product rule and do not guarantee
soil absorption under every condition. There is no separate soak-duration setting.

## Reservations, Stops, and Recovery

The calendar shows one group event with ordered members. Smart reservations
simulate peak demand over the coverage window, split by each saved run limit,
using the same valve order and repeat breaks as the actual plan. Today's rain
or previous delivery changes the preview dose, not this peak envelope.

```text
K = peak pulse count
R = repeat pulse count (K minus valves with a nonzero peak)
I = configured controller interval
A = derived command/retry allowance
scheduling_allowance = (K + R + 1) * I + K * A
reservation_seconds = watering_seconds + break_seconds + scheduling_allowance
```

**Watering**, **Breaks**, and **Scheduling allowance** are shown separately.
A single valve with a one-hour peak and a 30-minute run limit needs 90 minutes
before allowance. A two-hour peak needs four runs and three breaks: 210 minutes
before allowance. Grouped Fixed uses its one pass with no repeat breaks; a legacy
single-valve Fixed event remains exactly its runtime. Zero pulses have no allowance.

Missing Smart calibration omits that member from the flow-derived peak, with a
named warning. If all members are unavailable, the calendar shows a start marker
with duration N/A. Restoring a rate recalculates the envelope and requires conflict
checks before future admission. This deliberately replaces the earlier policy of
reserving two runs even for uncalibrated members.

The allowance covers nominal tick and command timing, not arbitrary outages or
guaranteed physical closure. New group windows cannot cross local midnight or overlap other automatic
rules at the site. Existing overlaps between independent single-valve Fixed
rules remain allowed. Reservations are revalidated when rate, curve, coverage,
duration, order, or cadence changes, and again at admission.
DST repeated start times produce one occurrence per local date; nonexistent
times are skipped and reported. Groups do not catch up outside their start minute.

Admission is atomic across grouped and individual starts. A conflicting run at
a scheduled group's start records a skipped occurrence, without a delayed start.
A submitted Fixed request that encounters a later conflict terminates visibly.
During a group reservation other manual/Run now starts are rejected: stop the
group first. The controller checks cancellation, current configuration/limits,
the saved pulse budget, conflicts, and remaining deadline before every pulse. It persists
the attempt before issuing the existing bounded hardware command.

Fresh confirmation that the previous valve is closed is required before advancing
to another valve. A finished database record or stale cached state is insufficient.
Uncertain opening or closure interrupts the remaining sequence. Reservation
deadlines stop new pulses that cannot fit with command/retry allowance; an
overrun retains the reservation until closure is confirmed.

While a repeat is waiting, the dashboard shows **Resting** and its next eligible
time. The persisted closure timestamp is stable across polls. The controller
checks on its normal cadence and retains the site's reservation; no service or
request sleeps to implement a break. Cancellation also works during rest.

**Stop rule** cancels pending pulses and requests closure. Closing any member of
an active group does the same, including when another member is watering.
Cancellation during an opening remains durable; the controller rechecks after
the call and closes the valve. The UI shows **Stopping** until closure is
confirmed. Disabling/deleting a group or changing the active schedule also stops
its pending work. Stop an active occurrence before changing its mode/membership
or converting a single-valve rule. History and saved configuration snapshots
survive edits and deletion.

After restart, unfinished occurrences are cancelled and attempted/running/
uncertain pulses are reconciled through closure and watchdog services, including
devices subsequently disabled. Attempted commands are never replayed and missed
dates are not backfilled. The next eligible scheduled date starts normally;
a new explicit Fixed request requires recovery and confirmed closure first.

Opening claims distinguish a sender that has not started from one whose command
is in flight. Recovery can revoke an unsent claim atomically. An in-flight web
sender keeps ownership until it acknowledges cancellation; an earlier closed
read or elapsed timeout cannot prove that a surviving process will not send
later. Scheduled senders belong to the single controller and can be reconciled
when that controller restarts. Recent committed opening attempts are recognized
by the watchdog, while genuine unexplained openings still receive closure.
If closure interrupts a command still awaiting acknowledgement, the later result
retains uncertain-delivery accounting and a warning; it cannot turn that
interrupted watering into a reported full successful delivery.
Cancellation received before the opening acknowledgement also keeps conservative
credit, including when a database failure prevents recording the racing close.
Stopping a normally acknowledged run still credits its known shortened delivery.

If a web process crashes without acknowledging its opening, the site stays
blocked conservatively. After stopping **all** old web and controller processes,
an operator can run `python manage.py reconcile_openings --senders-stopped` from
a maintenance container using the mounted database and normal relay connection,
then restart the web app and one controller. This exceptional recovery command
closes and reads valves; it never opens them. The flag confirms that no old sender
can return. Do not use it while any old sender process may still be running.

## Upgrade and Verification

Back up the persistent database, stop the old web and controller processes, apply migrations, then
start the upgraded web app and exactly one controller. Keep SQLite on the mounted
`/data` volume or use Postgres; no host cron/systemd or extra worker is required.
No deployment or hardware commissioning is part of the automated test suite.

The fallback migration fills missing temperatures with 25 °C and adds standard
curve settings for existing sites without a settings row. Existing temperature
overrides are preserved. Valve rates remain nullable/N/A; no calibration or
historical run rate is invented. Updating a Docker image requires no new
environment configuration for these defaults.

The sender-state migration adds bookkeeping without changing saved durations,
rate snapshots, or historical decisions. Older two-pass occurrences retain their
recorded meaning. New Smart occurrences use the revised finite sequence and
break policy. No new dependency or environment variable is required. Docker's
existing startup migration step applies the schema changes; keep only one
controller and stop the old web process as part of the upgrade.

The migration converts every legacy `DYNAMIC` rule to Fixed at its stored
maximum duration, including disabled rules and inactive schedules. IDs and all
other configuration/history are preserved. Already-commanded pulses retain their
original duration. Future converted runs can therefore water longer than their
previous randomly selected duration: review those maxima before re-enabling
automatic operation. Residual legacy values receive the same Fixed interpretation.

Unconfigured controller and relay polling defaults change from 30 to 60 seconds.
To preserve the old cadence, explicitly set both
`CONTROLLER_INTERVAL_SECONDS=30` and `RELAY_POLL_INTERVAL_SECONDS=30` **before the
upgrade**. Explicit settings remain supported; handovers never use faster polling.

Run automated tests in the `rainwise` environment with isolated Django test
databases and mocked hardware/weather:

```bash
env POSTGRES_HOST='' SQLITE_PATH='' RELAY_SIMULATOR=false \
  conda run -n rainwise python manage.py test apps.irrigation apps.weather --verbosity 1
```

The empty database variables select the default SQLite configuration expected by
the configuration-warning test; Django uses a temporary test database. For
Postgres verification, point the same test command at a disposable local cluster
with a dedicated test role/database. Never use production credentials for tests.
