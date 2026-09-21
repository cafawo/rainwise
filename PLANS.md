# Implementation plan: Fixed and Smart rules

Current design for the next Rainwise release, consolidated on 2026-09-21.
This document replaces the previous MVP checklist and intermediate proposals.
Application implementation is complete as of 2026-09-21. Follow this plan as
the current source of truth; update it explicitly if behavior changes. The
implementation and verification records are in sections 10 and 11 below.

## 1. Goal and compatibility contract

Provide two rule modes, **Fixed** and **Smart**, through one editor. Both allow
one or more ordered valves. Fixed runs each valve once for its configured
runtime. Smart uses the existing temperature curve, recent rain, and irrigation
history to calculate up to two runs per valve per local day.

The existing valve control has months of successful operation. Preserve its
hardware safety behavior before optimizing watering or meeting a target:

- Keep the existing `services.open_valve_for()`, close/read services, Modbus
  framing, polarity, retries, and hardware-timed pulses. Unbounded opening stays
  prohibited. Never issue an extra controller opening to extend an active run.
- The Waveshare relay accepts integer pulse durations of 1–3276 seconds. Every
  opening has a recorded intended duration and maximum. Existing invalid
  over-limit settings fail before actuation; do not silently clamp them.
- Keep existing single-valve Fixed timing, durations, manual overrides,
  duplicate prevention, stops, and watchdog behavior. The intentional changes
  are retiring Dynamic, preventing conflicts with active group reservations,
  and the documented alignment of unconfigured cadence defaults to 60 seconds.
- Preserve existing run history and any already-commanded pulse's original
  duration. Changing rule configuration must not rewrite an active run.
- One controller process owns grouped execution, stops, recovery, and periodic
  weather imports. Views use services and never execute group sequences or
  contain direct hardware I/O. Existing manual valve operations retain their
  service-layer hardware path, subject to group conflict/cancellation checks.
- Group-planning failures must not prevent existing stops/watchdog work. Add the
  group hook after those stages with its own exception boundary. Smart planning
  uses cached weather; it performs no network request in the actuation path.
- Use ordinary Django/database facilities, conservative timeouts, and writes
  only when needed. No Celery, Redis, extra controller, busy loop, or new dependency.
- Retain authentication, active-site isolation, UTC timestamps, site-local IANA
  timezone display/scheduling, and the existing charts and logs.
- Keep Docker/TrueNAS deployment and persistence: mounted SQLite such as `/data`
  or Postgres, without host cron/systemd. Local commands run in Conda `rainwise`.

## 2. Retire Dynamic with automatic Fixed compatibility

Dynamic currently refreshes weather and then chooses a random duration; the
weather values do not affect that duration. Retire that simulation completely.

1. Add a forward data migration changing every stored `ScheduleRule.mode` of
   `DYNAMIC` to `FIXED`, including disabled rules and inactive schedules. Change
   only the mode: preserve IDs, valve, schedule, enabled state, weekdays, start
   time, note, and `max_duration_seconds`. Do not block on Dynamic rows, delete
   them, disable them, or convert them to Smart. Reversing this normalization
   leaves rules Fixed; it must not turn genuine Fixed rules into Dynamic.
2. Retain one narrow internal normalization for residual stored `DYNAMIC` values:
   interpret them as Fixed when displaying, editing, copying, scheduling, or
   processing Run now. New saves persist `FIXED`; submitted/new Dynamic choices
   are not supported. This compatibility spelling is never exposed as a mode.
3. Remove Dynamic from current model/form/admin choices, user-facing labels,
   calendar presentation, and current feature documentation. Keep historical
   migration files intact. Do not create new Dynamic records.
4. Remove random-duration selection and its mode-specific weather refresh calls
   from scheduled execution and Run now. Retain controller-owned periodic
   weather refresh. Future converted runs use their stored maximum, exactly as
   Fixed does; they can therefore water longer than the old random selection.
5. Dispatch recognized modes explicitly after normalization. Unknown modes must
   fail without opening a valve; Smart must never fall through to Fixed's
   maximum-duration execution path.
6. Leave existing `IrrigationRun` rows and running pulses untouched. The removal
   affects future starts, not their existing commanded durations or stop times.

## 3. One rule editor and two execution modes

Editor order:

1. **Mode:** Fixed or Smart.
2. **Valves:** select one or more valves at the active site and order them.
3. **Schedule:** enabled state, weekdays, local start time, and optional note/name.
4. **Duration per valve:** fixed runtime or Smart maximum runtime per run.

