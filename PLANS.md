# PLANS

Build an MVP Django webapp named **rainwise** to monitor and schedule an irrigation system controlled by a Waveshare Modbus TCP Ethernet relay module (8-channel). Runs locally on macOS for development and is deployable to TrueNAS SCALE via Docker. MVP prioritizes simplicity, safety, and low resource usage.

## Smart rules: next major update (2026-09-21)

**Status: design proposal; application code is unchanged.** The user confirmed
the finite rolling-window calculation and handovers on the normal controller
tick. The coverage window uses whole days, default 2, matching the rule's daily
start time. Hourly weather observations remain an internal calculation detail.
Remaining recommendations below define the proposed first release.
The older MVP sections describe the existing foundation, including limitations
that this feature deliberately extends.

### Non-regression contract

The existing system has months of successful operation. Smart rules are an
opt-in addition; preserving established valve control takes precedence over
optimization or completing a watering target.

- Keep FIXED and DYNAMIC rule behavior, manual pulse durations, polarity,
  Modbus framing/retries, watchdog behavior, and hardware-timed closures intact.
  Do not convert existing rules or replace random DYNAMIC behavior with SMART.
  The narrow compatibility exception is conflict prevention while a smart
  reservation is active, as defined below; sites without smart work retain
  their existing execution behavior.
- Every smart opening uses the existing `services.open_valve_for()` with a
  positive integer duration, a recorded planned stop, and a bounded maximum.
  Never introduce a latched opening or issue an extra controller opening to
  extend an active pulse to meet demand. Existing driver retries are unchanged.
- Smart planning and execution belong in a separate service module, called by
  the single controller. Web pages configure, preview, and request cancellation;
  they never execute smart sequences or perform direct hardware I/O.
- A smart-planner error must not prevent existing run stops or watchdog work.
  Run the smart hook after the existing stop/watchdog stages, with a separate
  exception boundary. No network weather request belongs in smart calculation.
- Use additive migrations, existing Django/database facilities, and no new
  queue, worker, Redis, Celery, or high-frequency polling.
- No controller or hardware command is run as part of this planning task.

### Behavior and settings

A smart rule belongs to a schedule and has a name, enabled flag, site-local
start time, selected weekdays, and an ordered list of valves. All seven weekdays
are selected initially. Deselected weekdays prohibit that rule from running;
they still count as ordinary days in the balance window. Only the active
schedule executes. A valve may occur only once in a rule and in at most one
enabled smart rule in that schedule.

Grouping specifies execution order. Each valve has its own water credit and
runtime; the water target is not divided by the number of valves. This assumes
distinct irrigation zones. Overlapping zones require a separate allocation
design and are outside the first release.

| Setting | Location and proposed behavior |
| --- | --- |
| Existing `min_mm`, `max_mm`, `g`, `m` | Keep in `CurveSettings`; retain the current temperature curve. Validate finite values, `0 <= min_mm <= max_mm`, and positive `g`. |
| Coverage window (days) | Add `coverage_days` to `CurveSettings`; default 2, supported integers 1–7. This is a product scope limit, not a scientific soil-storage limit. |
| Fallback temperature (°C) | Add to `CurveSettings`; require an explicit finite value before enabling smart rules. Do not silently invent a universal fallback. |
| Application rate (mm/hour) | Add to `Valve`; optional for existing usage, finite and positive when used by a smart rule. Require measured calibration; no arbitrary default. |
| Maximum duration per run | Store per smart-rule valve membership. Require an explicit value no greater than both the valve's configured maximum and the relay maximum. |
| Maximum smart runs per valve per local day | Fixed at 2 logical controller attempts; display read-only on the curve/rule screens. Include uncertain outcomes; changing/copying schedules does not reset the allowance. Existing driver retries are part of one attempt. |

The existing `Valve.default_max_duration_seconds` is currently the manual
default, not a global limit on legacy scheduled rules. Applying it as an
additional ceiling is specific to smart rules; do not retroactively alter
FIXED/DYNAMIC schedules.

Keep settings where they belong: site-wide water policy on `CurveSettings`,
physical application rate on `Valve`, and per-rule runtime/order on membership.
The curve screen can show all relevant limits without duplicating their storage.
Do not add another daily-mm maximum, an irrigation interval, a hot-day threshold,
or an independently editable total daily runtime.

### Water calculation

Use the existing last-24-hour 90th-percentile temperature as a demand proxy,
with the validity checks below. The curve still returns **daily consumption**
in mm/day. The confirmed policy applies today's estimate to the whole coverage
window, rather than summing historical daily curve values.

