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
- Logs page with recent irrigation runs and standard/extended JSON downloads.
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

Hardware I/O is isolated in `apps/irrigation/services.py`. Dashboard **Open** and
**Close** execute immediately through that service layer. The existing
single-valve Fixed Run now endpoint remains available for compatibility, without
a button in the rule editor. The single controller handles
scheduled watering, group progression, normal stops, watchdog recovery and
weather imports. Use `RELAY_SIMULATOR=true` for local development without hardware.

Grouped rules run only at their scheduled time. Manual valve controls do not use
a command queue. The dashboard/status API display current activity and execution
errors beside the valve. Refresh to see the result; there is no additional polling
loop. **Last Known** is the last observed relay state, or **Unknown** before a
read. Close also works for a valve with no active watering record.
`GET /api/valve-status/` retains its existing cached-state fields and adds
`action_status` and `action_error`.

## Fixed and Smart Rules

Choose a mode, select and order valves at the active site, choose weekdays and
one local start time, and set a duration for each valve. Use the up/down arrows
beside each valve to change the watering order. Fixed requires explicit
weekday selection; Smart initially selects all seven days. Excluded days prevent
scheduled execution but remain part of water history. Each Smart valve has its
own target; grouping does not divide water between zones. Overlapping watered
areas need a separate allocation design.

Fixed runs each member once in order, with a **Runtime** of 60–3276 seconds. It needs
neither calibration nor weather. Existing single-valve Fixed rules retain their
IDs, schedule, and timing. A valve's default manual
duration is not an additional ceiling on a Fixed rule.

Smart runs in rounds, preserving valve order until each finite target is met.
Fulfilled valves are skipped and the final pulse can be shorter, to a whole
second. **Run time before a break** limits each uninterrupted run to 1–3276
seconds; it does not cap the total water target. Before repeating a valve, its
off-time must be at least as long as its preceding commanded run, measured from
the conservative relay expiry. Other valves' watering counts toward this break.
For example, A waters 30 minutes, B waters 5, then the controller waits another 25 before A
repeats. No break is added after a valve's final run.

New selections prefill the valve's existing default duration. An optional blank
Smart override resolves to that default when saved; explicit overrides may be
above or below it within the permitted range. **Use valve default** resets an
edited input. Saved rules and copies keep their resolved durations when the valve
default changes. That setting continues to bound individual manual Open commands.
The relay's independent 3276-second command limit is unchanged.

Newly selected Smart valves require a finite positive application rate. Curve
settings include an editable 25 °C fallback. Each execution calculates a finite
pulse budget; there is no two-run limit or separate daily watering cap. Copies
and schedule changes still credit previous calibrated delivery, including
uncertain attempts. A valve can belong to only one enabled Smart rule per schedule.

An unmeasured valve shows **N/A** for its application rate and cannot be newly
selected for Smart. Clearing the rate of an existing member preserves its
membership; the next execution skips that valve while calibrated members
continue. Preview explains missing rates and other unavailable inputs. Fixed and
manual watering remain available without calibration.

Edits to a rule, valve rate, run limit, watering order or curve take effect on the
next execution. A running sequence retains its admitted configuration and rates.
Clear **Enabled** in the rule editor and click **Save** to stop future scheduled
executions until the rule is re-enabled; the controller also stops the current
sequence on its next tick. The rule editor has no **Run now** button. Smart keeps
**Preview**; manual **Open** and **Close** remain on the dashboard.

## Calibration and the Rolling Water Balance