| Mode | Duration and sequence | Weather requirements |
| --- | --- | --- |
| Fixed | One ordered pass; each valve runs its configured duration once. | None. Calibration/fallback settings are not required. |
| Smart | First pass in valve order, then a second pass if needed; skip fulfilled valves and shorten the final pulse. | Measured valve rates; curve settings and an overridable 25 °C fallback default. |

- Existing Fixed rules display with their one valve selected. Do not merge
  independent existing rules into groups automatically or change their days.
- Smart defaults to all seven weekdays selected. Require at least one selected
  day; an excluded day prohibits scheduled execution but still counts in history.
- Fixed retains explicit weekday selection. Both modes use one start time for
  their whole sequence, not an independent identical start for every member.
- Only the site's active schedule executes automatically. Reject duplicate
  members and valves from another site. Within a schedule, a valve belongs to
  at most one enabled Smart rule; independent Fixed watering is still credited.
- Grouping changes order, not water allocation: each Smart valve represents its
  own irrigation zone and receives its own target. Overlapping watered areas
  require a separate allocation design and are outside this release.
- Fixed single-valve Run now keeps its existing behavior. Fixed group Run now
  submits a request to the controller for one pass; it does not open valves in
  the HTTP request. It bypasses weekday/start-time matching, but requires an
  enabled rule in the active schedule and all group safety/conflict checks.
  Return an existing pending/active request instead of queueing a duplicate.
- Smart offers Preview and Stop; live execution occurs at its scheduled minute.
  Do not add an unscheduled Smart override in this release.
- Copying/loading schedules, rule editing/deletion, calendar events, logs, and
  admin must handle both storage types below through this same product model.

## 4. Data and service structure

Keep the existing single-valve `ScheduleRule` and Fixed execution path. Its
current choices become Fixed only, with the internal legacy normalization above.
New single-valve Fixed rules use this path too.

Use shared group models:

- `GroupedRule`: schedule, Fixed/Smart mode, name/note, enabled state, weekday
  mask, and local start time. Use it for multi-valve Fixed and all Smart rules.
- `GroupedRuleValve`: valve, unique order, and configured duration. The duration
  means a fixed runtime in Fixed mode and a per-run maximum in Smart mode.
  Enforce unique membership and same-site ownership.
- `RuleOccurrence`: durable group execution/decision, including zero-demand
  decisions, immutable mode/configuration snapshot, scheduled local date and UTC
  instant, request source/time, reservation deadline, and outcome. Scheduled
  occurrences are unique by `(rule, scheduled_local_date)`; manual Fixed requests
  have no scheduled date and can be repeated after a prior request completes.

Continue to use `IrrigationRun` for individual pulses. Add optional occurrence,
pass/order, attempt timestamp, and application-rate snapshot metadata; preserve
existing rows. Group pulses are unique by `(occurrence, valve, pass)`. Identify
Smart accounting from the occurrence's saved mode. Preserve SCHEDULED/MANUAL
trigger semantics and existing single-valve scheduled-minute idempotence; group
passes use occurrence/pass identity rather than that single-valve lookup.

Editing a single-valve Fixed rule into Smart or a multi-valve rule creates its
replacement group configuration and removes the old rule configuration in one
transaction; run history is untouched and the response redirects to the new
rule. Do not convert a rule while its valve has an active run/reservation.
A grouped rule reduced to one Fixed valve may stay in group storage. Mode or
membership changes on a group likewise require its occurrence to be stopped
first. Preserve historical occurrence snapshots if a group configuration is
later deleted; configuration cascades must not erase execution history.

Put shared group planning/execution in a separate irrigation service module.
Keep the hardware driver unchanged. The shared editor routes to the appropriate
storage/service; no user-facing distinction between legacy and grouped storage.
No second pulse-log model or general-purpose job queue is needed.

### Settings and limits

| Setting | Location and behavior |
| --- | --- |
| Existing `min_mm`, `max_mm`, `g`, `m` | `CurveSettings`; retain the curve. Validate finite values, `0 <= min_mm <= max_mm`, and positive `g`. |
| `coverage_days` | `CurveSettings`; default 2, supported integers 1–7. This is a product scope limit, not a scientific soil-storage limit. |
| Fallback temperature (°C) | `CurveSettings`; finite, defaults to 25 °C, editable in the app. Existing null values become 25 °C; preserve overrides. No Docker/environment setting is required. |
| Application rate (mm/hour) | `Valve`; nullable and displayed as N/A until measured. New Smart selections require a finite positive rate. Existing Smart members that lose their rate are skipped individually with a warning; other members continue. |
| Fixed runtime | Existing rule or group membership; preserve existing Fixed validation, 60–3276 seconds. |
| Smart per-run maximum | Group membership; integer 1–3276 seconds, also no greater than `Valve.default_max_duration_seconds`. |
| Smart attempts per valve/local day | Fixed at 2, displayed read-only. Include uncertain attempts across copied/switched schedules. Driver retries belong to the same logical attempt. |