For valve `v`, local date `d`, daily decision time `t`, and coverage days `L`:

```text
daily_need = curve(selected_temperature_at_t)
target_mm[v] = max(0, L * daily_need - recent_rain_mm - irrigation_credit_mm[v])

run_cap_seconds[v] <= min(valve.default_max_duration_seconds, 3276)
daily_capacity_mm[v] = 2 * run_cap_seconds[v] * application_rate_mm_h[v] / 3600
planned_mm[v] = min(target_mm[v], daily_capacity_mm[v])
planned_seconds[v] = floor(planned_mm[v] / application_rate_mm_h[v] * 3600)
```

Precise window definitions:

- Rain credit covers `L` complete local-day periods ending at the last completed
  provider hourly boundary at or before `t`. Subtract `L` local dates from that
  boundary for the start. For a 06:30 decision with whole-hour local data, count
  through 06:00 and leave the final half hour for the next decision. Do not
  invent partial-hour rainfall or report this expected lag as missing data.
  Respect the provider's preceding-hour accumulation convention; credit `L`
  rain periods, not only `L - 1`. Record the actual rain cutoff in the snapshot.
- Irrigation credit includes the preceding `L - 1` local calendar dates plus
  any delivery earlier on today's date, before the decision cutoff. Count all
  calibrated irrigation for that valve, including manual and legacy rules.
  Intersect delivery intervals with the window, including midnight crossings.
- `L = 1` therefore means one day's demand less the last daily rain period and
  today's earlier irrigation; yesterday's irrigation is not carried forward.
- Construct local-date boundaries in the site's IANA timezone, convert them to
  UTC for queries, and test 23/25-hour days. Rain and irrigation use these
  intentionally different windows: this is a batching heuristic, not a physical
  soil-water balance over one identical interval. The setting remains whole
  calendar days across daylight-saving changes, not fixed multiples of 24 hours.
- Snapshot the calculation once for the scheduled occurrence. Rain arriving
  afterward affects the next day's decision; there is no continuous replanning
  or forecast-based rain credit in this release.
- At initial activation, use trustworthy history if present. Otherwise the
  recorded credit is zero, with an explicit incomplete-history warning in the
  preview. The first day can request the full window target, within the caps.

For the default two-day window, Wednesday's decision credits Tuesday's
irrigation and anything delivered earlier on Wednesday. Monday's irrigation
has expired from that credit window, even if it started just after 06:00.
This preserves the intended alternating-day behavior without a sliding hourly
boundary through an older run. The curve still uses one recent p90 temperature.

This **finite rolling target** deliberately forgets old deficits and surpluses.
It does not promise repayment of all missed water after an outage, long dry
period, or excluded weekdays. It also does not guarantee alternating days when
temperature, rainfall, or available capacity changes.

The existing `max_mm` limits estimated daily consumption. An application serving
multiple days may exceed `max_mm` today, without introducing another mm ceiling.
Each of the two logical attempts has a bounded commanded duration. Existing
driver retries can repeat a command after a lost response, so this is not a
guarantee of exactly two physical relay activations or precisely twice the cap
in delivered runtime. Round down to whole seconds; skip a zero-second result,
rather than imposing a new minimum watering dose. No separate remainder is
accumulated; later days recompute from credited delivery.

### Worked examples and capacity warnings

Assume two identical valves, 12 mm/hour each, a 15-minute per-run maximum,
`L = 2`, no rain, and no earlier watering. Each pulse delivers an estimated
3 mm, so each valve can deliver at most 6 mm per day.

| Daily curve requirement | Per-valve daily delivery sequence | Interpretation |
| --- | --- | --- |
| 2 mm | 4, 0, 4, 0 mm | Each watering day covers two days. |
| 4 mm | 6, 2, 6, 2 mm | The requested hot-day large/small pattern. |
| 7 mm | 6, 6, 6, 6 mm | Capacity cannot meet even one day's need. |

In the 4 mm case, day one is `A:3, B:3, A:3, B:3`; day two is `A:2, B:2`.
If 3 mm of rain is credited on a day with 2 mm daily demand and zero irrigation
credit, that day's target is `max(0, 4 - 3) = 1 mm` per valve. Sufficient rain
reduces the target to zero. If demand rises from 2 to 4 mm after a 4 mm watering
day, the next target is `8 - 4 = 4 mm`, before rain.