Measure each zone with catch containers. Average collected depth in millimetres
divided by watering time in hours gives the valve's application rate in mm/hour.
There is no assumed rate. New calibrated runs snapshot the rate; changing a
calibration does not change historical credit. Old runs without a rate remain
unknown. See [CSU's home lawn irrigation guidance](https://extension.colostate.edu/resource/methods-to-schedule-home-lawn-irrigation/).

The curve returns daily demand in mm/day from temperature in °C. `min_mm` and
`max_mm` are finite and satisfy `0 <= min_mm <= max_mm`; `g` is finite and positive
and `m` is finite. `coverage_days` is an integer from 1 through 7, default 2.
The Curve page displays the symbolic logistic equation in "How Smart calculates water":
`D(T) = min_mm + (max_mm - min_mm) / (1 + exp(-g * (T - m)))`.
The equation, parameter definitions and reference-point explanation precede the
rolling-balance description. The default curve approximates the chart's four
reference points: 15 °C / 1 mm/day, 20 °C / 2 mm/day, 25 °C / 3 mm/day and
30 °C / 5 mm/day. These provide a practical demand scale; the sigmoid itself
is Rainwise's adjustable model.
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
Rain after the current decision cutoff affects the next execution.

Calibrated Fixed, manual, and Smart delivery all count for that valve. Delivery
is estimated from the commanded duration, shortened by known early closure;
late controller bookkeeping adds no water. Uncertain commands conservatively
credit their nominal amount, including known command/retry time, and show an
uncertainty warning. An interrupted command without an end time also has unknown
extra delivery. Relay retries can restart a timer: these are estimates, not
measurements or an exactly-once physical watering guarantee.
An opening that succeeds only after a retry also retains uncertain-delivery
credit. The remaining group sequence is cancelled under the same policy as
other uncertain deliveries; a successful retry does not erase that uncertainty.

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
refreshed before use. Successful import time controls the normal refresh cadence;
failed attempts use the existing retry throttle. The initial import backfills the
configured history (30 days by default), or the Smart coverage window if longer.
Later imports refresh the recent temperature/Smart coverage window and retain
older history. Increasing coverage takes effect at the next regular refresh.
Individual archive gaps do not trigger extra imports.

Only known finite nonnegative precipitation is credited. Missing rain gives zero
known credit; it is not evidence of dry weather and can lead to overwatering during
an outage. Smart Preview explains missing weather and uncalibrated historical
watering. The dashboard and Curve page use one fallback-temperature banner rather
than running watering calculations to produce additional warnings. Logs contain
actual attempted watering, not saved zero-demand or skipped decisions.
Open-Meteo precipitation represents the preceding hour, includes snow, and is not
a rain-gauge measurement. Total rain also approximates available root-zone water:
runoff and drainage are not modeled.

The curve is a practical heuristic, not a soil/ET model or a universal irrigation
recommendation. Equal-duration breaks are a product rule and do not guarantee
soil absorption under every condition. There is no separate soak-duration setting.

## Run History and JSON Downloads

The Logs page shows the 200 most recent runs. **Download logs** exports all
recorded history for the selected site; **Download extended logs** adds the
historical input appendix for each run. Both require login and return a JSON
attachment. The endpoints are `GET /logs/export/` and
`GET /logs/export/?extended=1`.

The document contains `schema_version` (currently `1`), `exported_at`, `site`
(`id`, `name`, `timezone`), and `runs`, ordered by newest run ID first. An empty
history produces an empty array; an installation without a site has `site: null`.
Each run includes its ID, valve identity, trigger/status, request/planning/attempt/
start/stop/closure timestamps, intended and maximum durations, stored application
rate, uncertainty, stop reason, error message, and a `delivery` object.

`delivery` contains `nominal_mm`, `estimated_mm`, the estimated delivery interval,
and calibration/uncertainty flags. It uses the same conservative estimate as the
water balance, with the export time as its cutoff. An uncertain command may keep
its full nominal allowance beyond that cutoff; the corresponding flag identifies
this. These are estimates, not flow-meter measurements. Do not calculate water
volume from the difference between bookkeeping start/stop timestamps. Exports
are read-only and can include runs whose status is still changing.

Extended exports additionally contain each run's stored `appendix`:

| Key | Historical content |
| --- | --- |
| `schema_version` | Appendix format version, currently `1` |
| `action`, `decision_at`, `site`, `valve` | Watering or recovery-close context, UTC decision time, site name/timezone and valve name |
| `mode`, `rule`, `schedule`, `simulator` | Fixed/Smart mode and saved rule/schedule identity; mode/rule/schedule are null for direct manual and recovery actions |
| `pulse` | Group scheduled start, pass number, member order, and this pulse's duration |
| `smart.calculation_version` | Calculation policy version, currently `1` |
| `smart.settings`, `smart.sequence_options` | Effective curve, coverage/fallback, remaining-day budget, controller cadence and command allowance |
| `smart.temperature` | Selected value/source, fallback reason, selection parameters, accepted hourly samples and latest valid sample |
| `smart.rain` | Accounting window, accepted hourly samples, credited rain and completeness counts |
| `smart.valve` | This valve's rate/run limit, credits, target, total planned watering, pulse count and rounding remainder |
| `smart.valve.irrigation.contributions` | Prior-run IDs, rates, credited intervals/amounts, calibration and delivery uncertainty as used at decision time |
| `smart.warnings` | Shared weather warnings and warnings relevant to this valve |

All instants use ISO 8601 UTC timestamps; site timezone identifies the local-day
accounting context. Durations and allowances are seconds, rates are mm/hour,
temperatures are °C, and water depths/credits are mm. Curve `min_mm`/`max_mm` and
`daily_need_mm` describe mm/day; `g` is inverse °C and `m` is °C. Coverage is a
count of local calendar days. `smart.valve.unmet_mm` is the planned whole-second
rounding remainder, not a final shortfall caused by cancelled/failed watering.
JSON `null` means unknown/unavailable, not zero; nonfinite legacy numbers export
as null. Keep schema versions stable for additive keys and bump them for breaking
format changes; bump `calculation_version` when calculation semantics change.

Smart inputs are frozen from the calculation used to admit the group. Each
attempted pulse stores its own valve's evidence before the relay call, so failed
attempts retain their context. Later weather imports and edits to settings or
names do not rewrite the appendix. The ordinary exported valve name is current;
`appendix.valve.name` is historical. Accepted weather samples retain their valid
and retrieval timestamps. Fixed/manual runs have basic context without Smart
evidence, and recovery records identify a close action without claiming a new
watering decision. Preview does not persist evidence.

The additive `0014_irrigationrun_appendix` migration gives existing runs `{}`;
missing historic inputs cannot be backfilled reliably. Repeated pulses duplicate
bounded weather evidence to keep each record self-contained. Routine controller
and history reads omit this data. Downloads fully read at most 200 rows at a
time before sending them, then continue by descending run ID. A slow client
therefore does not hold a database read cursor open and block SQLite writes
between download chunks. The full history is never loaded into memory. There
is no automatic pruning; existing database backups and deletion behavior apply.
No extra settings or dependencies are needed.

Appendix capture and the run INSERT happen before the opening command. If they
fail, that attempt does not open the valve. Once sent, the relay's finite timer
is independent of subsequent logging/database failures. Recovery closes are
attempted before recording them; a recovery-context or log-write error is caught
per valve so it does not abort the remaining closure loop. This preserves the
physical timer protection but is not complete resource isolation: database locks,
disk exhaustion or stalled storage can still delay software actions or prevent
scheduled watering. Those failures must never be handled by sending an unlogged,
unbounded opening. Hardware timer behavior still requires normal commissioning.

Zero-demand/skipped decisions and unattempted future pulses still have no run
records. Appendices never restore an interrupted sequence. Lawn/soil observations
and measured flow are not collected; dated observations can be compared with
these logs separately when assessing watering outcomes.

## Calendar, Stops, and Restarts

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
Missing Smart calibration omits that member from the peak, with a named warning;
if all members lack rates, the calendar shows a start marker with duration N/A.

Group reservations cannot cross local midnight or overlap other automatic rules
at the site. Existing overlaps between independent single-valve Fixed rules remain
allowed. Configuration is validated when saved and again before starting a group.
The allowance covers nominal tick and command timing, not arbitrary outages.
The controller will not start a pulse that cannot finish within the reservation.
Repeated DST times share one scheduled start; nonexistent times are skipped.
Groups do not catch up outside their scheduled minute.

The single controller holds active sequences and rest deadlines in memory. It
advances them on normal ticks without another worker, polling loop or persistent
progress record. Only attempted pulses create watering logs. Each opening carries
the existing relay timeout; the command-return time plus its duration bounds
completion and starts the rest interval. No additional read-back is required
before the next pulse after that deadline. Uncertain delivery cancels the
remaining sequence and retains conservative water credit.

Clear **Enabled** in the rule editor and click **Save** to disable the rule until
it is explicitly re-enabled.
On its next tick the controller discards future pulses and attempts an ordinary
early close of the current pulse. Deleting the rule or switching the active
schedule also stops the sequence. If early close fails, the relay's existing
timeout ends watering. Editing other settings applies to the next execution.
Closing the currently watering valve ends the remaining sequence at the next
controller check. During a break, clear **Enabled** and click **Save** because no
pulse is active.

Restarting the controller abandons unfinished sequences; they are not rebuilt or
resumed. Attempted watering logs remain, and recorded attempts prevent replaying
the group within its scheduled minute. Decisions that attempted no watering need
no stored record. Existing relay timers and the original watchdog provide the
physical backstop. Fixed/manual completion and watchdog behavior are retained.
The relay protocol, duration bounds, polarity and transport retries are unchanged.

### Accepted manual-control concurrency limit

Manual actions and the controller can issue commands concurrently, as in the
previous release. Their ordering can shorten a watering pulse or allow a delayed
timed opening after a Close action. Web controls also cannot see an in-memory
group reservation during a break, so manual intervention may interrupt a group.
Every opening still carries its own relay timeout. A failed early close may leave
watering active until that timeout. Existing overlapping single-valve Fixed runs
also retain their released behavior; their stop commands can shorten a later
pulse. These are accepted operating limits, not pending coordination work.

Page-wide warnings, errors, and action results appear above the page heading.
Failed forms show a red summary linking to invalid fields, alongside inline
errors; entered values are retained. Valve/rule-specific diagnostics remain
beside their records. Smart calculation details are available in Preview.

## Upgrade and Verification

Remaining operational limits and optional follow-up work are triaged in
[docs/ISSUES.md](docs/ISSUES.md). They are not a mandate for additional infrastructure.

For this upgrade:

1. Back up the persistent database and stop **all old web and controller
   processes**. Allow active relay timers to finish before applying the upgrade;
   the maximum timer is 3276 seconds from its last opening command.
2. Apply migrations through `0014_irrigationrun_appendix` using the existing
   database volume. The forward cleanup preserves rule IDs, saved durations,
   calibration and actual attempted watering. It removes obsolete command-queue
   and occurrence state, including unattempted future pulse logs and stored
   zero/skipped decisions. The subsequent appendix migration adds an empty JSON
   field to existing runs without rewriting their history. No migration actuates
   hardware or replays watering.
3. Start the upgraded web app and **exactly one** upgraded controller. No separate
   reconciliation command is required; manual controls are immediately available
   through the service layer.

Keep SQLite on the mounted `/data` volume or use Postgres. Docker's existing
startup migration step applies schema changes. No host cron/systemd, additional
worker, dependency or environment variable is required. Automated tests use
disposable databases and mocked hardware; they do not commission a physical relay.

The fallback migration fills missing temperatures with 25 °C and adds standard
curve settings for existing sites without a settings row. Existing temperature
overrides are preserved. Valve rates remain nullable/N/A; no calibration or
historical run rate is invented. Updating a Docker image requires no new
environment configuration for these defaults.

Previously applied migrations remain unchanged. The forward cleanup migration
supports both an upgrade from v0.1.4 and databases that already applied the
development migrations through `0012`; `0013` removes the obsolete persistent
execution state.

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

[PLANS.md](PLANS.md) records the current implementation and verification scope.
[docs/ISSUES.md](docs/ISSUES.md) records accepted limits and remaining findings.