`Valve.default_max_duration_seconds` currently governs manual openings; it is
not a global ceiling on existing Fixed rules. Do not introduce that extra
ceiling for Fixed as part of this release. Smart explicitly applies it.

Show relevant limits and capacity on the curve page without duplicating their
storage. Do not add another daily-mm maximum, an irrigation interval, a hot-day
threshold, or an independently editable total daily runtime.

## 5. Smart water calculation

Use the existing last-24-hour 90th-percentile temperature as the daily-demand
proxy, subject to the weather checks below. Apply today's estimate to the whole
coverage window; do not sum historical daily curve values or integrate a new
hourly temperature model.

For valve `v`, local date `d`, decision cutoff `t`, and coverage days `L`:

Use the actual admitted decision instant for `t`, within the scheduled minute;
store the nominal scheduled instant separately for occurrence identity. Watering
that finishes earlier in that same minute is therefore included in the credit.

```text
daily_need = curve(selected_temperature_at_t)                         # mm/day
target_mm[v] = max(0, L * daily_need - rain_credit_mm - irrigation_credit_mm[v])
capacity_mm[v] = 2 * run_cap_seconds[v] * application_rate_mm_h[v] / 3600
planned_mm[v] = min(target_mm[v], capacity_mm[v])
planned_seconds[v] = floor(planned_mm[v] / application_rate_mm_h[v] * 3600)
```

### Accounting boundaries

- Rain covers `L` local-day periods ending at the last completed provider hourly
  boundary at/before `t`. Subtract `L` local dates for the start. For 06:30 with
  whole-hour local weather, count through 06:00; do not invent partial-hour rain
  or mark the expected half-hour lag as missing. Respect preceding-hour totals
  and record the actual cutoff. Credit `L` rain periods, not `L - 1`.
- Irrigation includes the preceding `L - 1` local calendar dates plus delivery
  earlier today before `t`. Count calibrated Fixed, manual, and Smart irrigation
  for that valve. Intersect delivery intervals with the window, splitting at
  midnight as needed. With `L = 1`, only today's earlier irrigation is credited.
- Construct boundaries in the site's IANA timezone and query in UTC. Coverage
  remains calendar days across 23/25-hour DST dates, not fixed multiples of
  24 hours. Rain and irrigation intentionally use different windows: this is a
  batching heuristic rather than a physical soil-water balance.
- With the default two-day window, Wednesday credits Tuesday's watering and
  Wednesday's earlier watering. Monday's watering has expired even if it started
  just after 06:00; this avoids an additional skipped day from an hourly boundary.
- Snapshot each scheduled decision once. Rain after the cutoff affects the next
  decision; there is no continuous replanning or forecast rain credit.
- At first activation, use trustworthy recorded history; otherwise record zero
  known credit and show an incomplete-history warning. The first target can be
  the full coverage amount, still bounded by the runtime limits.

The finite window intentionally forgets old deficits and surpluses. It cannot
promise indefinite repayment after outages, excluded days, or sustained capacity
shortfalls. Temperature/rain changes can interrupt the alternating-day pattern.

`max_mm` limits daily demand, not the application on a day covering several days.
Skip zero-second doses; do not invent a minimum watering dose. No separate
rounding remainder accumulates: later decisions recompute from recorded credit.
Non-finite/invalid calculation results must fail without actuation.

### Examples and capacity diagnostics

Two valves, each at 12 mm/hour with a 15-minute per-run cap, can each deliver
3 mm per pulse and 6 mm per day. For `L = 2`, no rain, and no initial credit:

| Daily need | Per-valve daily delivery | Result |
| --- | --- | --- |
| 2 mm | 4, 0, 4, 0 mm | Alternate-day watering. |
| 4 mm | 6, 2, 6, 2 mm | Hot-day large/small pattern. |
| 7 mm | 6, 6, 6, 6 mm | Capacity cannot cover even one day's need. |

In the middle case, day one is `A:3, B:3, A:3, B:3`; day two is `A:2, B:2`.
With 2 mm daily need, 3 mm rain credit, and no irrigation credit, the target is
`max(0, 4 - 3) = 1 mm`. If need rises from 2 to 4 mm after a 4 mm watering day,
the next target is `8 - 4 = 4 mm` before rain.

Display distinct diagnostics without rejecting a valid low-capacity setup:

- `capacity_mm < max_mm`: cannot sustain peak daily need, even with daily watering.
- `capacity_mm < L * max_mm`: cannot cover the full window at peak need in one day;
  additional watering on the following day is expected.
- Report unmet targets caused by dose, attempt, or reservation limits. Never
  increase a safety limit automatically to erase a deficit.

## 6. Weather resilience and irrigation estimates

An API failure must not disable Smart temperature decisions. Use the configured
fallback (25 °C by default). Missing weather must be visible.

- Use only elapsed hourly values. Fix the current curve query's missing upper
  time bound and the importer's ability to store today's future forecast hours.
- Add nullable retrieval provenance to weather rows. Smart inputs must have
  been fetched at/after their valid time. Refresh legacy rows without provenance
  and previously forecast rows before trusting them; new imports keep elapsed
  hours only. Model estimates must not be labelled rain-gauge measurements.
- Temperature requires at least 18 finite hourly values in the preceding 24 hours
  and a newest valid hour no older than the existing weather refresh interval.
  Otherwise use the configured fallback. These are fixed engineering checks,
  not new agronomic settings. Preview and controller use the same selection helper.
- Base refresh freshness on successful import time and existing retry throttling,
  not the newest observation timestamp. Future/sparse rows cannot suppress a
  needed refresh. Keep short timeouts and periodic controller-owned importing.
- Fetch/backfill the complete rainfall window required by enabled Smart rules
  and the preceding 24 hours needed for temperature. Include missing or untrusted
  hours within that range, including after increasing `coverage_days`; do not
  merely continue from the newest observation or retain a two-day-only fetch.
  Use the existing import lookback when it is longer. Repair gaps through the
  normal throttled refresh, without extra calls during a Smart decision.
- Credit known finite nonnegative precipitation only. Unknown rainfall supplies
  no credit and raises a separate warning: continuing during an outage can
  overwater, and zero credit must not be described as proof of no rain.
- The dashboard and curve page show “Operating with fallback temperature X °C”,
  the reason, and latest valid weather time. Show missing-rain/history warnings
  separately. Clear current warnings when resolved, preserving past decision logs.

Snapshot application rates on new calibrated runs, including Fixed/manual runs.
For known outcomes, estimate delivery from the commanded duration, shortened by
known early closure, not the later time when the controller recorded completion.
Rate edits must not rewrite history; old runs without calibration remain unknown.

An uncertain command is not zero delivery. Credit its full nominal commanded
amount conservatively, with an uncertainty warning. Where the command/retry
interval is known, include it in the possible-delivery estimate. A crash without
an attempt end retains nominal credit plus explicitly unknown extra delivery.
Existing retries can restart the relay timer: neither the estimate nor the
two-logical-attempt limit is proof of an exact physical volume or pulse count.

## 7. Durable sequential execution and recovery

For Fixed groups, plan one pulse per member. For Smart, split each valve's target
into at most two capped pulses, first pass in member order and then second pass.
Skip fulfilled valves. Persist zero-demand Smart decisions too.

1. At the scheduled local minute, atomically create the occurrence and planned
   pulses. A manual Fixed group request uses the same planner on a controller
   tick. Snapshots prevent subsequent configuration edits from changing history.
   Admit scheduled work only when the site is free of conflicting active,
   in-flight, or uncertain earlier watering. Otherwise record a skipped
   occurrence and no runnable pulses; do not wait and launch a frozen Smart
   target after that other watering finishes.
2. Before each pulse, check cancellation, active schedule, enabled rule/device,
   current limits, conflicts, and remaining time. For Smart also check the two
   logical attempts per valve/local date across all Smart occurrences. A reduced
   limit cancels an incompatible pending pulse; it does not grow a plan.
3. Claim and persist the attempt before sending a command. Use fresh timestamps
   around the hardware call rather than the earlier controller-loop time.
   Database failure must never lead to starting an unrecorded planned attempt.
4. Open through the existing timed-pulse service. The unchanged stop/watchdog
   path provides normal finishing and recovery. Before advancing, obtain fresh
   confirmation that the prior valve is closed; a successful read this iteration
   can be reused. Cached `last_polled_at` changes only on state changes and is
   not proof of a fresh read.
5. A FINISHED run alone is insufficient: existing completion intentionally
   tolerates a failed redundant close after the hardware timer should expire.
   An ambiguous open or unconfirmed close interrupts the group and cancels its
   pending pulses. Never replace an uncertain pulse automatically.