This resolves a possible inconsistency in the original example: a large/small
pattern requires capacity greater than one day's need but less than the full
window target. If daily need itself exceeds capacity, every day hits the cap.

Show two distinct capacity diagnostics without blocking valid configurations:

- `daily_capacity_mm < max_mm`: cannot sustain peak daily demand even with
  watering every day; report likely under-irrigation.
- `daily_capacity_mm < L * max_mm`: cannot cover the full window at peak demand
  in one day; additional watering on the following day is expected.

Also report the current occurrence's unmet target when runtime/window limits
prevent delivery. Never raise a safety maximum automatically to erase a deficit.

### Weather failure and provenance

An API failure must not disable temperature-based smart decisions, but missing
weather must remain visible.

- Use only elapsed hourly values, never future timestamps. The current curve
  query has no upper bound, and the existing importer can store today's future
  hours from the forecast API; both need focused correction before activation.
- Give imported rows retrieval provenance (`retrieved_at`, nullable for legacy
  rows), and import only elapsed hours going forward. Smart weather must have
  been fetched at or after its valid time. Refresh legacy/previously forecast
  rows before trusting them; do not relabel predictions as measured rainfall.
- Proposed temperature quality rule: at least 18 finite hourly values in the
  preceding 24 hours, with the newest valid hour no older than the existing
  weather refresh interval. These are explicit engineering thresholds, not
  agronomic parameters. If the checks fail, use the configured fallback.
- Fix refresh freshness to use successful import time and retry throttling,
  rather than the maximum stored observation timestamp. Future or sparse rows
  must not suppress refresh. Keep existing short timeout/retry policies and
  controller-owned periodic import; smart calculation uses cached data only.
- Sum known nonnegative precipitation for elapsed intervals. Missing rain
  contributes no credit and raises a separate warning; this is an explicit
  continuity policy that can overwater during an outage, not a claim of no rain.
- Display: “Operating with fallback temperature X °C”, why it was selected,
  and the latest valid weather time. Show incomplete rainfall/history warnings
  separately. Clear the current fallback warning when valid data returns, while
  preserving each past decision's source and assumptions in its log.
- Use the same temperature selection helper on the curve preview and in smart
  decisions, so the curve marker and the calculated target agree.

The provider supplies weather-model estimates, not a rain gauge at this lawn.
Label precipitation and delivered irrigation as estimates. Full precipitation
credit is a simple approximation; runoff, snow, and drainage mean it need not
equal water available to roots. Cold-season irrigation and soil/ET modelling
are outside this release. The provider defines precipitation as the preceding
hour's total, including snow. [Open-Meteo API documentation](https://open-meteo.com/en/docs)

### Execution, history, and restart behavior

Use a separate `SmartRule` model and an ordered `SmartRuleValve` membership
model. This avoids making the tested single-valve `ScheduleRule` fields nullable
or overloading existing FIXED/DYNAMIC semantics.

Add a `SmartOccurrence` for each rule/local-date decision, including zero-water
decisions, with a database uniqueness constraint on `(rule, local_date)`.
Store the scheduled UTC instant, decision inputs, resulting targets, reserved
end time, and outcome. Keep a compact immutable calculation snapshot for audit.

Represent each planned pulse with the existing `IrrigationRun`, adding optional
smart occurrence, pass/order, attempt timestamp, and application-rate snapshot
fields. Use a distinct SMART trigger and a uniqueness constraint on
`(smart_occurrence, valve, pass)` for smart rows. Existing rows remain valid.
No second execution-log or general-purpose queue system is needed.

1. At the scheduled local minute, create the occurrence and its ordered pulses
   in a transaction. Skip fulfilled valves. Split each valve's target into at
   most two pulses: pass one in valve order, then pass two in the same order.
2. Before each pulse, check the occurrence is active, the rule/schedule/device
   is enabled, limits are still valid, the daily allowance remains available,
   and the pulse fits before the reservation deadline. A reduced limit cancels
   an incompatible pending pulse; it never lengthens or increases a plan.
3. Claim and record the attempt before calling `open_valve_for()`. Count attempts
   across all smart occurrences for that valve/local date, including copied or
   switched schedules. Preserve the single-controller assumption; do not rely
   solely on an in-memory counter or an `exists()` check for occurrence identity.
4. Let the existing bounded-pulse stop/watchdog path finish the run. Confirm the
   prior valve is closed with a fresh service-layer read before starting another
   valve. Reuse a successful read from this iteration when available; cached
   `last_polled_at` is not proof of freshness because it updates only on changes.
5. A failed/ambiguous open or an unconfirmed close interrupts the occurrence and
   cancels pending pulses. Never start the next valve merely because the prior
   run is marked FINISHED: existing completion deliberately tolerates a failed
   redundant close once the hardware timer should have expired.
6. On controller restart, reconcile existing smart pulses through the current
   close/watchdog facilities and cancel unfinished occurrences for that day.
   Never replay an attempted pulse whose hardware outcome is uncertain. Resume
   normal planning on the next eligible date; do not backfill missed dates.

This conservative restart policy trades possible under-watering for avoiding
duplicate actuation. A database failure cannot remove the relay's independent
pulse deadline; successful command delivery and exactly-once physical watering
cannot be guaranteed across network failures and crashes.

For water credit, snapshot calibrated rates on new runs, including manual and
legacy runs for calibrated valves. Estimate delivery using the commanded pulse
duration, shortened by a known early stop; never count the delay until the
controller finally recorded `actual_stop_at` as extra watering. Rate edits must
not rewrite historical delivery. Legacy rows without a rate snapshot remain
unknown rather than acquiring invented historical accuracy.

An uncertain command outcome is not zero delivered water. Credit the full
commanded dose conservatively during the lookback, with an uncertainty warning,
rather than automatically replacing it with another pulse. Where the attempt
start/end are known, include that command/retry interval in the conservative
possible-delivery estimate. A crash with no recorded attempt end retains the
nominal dose and explicitly unknown additional delivery. This accounting choice
is not a proven physical upper bound or measured flow; existing retries can
restart the relay timer without changing the recorded nominal pulse duration.

### Scheduling, cancellation, and calendar reservations

For the first release, reserve smart sequences serially within a site. Reject
new/edited smart windows that overlap another automatic rule at the site;
apply the same smart-window check when editing or activating a schedule. Do not
retroactively reject unrelated overlaps between two existing legacy rules.
At runtime, a conflicting active/manual run prevents smart progression and
produces a visible skip/interruption rather than parallel watering.
Add a narrow check before legacy automatic starts too: while a smart opening
is claimed/in flight, running, or awaiting closure confirmation, skip and report
conflicting automatic starts. A calendar end or FINISHED log alone must not
release that reservation. Existing stop/watchdog processing continues. This
guard is necessary because legacy automatic starts currently precede stops and
the new smart hook in the controller loop.

Manual close must remain effective. Closing a member of an active smart group
also cancels the occurrence's remaining pulses; the controller closes any other
active pulse in that occurrence through the existing service. Provide a clear
“Stop smart run” action. Manual open and legacy “Run now” must check an active
smart reservation before issuing a pulse and ask the user to stop that smart
run first. Outside an active smart reservation, existing manual behavior stays
the same. Persist cancellation and treat a claimed/in-flight opening as an
active reservation. After the hardware call returns, immediately recheck
cancellation and close through the existing service if cancellation arrived
during the call. Show “Stopping” until the controller acknowledges cancellation
and confirms closure; a web close alone cannot defeat an in-flight later open.
Use atomic database state transitions that work on SQLite and Postgres, not
`select_for_update()` alone. Test both possible orderings of start/cancel and
do not hold a transaction open across hardware I/O.

Disabling/deleting a smart rule or changing the active schedule cancels its
pending work and requests closure of its active pulse. Preserve occurrence/run
audit history when editing or deleting configuration.

For differing per-valve limits, maximum watering time is:

```text
water_time = 2 * sum(per_valve_run_cap_seconds)
```

The original `valve_count * common_max * 2` is its equal-limit special case.
Normal controller handovers also consume wall-clock time. Reserve and display
`water_time + 2 * valve_count * controller_interval_seconds` as the initial
window, showing watering time and handover allowance separately. This derived
allowance introduces no new user setting. Slow hardware/network operations can
exhaust it: use the end as a launch/progression deadline and skip pulses whose
nominal duration plus the configured command/retry allowance cannot fit.
Calculate fit using a fresh clock reading immediately before sending, not the
controller loop's earlier timestamp. Network timing prevents this from being
an exact physical stop guarantee: retain the active reservation and block
conflicting starts until closure is confirmed, even beyond the displayed end.
Revalidate before starting if cadence changed.

Reject smart windows crossing local midnight in the first release, so weekday
selection and the two-runs-per-date limit remain straightforward. A repeated
DST start time has one occurrence per local date; a nonexistent local start
time is skipped and reported. No catch-up batch is started outside its due
minute. Tests must cover both DST transitions and controller delays.