6. On restart, cancel unfinished group occurrences and reconcile active/uncertain
   pulses through the existing closure/watchdog facilities. Do not replay an
   attempted command or backfill missed dates. Normal planning resumes on the
   next eligible date; a new explicit Fixed manual request requires recovery
   and confirmed closure first.
   Explicitly inspect attempted PLANNED/FAILED rows as well as RUNNING rows:
   existing stops/watchdog alone cannot establish their hardware outcome.
   Use existing close/read services even if the relay device was subsequently
   disabled; disabling prevents new openings, not necessary closure/recovery.

Claims must use atomic state changes/constraints that work on SQLite and
Postgres, not `select_for_update()` alone. Do not hold a database transaction
open across hardware I/O. Preserve the single-controller deployment assumption.
Group reservation acquisition and admission of existing manual/single-valve
Fixed starts must share atomic per-site coordination. A separate check followed
by a later run creation is insufficient because web requests race the controller.
Register an admitted opening as in flight before hardware I/O, so the other
path sees it as occupied. Serialize admission transactions only; do not impose
new lifetime exclusivity on unrelated legacy single-valve Fixed runs.
A database/network outage cannot remove an already-issued hardware timer, but
exactly-once physical watering cannot be promised after an ambiguous response.

### Cancellation and coexistence

- Reserve group execution serially within each site. Reject a proposed group
  window overlapping another automatic rule there; check edits, copies, and
  schedule activation. Do not reject unrelated overlaps between two existing
  single-valve Fixed rules. Existing group configuration conflicts block launch.
- A conflicting run/claim at a scheduled group's start produces a skipped
  occurrence, with no delayed automatic launch. Fixed group Run now rejects a
  conflict at submission; if one arises before controller admission, terminate
  the request visibly rather than retaining it for an unexpected later start.
  A conflict detected within an executing group interrupts its remaining work.
  Before a single-valve automatic start, use the same atomic admission mechanism
  to check group claims, in-flight opens, running pulses, and unconfirmed closures
  and skip/report conflicts. This guard is needed because existing automatic
  starts occur before the controller's stop stage.
- Keep reservations until closure is confirmed, even past the calendar end.
  Keep attempting existing recovery/closure while group progression is stopped.
- Closing any member of an active group cancels its remaining pulses and asks
  the controller to close any other active pulse in the occurrence. Provide
  “Stop rule” for both group modes. Other manual starts/Run now are rejected
  during a site group reservation, with a clear instruction to stop the group.
- Persist cancellation during in-flight opening. Immediately after the hardware
  call, recheck cancellation and close if it arrived during that call. Show
  “Stopping” until acknowledged and closure confirmed; a web close alone cannot
  defeat a later in-flight open. Test both start/cancel orderings.
- Disabling/deleting a group or switching the active schedule cancels pending
  work and requests closure. Preserve all historical occurrence/run snapshots.

### Calendar reservations and local time

For `N` valves and `P` passes (`1` Fixed, `2` Smart):

```text
water_time = P * sum(configured_member_duration_seconds)
reserved_time = water_time + P * N * controller_interval_seconds
```

For Smart the configured durations are maxima, even when expected demand is
smaller. Show watering time and handover allowance separately. This is a
worst-case watering reservation with a derived handover allowance, not a new
user parameter or a guarantee of exact physical closure time.

Use the end as a launch/progression deadline. Skip pulses whose nominal duration
plus configured command/retry allowance cannot fit. Check with a fresh clock
immediately before sending. If network timing overruns the displayed end, the
reservation/conflict guard still holds until closure is confirmed. Revalidate
reservations if duration limits or cadence change.

Reject new group windows crossing local midnight, including manual Fixed group
requests that cannot fit before midnight. Scheduled DST repeated times produce
one occurrence per local date; nonexistent times are skipped and reported.
Do not start an automatic catch-up group outside its scheduled minute. These
restrictions apply to groups; preserve existing single-valve Fixed semantics.

### Controller cadence

Align controller and relay-poll defaults and documentation with the 60-second
requirement in `AGENTS.md`. Current code/examples use 30 seconds: preserve explicit
configuration overrides and test both 30 and 60. Document that deployments
currently omitting these settings must explicitly pin them to 30 before upgrade
if they want unchanged cadence. No faster polling for group handovers.

## 8. UI, documentation, and research boundaries

- Keep one schedule editor/calendar with Fixed and Smart labels. Display one
  event for a group, ordered member names, and its full reservation. Preserve
  existing single-valve event behavior; edit/copy/delete links must distinguish
  storage types without ambiguous IDs.
- Curve page: document units and all settings, fallback selection, coverage
  days, calibration, capacity limits, and the examples above. Show the same
  temperature/demand source used in execution and capacity per configured valve.
- Dashboard/logs: show group progress, cancellation, fallback/data quality,
  skipped/zero-demand outcomes, nominal/estimated delivery, and unmet targets.
  Retain historical charts and ordinary valve/manual controls.