**Cadence discrepancy to resolve explicitly during implementation:** current
code, README, and `.env.example` use 30 seconds; `AGENTS.md` requires a 60-second
default. Align defaults/documentation with 60 seconds, preserve explicit
deployment overrides, and regression-test 30 and 60 seconds. An existing
deployment that currently omits the setting also inherits a changed default;
document that it must pin both controller/poll intervals to 30 before upgrading
if unchanged cadence is required. Do not add faster polling for smart handovers.

### Research and scope boundaries

- German LWG guidance puts established-lawn consumption around 2–3 mm/day at
  20–25 °C daily maximum and 4–7 mm/day at 30–35 °C, with less frequent deeper
  applications. This supports the current curve's rough scale, but not its
  exact sigmoid, p90 input, or a universal two-day schedule.
  [LWG: Basiswissen Rasenbau](https://www.lwg.bayern.de/mam/cms06/landespflege/dateien/basiswissen_rasenbau.pdf)
- Application rates and soil conditions vary substantially. Measure each zone
  with catch containers; a simple calibration is average collected depth in mm
  divided by run time in hours. There is no defensible universal per-run minute
  cap. Set membership caps from site observations and retain both hardware and
  valve ceilings. [CSU: Methods to Schedule Home Lawn Irrigation](https://extension.colostate.edu/resource/methods-to-schedule-home-lawn-irrigation/)
- Rotating zones provides a break, but one-valve groups, skipped valves, and
  short pulses may give almost no soak time. The proposed first release does
  not guarantee runoff prevention and adds no soak-delay parameter. If a site
  needs a guaranteed absorption period, a separately designed minimum soak
  setting and longer calendar reservation are necessary before using smart
  control there. Two runs is the requested operating cap, not an agronomic
  optimum. [CSU: Watering Efficiently](https://extension.colostate.edu/resource/watering-efficiently/)
- Effective rainfall differs from total precipitation because some water is
  lost before roots can use it. Keep the approximation visible and avoid an
  uncalibrated efficiency multiplier. [FAO: Rainfall and Evapotranspiration](https://www.fao.org/4/r4082e/r4082e05.htm)
- The existing Waveshare duration limit is grounded in the device's
  `0x7FFF * 100 ms` pulse range, yielding 3276 whole seconds. This is a hardware
  ceiling, not a recommended irrigation duration.
  [Waveshare relay protocol](https://www.waveshare.com/wiki/Modbus_POE_ETH_Relay)

### Implementation sequence and acceptance gates

1. **Protect the baseline.** Retain all existing tests, document the compatibility
   contract, resolve the cadence documentation/default discrepancy explicitly,
   and add regression coverage for smart exceptions leaving stops/watchdog
   operational. Do not refactor the Modbus driver as part of this feature.
2. **Add configuration and pure calculation.** Add models/migrations, calibrated
   rate snapshots, shared curve/fallback selection, corrected weather provenance
   and freshness, and deterministic balance/capacity helpers. Smart rules remain
   disabled until their required configuration is valid.
3. **Build a reviewable preview.** Extend the curve screen with units, parameter
   explanations, fallback state, capacity checks, calibration guidance, and the
   worked examples. Add smart rule CRUD, ordered members, all-days defaults,
   calendar reservations, and a preview of expected doses/pulse order. Copy/load
   schedules must include smart rules and validate conflicts. This stage has no
   smart actuation.
4. **Integrate bounded execution.** Add durable occurrences/claims, the isolated
   controller hook, fresh closure confirmation, cancellation/conflict handling,
   global per-valve daily attempt accounting, and restart interruption. Record
   zero-demand, capacity-limited, fallback, uncertain, and skipped decisions.
5. **Verify before deployment.** Run the full Django suite and deterministic
   multi-day simulations. Update README and curve-page documentation with the
   final behavior, setup/calibration, persistence, fallback trade-offs, and
   recovery policy. Synchronize `.env.example` if defaults change; no new
   environment variables or dependencies are planned. Review simulator results
   before a separately arranged bounded hardware commissioning run.

Required focused tests cover:

- Alternate-day and large/small examples; sustained insufficient capacity;
  rainfall-only skipping; temperature changes; `L = 1`, `L = 2`, and longer
  supported windows; reject fractional-day settings; local-date windows across
  DST; irrigation just after the daily start time; excluded days; first
  activation and credit expiry; different valve rates.
- Invalid/missing calibration and fallback; NaN/infinite/negative inputs;
  temperature coverage/staleness; future weather; incomplete rain; API outage
  and recovery; import freshness; DST and precipitation interval boundaries.
- Valve order across two passes, partial second pulses, fulfilled-valve skipping,
  caps at all three levels, zero duration, and no third attempt after duplicate
  ticks, errors, copies, active-schedule changes, or restarts.
- Failures before/after command delivery and DB writes, no uncertain replay,
  fresh closed-state confirmation, manual cancellation/start races, disabled
  devices/rules, deadline exhaustion, midnight restrictions, and site isolation.
- Estimated delivery bounded by pulse/early stop, midnight attribution, unknown
  historical rates, and unchanged credit after calibration edits.
- Existing fixed/dynamic/manual behavior, active-high/low protocol frames,
  forbidden unbounded opening, weather/UI behavior, schedule copy/load, and
  SQLite-compatible constraints; verify the new migrations on Postgres too.

Planning validation (2026-09-21): all **43 existing irrigation/weather tests
pass** in the `rainwise` Conda environment, using Django's temporary test DB
and mocked hardware/network access:

```sh
env POSTGRES_HOST='' SQLITE_PATH='' RELAY_SIMULATOR=false \
  conda run -n rainwise python manage.py test apps.irrigation apps.weather --verbosity 1
```

An initial run with an explicit SQLite path made the existing default-SQLite
warning test fail because that test expects the fallback configuration; the
command above restores that intended test configuration. No application change
was needed. The suite also emits the existing missing-`staticfiles` warning.
These automated tests establish a regression baseline, not hardware validation.

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

## Relay-enforced failsafe hardening (2026-05-19)

Rainwise targets the Waveshare Modbus POE ETH Relay / 8-channel Ethernet relay
module (SKU 24964). Valve openings must use the module's hardware-side timed
flash command, not a latched relay ON command.

### Safety model

- Every valve opening is sent as a bounded relay pulse.
- Active-high valves use flash-on addresses `0x0200..0x0207`.
- Active-low valves use flash-off addresses `0x0400..0x0407`.
- Flash intervals are `data * 100 ms`; Rainwise accepts integer durations from
  1 to 3276 seconds (`0x7FFF / 10`, rounded down).
- Scheduled fixed runs pulse for `ScheduleRule.max_duration_seconds`.
- Scheduled dynamic runs compute `optimal_duration_seconds` first and pulse for
  that intended duration.
- Manual dashboard opens pulse for `Valve.default_max_duration_seconds`.
- Manual close and watchdog close still send the normal closed coil state for
  early cancellation and recovery, but safety does not depend on a later close.
- New runs still require the DB because Rainwise needs configuration and audit
  state before sending hardware commands.

### Implementation notes

- `apps/irrigation/services.py` exposes `open_valve_for(valve, duration_seconds)`.
- Unbounded `open_valve(valve)` is disabled so new code cannot latch a relay on.
- The Waveshare flash command uses Modbus function `0x05` with a non-boolean
  value field, so Rainwise sends that one command with an explicit Modbus TCP
  frame rather than `pyModbusTCP.write_single_coil()`.
- Duration validators reject schedule and valve defaults above 3276 seconds.
- Existing over-limit rows are not clamped; attempts to start them fail with a
  clear error until configuration is corrected.

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
- Site timezone selection should use validated IANA timezone names.
- The UI should operate on one explicit active site at a time.
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

- `open_valve_for(valve: Valve, duration_seconds: int) -> None`
- `close_valve(valve: Valve) -> None`
- `read_valve_state(valve: Valve) -> bool`
- `read_device_states(device: RelayDevice) -> list[bool]`

Implementation:
- Real mode uses explicit Modbus TCP function `0x05` frames for Waveshare flash
  open commands and `pyModbusTCP` for normal close/read operations.
- Simulator mode (`RELAY_SIMULATOR=true`) uses a DB-backed simulated coil state so UI/tests work without hardware.

Rules:
- Unbounded relay ON is not allowed for valve opening.
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
    - send a relay flash command for `optimal_duration_seconds`
    - on success: status RUNNING + set `actual_start_at`
    - on failure: status FAILED + record error

3) **Stop runs**
For each RUNNING IrrigationRun:
- If `optimal_duration_seconds` is set and `now >= actual_start_at + optimal_duration_seconds`:
  - best-effort close valve; the relay flash command already provides the
    primary hardware stop
  - mark stop_reason COMPLETED
- If `now >= actual_start_at + max_duration_seconds`:
  - best-effort close valve; the relay flash command already provides the
    primary hardware stop
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