- Update README for configuration, calibration, Fixed compatibility, grouped
  execution, timing, restart behavior, and TrueNAS persistence. Synchronize
  `.env.example` cadence defaults; no new environment variables are planned.
  Keep dependency files in sync if a justified change becomes necessary.

Research informs the scope, not universal watering promises:

- Established-lawn consumption around 2–3 mm/day at 20–25 °C daily maximum and
  4–7 mm/day at 30–35 °C supports the current curve's rough scale, not its exact
  sigmoid, p90 input, or a universally optimal two-day schedule.
  [LWG: Basiswissen Rasenbau](https://www.lwg.bayern.de/mam/cms06/landespflege/dateien/basiswissen_rasenbau.pdf)
- Measure each zone using catch containers: average depth in mm divided by
  elapsed hours gives mm/hour. Runtime caps depend on the site and sprinkler
  output; the hardware ceiling is not a recommended watering duration.
  [CSU: Methods to Schedule Home Lawn Irrigation](https://extension.colostate.edu/resource/methods-to-schedule-home-lawn-irrigation/)
- Rotation does not guarantee absorption time for one-valve/short groups.
  No soak-delay setting is included in this release; sites requiring guaranteed
  soak need that separately designed before using Smart. Two attempts is an
  operating limit, not an agronomic optimum.
  [CSU: Watering Efficiently](https://extension.colostate.edu/resource/watering-efficiently/)
- Crediting total precipitation approximates root-available water; runoff and
  drainage can reduce it. Avoid an invented efficiency multiplier.
  [FAO: Rainfall and Evapotranspiration](https://www.fao.org/4/r4082e/r4082e05.htm)
- Open-Meteo provides model estimates and preceding-hour precipitation totals
  including snow. Do not claim rain-gauge accuracy. Cold-season controls and
  soil/ET modelling are outside this release.
  [Open-Meteo API documentation](https://open-meteo.com/en/docs)
- The existing 3276-second whole-second limit comes from `0x7FFF * 100 ms`.
  Preserve active-high flash-on addresses `0x0200..0x0207`, active-low flash-off
  addresses `0x0400..0x0407`, and normal close commands.
  [Waveshare relay protocol](https://www.waveshare.com/wiki/Modbus_POE_ETH_Relay)

## 9. Implementation order and acceptance tests

1. **Baseline and retirement:** run the existing suite; add Dynamic-to-Fixed
   migration/normalization tests; remove random behavior; preserve Fixed and
   hardware regression tests. Align/document cadence defaults explicitly.
2. **Models and calculations:** add shared group configuration/occurrences,
   calibration snapshots, weather provenance/freshness, and pure Smart balance
   helpers. No group actuation at this stage.
3. **Editor and preview:** mode-first editor, ordered membership, copy/load,
   conversion, calendar reservation/conflict validation, curve explanations,
   warnings, and previews. New Smart members require valid watering rates;
   missing rates on existing members cause individual skips, not group failure.
4. **Controller integration:** shared Fixed/Smart sequencing, durable claims,
   fresh closure confirmation, cancellation, conflict guards, daily Smart attempt
   accounting, Fixed group manual requests, and restart interruption.
5. **Release verification:** full Django tests, deterministic multi-day sequence
   simulations, SQLite/Postgres migration checks, and final documentation. Keep
   live hardware commissioning separate from tests; preserve one controller
   during upgrade and migrate before starting the upgraded controller.

Required regression coverage:

- Dynamic migration across all schedule/enabled states changes only the mode;
  history and running pulse durations survive. Residual stored Dynamic displays,
  edits, copies, schedules, and runs manually as Fixed at its stored maximum.
  No Dynamic UI choice, random duration, or mode-triggered weather request remains.
- Unknown modes and misrouted Smart modes never actuate as Fixed. Existing Fixed
  rules retain IDs/configuration and single-valve behavior through upgrade.
- Fixed groups run once in order, with per-member durations, no weather/calibration
  requirement, correct reservation, and controller-owned Run now. Smart groups
  rotate twice at most, with partial final pulses and fulfilled-valve skipping.
- Daily examples `4/0`, `6/2`, and `6/6`; rain-only skipping; temperature changes;
  `L = 1`, `L = 2`, longer valid windows, and invalid fractional days; excluded
  weekdays; first activation; credit expiry; different valve application rates.
- Midnight attribution, daily-start boundaries, DST gaps/folds/23/25-hour days,
  incomplete rain, future timestamps, unknown provenance, sparse/stale temperature,
  API outage/recovery, and refresh throttling. Reject non-finite/invalid inputs.
- Complete rainfall backfill for a seven-day coverage window and after increasing
  coverage, even when newer observations already exist; repair missing/untrusted
  interior hours without unthrottled decision-time requests.
- Rate snapshots survive calibration edits. Known delivery never includes a
  delayed controller bookkeeping interval; uncertain delivery is visibly distinct
  and is not silently zeroed. Uncalibrated legacy history stays unknown.
- Duplicate ticks/requests, copies, active-schedule changes, failures, and restarts
  cannot create a third Smart attempt per valve/date or replay an uncertain pulse.
  Scheduled zero-demand decisions are idempotent and auditable.
- Failures before/after command transmission and database writes, failed closure,
  stale cached state, cancellation during opening, disabled devices/rules,
  deadline exhaustion, midnight rejection, active reservation overruns, and site
  isolation. Planner errors leave existing stops/watchdog operational.
- A manual pulse crossing the Smart decision cutoff causes a recorded skip,
  not a deferred launch with stale credit; a pulse finishing before admission
  within the scheduled minute is credited through its end. Race group admission
  against manual and single-valve Fixed starts in both orders on SQLite and
  Postgres; only compatible admissions reach hardware. Recover attempted
  PLANNED/FAILED rows with stale closed caches and subsequently disabled devices
  without replay.
- Group/single-rule conversion and copy/load preserve intended ownership/order;
  deletion preserves history. Conflict guards work in both directions while
  unrelated existing single-valve behavior remains unchanged.
- Existing manual open/close, scheduled-minute duplicate prevention, active-high/
  active-low frames, disabled unbounded opening, duration rejection, completed
  stops after redundant-close failures, and watchdog recovery remain covered.
- Validate migrations/constraints on both SQLite and Postgres and timing at both
  30 and 60 seconds. No tests contact real irrigation hardware or live weather.

Earlier planning baseline: 43 existing irrigation/weather tests passed in the
`rainwise` environment using Django's temporary test DB and mocked hardware and
weather. This is a starting baseline, not validation of the new implementation.
Run the relevant tests after implementation changes and the full suite before
handoff; do not preserve old assertions requiring random Dynamic behavior.

```sh
env POSTGRES_HOST='' SQLITE_PATH='' RELAY_SIMULATOR=false \
  conda run -n rainwise python manage.py test apps.irrigation apps.weather --verbosity 1
```

The explicit empty database environment values select the configuration expected
by the default-SQLite warning test; Django creates its own temporary test DB.
The service tests mock the hardware transport even with simulator mode disabled.

## 10. Implementation and verification record (2026-09-21)

All five implementation phases are complete. The implementation retains the
existing hardware driver and introduces no dependencies. Shared group services
own durable planning, atomic per-site admission, sequential execution,
cancellation, and recovery. The shared editor, calendar, preview, curve page,
dashboard, logs, admin paths, and documentation cover both storage types.

- Baseline before edits: all 43 existing irrigation/weather tests passed.
- Final complete Django suite: all 145 tests passed on isolated SQLite with
  controller/poll cadence explicitly set to 60 seconds.
- Final complete Django suite: all 145 tests passed on disposable PostgreSQL
  17.11 with controller/poll cadence explicitly set to 30 seconds. Its temporary
  server was shut down after verification.
- Fresh migrations passed on both SQLite and PostgreSQL. The suite also tests
  forward/reverse migration preservation, database constraints, and admission
  races using independent connections. Migration drift and whitespace checks
  pass.
- End-to-end simulations using recorded delivery reproduce the approved
  `4/0/4/0`, `6/2/6/2`, and `6/6/6/6` sequences for both valves. Acceptance tests
  also cover weather provenance/backfill, DST, actual decision cutoffs, calibrated
  and uncertain credit, snapshots, UI conversion/copy/cancellation, durable
  claims, failures around hardware calls/database writes, and restart recovery.
- Independent implementation/review findings were resolved, including repeated
  valve passes, pre-upgrade running pulses without attempt metadata, startup
  recovery failure, queued request snapshots, and fresh closure confirmation
  after a failed redundant close. Uncertain commands retain the approved full
  nominal conservative credit even when that allowance extends past a cutoff;
  this is explicitly reported, while known delivery remains cutoff-bounded.

No implementation phase remains incomplete. Browser interaction and live
hardware commissioning were not performed; automated UI validation uses Django
requests/rendering, and hardware/weather are mocked throughout. No live
controller was launched, production data changed, or deployment performed.
Deployment still requires a database backup, stopping the old controller,
applying migrations before starting exactly one upgraded controller, and
measured application rates for valves selected into Smart. The fallback now
defaults to 25 °C as specified in the user-approved follow-up below.
Pin both cadence settings to 30 before upgrading if the old cadence is desired;
otherwise the defaults are 60 seconds. See README for the operational steps.


## 11. User-approved follow-up: defaults and missing valve rates (2026-09-21)

The user explicitly revised the earlier no-fallback-default requirement:
Docker clients should update the image without adding configuration. This
follow-up is complete with the following behavior:

- Fallback temperature defaults to 25 °C and remains editable on the curve page.
  Forward migrations fill only missing fallback values and supply default
  curve settings for existing sites without a row; existing overrides survive.
  New sites also use standard curve defaults and 25 °C without requiring a
  settings-page visit. This is an application/model default, not a new Docker
  variable. Numeric overrides, including 0 °C, remain valid.
- The existing nullable `Valve.application_rate_mm_h` migration already supplies
  the requested N/A default. Do not invent a rate or backfill historical pulse
  snapshots. Fixed/manual watering continues to accept uncalibrated valves.
- Only valves with a finite positive rate can be newly selected for Smart.
  Existing membership remains when its rate is cleared, allowing correction
  without recreating the schedule. Preserve that membership when editing or
  loading an existing schedule, and show which valves need a rate entered.
- At a scheduled decision, skip missing/invalid-rate members individually,
  preserve a visible per-valve decision reason, and plan the calibrated members
  in their normal order. If every member is unavailable, record a skipped
  occurrence, not a zero-demand result. Calendar reservations still include all
  configured maxima and handovers; missing calibration does not shrink them.
- Recheck rates before claiming and sending every Smart pulse. If a rate is
  cleared after planning, skip all unattempted pulses for that valve in that
  occurrence, keep its history/snapshots, and continue the other valves after
  confirming any prior pulse is closed. Skips consume no logical attempts.
  Already-commanded bounded pulses retain their duration, stop, and rate snapshot.
  Restoring a rate allows the next scheduled decision to include the valve;
  skipped work is not replayed during the old occurrence.
- Dashboard, editor, curve, preview, and execution history warn that affected
  valves require a watering rate and are skipped in Smart. Current warnings
  clear after a valid rate is restored; historical skip reasons remain.

Initial findings: the valve field/default migration was already present, but
fallback was nullable with no default and missing calibration invalidated the
entire Smart rule. Both behaviors have been changed by this follow-up.

Review decisions and findings:

- Migration `0008` backfills null fallbacks and missing settings rows; `0009`
  applies the non-null field and 25 °C model default. PostgreSQL testing with
  existing data caught deferred foreign-key trigger events blocking a schema
  alteration after the inserts in the same transaction. Separate ordinary
  migrations resolve that upgrade failure; both execute automatically through
  the existing Docker entrypoint.
- New-site reads use an unsaved default settings instance rather than creating
  database rows in dashboards or previews. Explicitly blank fallback input uses
  25 °C; an omitted field or resetting curve parameters preserves an existing
  fallback override, including 0 °C.
- Model/form validation rejects newly selected uncalibrated Smart valves, while
  runtime validation accepts retained members so each can be skipped separately.
  The editor checks new selections again inside the admission transaction.
- Missing and invalid rates (including non-finite values from existing/bypassed
  validation data) are unavailable. Decision/configuration JSON uses null for
  those rates so an invalid numeric value cannot prevent the other valves from
  being planned. Missing target/capacity values display N/A, not zero demand.
- A previously planned pulse skipped before transmission remains in history as
  `FAILED` with an explicit "Skipped in Smart" reason and no attempt timestamp.
  This reuses the existing pulse states, consumes no attempt, and gives no
  irrigation credit. All of that valve's pending passes are skipped together;
  restoring its rate does not resurrect them. Active pulses and saved decision,
  duration, and application-rate snapshots are untouched.
- Current warnings identify unavailable valves and clear on correction. Saved
  decision warnings and pulse skip reasons remain visible in execution history.
  Reservations continue to include every configured member's maximum duration
  and handover allowance, including members currently missing a rate.

Verification: the pre-change baseline was 145 passing tests. The final complete
Django suite passes all **172 tests** on isolated SQLite at 60-second cadence
and PostgreSQL 17.11 at explicit 30-second cadence. Populated upgrade/reverse
migration tests and standalone migration checks pass on both databases;
`makemigrations --check --dry-run` reports no changes. Hardware and weather remain
mocked, and the temporary PostgreSQL server was stopped after testing. No Docker
configuration, dependencies, hardware driver, production data, or deployment was
changed. Live hardware commissioning and browser interaction remain outside this
automated verification.
