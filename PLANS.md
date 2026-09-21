# Implementation plan: reliable Fixed and Smart rules

Updated 2026-09-21 after implementation review and the user's editor/timing
feedback. This is the current design and supersedes the earlier two-pass plan.
The existing implementation is the baseline, not a completed implementation of
this revision. Only this planning document is being changed at this stage.

Confirmed follow-up decisions:

- Keep the day-based rolling balance and allow coverage-window catch-up above
  the curve's daily maximum. Do not introduce a second daily-water cap.
- Remove the fixed two-run limit. Split a finite calculated target into as many
  bounded runs as necessary, with breaks and a same-day reservation.
- Before repeating a valve, require a break at least as long as its preceding
  run. Other valves' watering counts toward that break; wait only the remainder.
  Apply this same rule to every Smart sequence without a separate break setting.
- Use the valve's existing duration setting as the default for a new rule's
  duration; allow an explicit rule override.
- Improve validation, explanations, defaults, and the editor's appearance.

The user has approved the minimum-break rule. No timing clarification remains;
the revised design is ready for implementation in the order given in section 7.

## 1. Compatibility and safety contract

Keep Fixed and Smart as the only modes. Fixed waters each selected valve once
for its explicit runtime. Smart calculates a water target for each separate
zone and divides it into bounded runs in valve order.

- Keep the existing Modbus driver, framing, polarity, retries, close/read
  services, and hardware-timed pulses. Never issue an unbounded opening or add
  an application-level opening to extend an active run. Existing bounded
  transport retries remain; they can restart a relay timer and must retain the
  conservative uncertain-delivery accounting described below.
- Every opening has a durable intended duration and maximum. The independent
  relay limit remains an integer 1–3276 seconds per physical command. Invalid
  values fail before hardware I/O; they are not silently clamped.
- Preserve existing single-valve Fixed rule IDs, runtimes, schedules, history,
  manual operations, and scheduled-minute duplicate prevention. Its stored
  `max_duration_seconds` is an implementation name: label it Runtime in the UI.
  Avoid a cosmetic database-field rename or migration.
- Existing stored `DYNAMIC` rules migrate/normalize to Fixed at their stored
  duration. Preserve IDs, disabled/inactive configuration and old run history.
  Keep the narrow stored-value compatibility path; do not expose Dynamic or
  reintroduce its random duration/weather-fetch behavior. Unknown modes and
  Smart routed into the single-valve Fixed path must fail without actuation.
- Keep one controller process, the normal 60-second cadence, explicit cadence
  overrides, and conservative network timeouts. No Celery, Redis, extra worker,
  high-frequency polling, or new dependency is needed.
- Views call services. Group sequencing, waiting, stops, weather imports and
  recovery belong to the controller. Cached weather is used during planning;
  no decision-time weather request is added.
- Group/planning errors must not prevent existing stops and watchdog work.
  Preserve authentication, site isolation, UTC storage, site-local scheduling,
  Docker/TrueNAS persistence and the `rainwise` Conda development environment.
- Configuration edits must not rewrite an already-commanded pulse, its rate
  snapshot, intended duration, original stop, or historical decision.

## 2. Settings, defaults and clear terminology

There are two user concepts, with different units and purposes:

| Concept | Source | Meaning |
| --- | --- | --- |
| Peak daily demand | Existing `CurveSettings.max_mm`, mm/day | Upper end of the temperature curve. It bounds daily estimated need, not the total catch-up watering in a multi-day window. |
| Run time before a break | Existing valve duration setting, copied into a Smart rule's member duration | Longest uninterrupted watering for that valve in this rule. More runs may follow after a break. |

The relay's 3276-second command ceiling is an independent technical safety
constraint, not another editable watering target.

### Fixed

- Show **Runtime**. This is the actual duration of its one run, not an optimal
  duration plus a separate maximum. Keep existing 60–3276-second validation.
- When selecting a valve for a new member, prefill its runtime from
  `Valve.default_max_duration_seconds`. The user can change it.
- Fixed needs no calibration, curve, or weather settings. Do not introduce a
  valve-default ceiling on Fixed rule runtimes.

### Smart

- Show **Run time before a break**. Prefill from the selected valve's existing
  `default_max_duration_seconds`, with a short explanation of the default.
- The override is optional: blank means use the selected valve's current
  default at save time. Resolve this on the server as well as in the browser.
  Blank never means an unbounded pulse.
- Save the resolved positive integer in the existing member duration field.
  Existing rules and copies keep their saved durations. Later edits to a valve
  default affect new selections, not existing rules or active occurrences.
- A rule override may be below or above the valve default, within 1–3276 seconds.
  Remove the earlier Smart validation that treated the valve default as a
  second absolute ceiling. The effective saved rule value is the per-run limit.
  Reducing that rule limit still blocks incompatible pending pulses.
- Keep manual Open's existing use of the valve duration setting. Do not change
  its bounded-command behavior while clarifying the default's Smart use.
- Preserve overrides and invalid submitted input after validation errors.
  Auto-fill only new/pristine selections; do not overwrite edited values during
  mode changes or page rerendering. Provide an explicit Use valve default action.
- If a valve default is invalid for the chosen mode, explain the allowed range
  beside the field; do not silently increase or clamp it.

Keep existing finite curve validation (`0 <= min_mm <= max_mm`, positive `g`,
finite `m`), integer coverage days 1–7 with default 2, editable fallback default
25 °C, and finite positive measured application rates. Preserve 0 °C overrides.
Do not add a separate daily-mm maximum, editable daily total runtime, cycle
count, or independently configured soak duration.

## 3. Rule editor and validation

Use existing Bootstrap styles, spacing, cards, controls and button conventions.
No visual framework, drag-and-drop library, wizard or new scheduling UI is
needed. Keep Mode first, followed by ordered valves and then the schedule.

- Put each valve selector, its mode-specific duration, sequence controls and
  Remove action together in one responsive row/card. Remove the distant second
  duration list, raw-looking controls, and large numbered instruction blocks.
- Make order easy to understand. Small accessible up/down controls can use the
  existing formset order field; retain straightforward keyboard operation.
- Present enabled state, weekdays, start time and optional name/note compactly.
  Smart initially selects all seven weekdays; both modes require at least one.
- Only the site's active schedule executes automatically. Reject duplicate
  members and valves from another site. A valve may belong to at most one
  enabled Smart rule per schedule; independent Fixed/manual watering still
  contributes irrigation credit. Members represent separate watering zones,
  not overlapping areas sharing one water allocation.
- Keep the principal Save and Cancel actions clear. Preview is for Smart;
  Fixed retains Run now. Stop and Delete must remain distinguishable actions.
- Use short mode-specific help. Fixed shows its runtime. Smart shows the valve
  rate, per-run limit, peak watering time for the selected coverage window,
  and the meaning of automatic breaks. Do not show two editable maxima in the
  rule editor or label Fixed's runtime as a maximum.
- Use seconds internally and keep supported precision. Display human-readable
  durations alongside inputs and in summaries; do not introduce ambiguous
  duration parsing or silently round existing values.

### Failed saves must explain what to fix

Server-side Django validation remains authoritative. On an invalid submission:

1. Rerender the bound form with a prominent red summary: **Rule was not saved.
   Correct the highlighted fields.** Include editor, formset management,
   formset-wide, member-field and service/model errors.
2. Link each summary item to the affected field or valve row. Use red invalid
   borders and visible inline messages, `aria-invalid` and `aria-describedby`.
   Color alone is insufficient. Focus the summary after the response so a user
   returned to the top immediately understands the failure.
3. Say how to recover: Select a valve or remove this row; Enter a start time;
   Enter a runtime in the permitted range; Select at least one weekday; This
   valve is already selected; This reservation overlaps the named rule/time.
4. Attach valve and duration validation to the appropriate member field when
   possible. Keep genuinely rule-wide errors in the summary. Do not flatten
   everything into an unexplained generic failure.
5. Preserve all submitted rows, values, order, mode and weekdays. JavaScript
   must not clear a submitted valve just because its rate is invalid or missing.
   Newly unavailable Smart valves can remain visibly invalid with instructions
   to enter a rate or choose another valve.
6. Every deliberately added, nondeleted row must be completed or explicitly
   removed. Do not silently ignore an empty added row. A deleted row's missing
   or malformed values must not block the remaining valid form.

Use `novalidate` on this dynamic form and rely on the visible Django error
presentation, so hidden/deleted numeric fields cannot block Save with an
invisible browser validation message. Invalid input must never write partial
configuration or report success. The basic error explanation must work without
JavaScript; JavaScript improves focus, defaults and ordering only.

## 4. Smart balance and weather

Retain the approved finite rolling window, whole local dates and immutable
admission decision. Older deficits expire; there is no cumulative debt ledger.
At the actual admitted instant `t`, for coverage `L` and valve rate `q` mm/hour:

```text
daily_need = curve(selected_temperature_at_t)
target_mm = max(0, L * daily_need - rain_credit_mm - irrigation_credit_mm)
total_seconds = floor(target_mm * 3600 / q)
peak_seconds = floor(L * curve.max_mm * 3600 / q)
```

Use finite validated arithmetic and stable whole-second flooring. A zero-second
result creates no pulse. Remove `2 * per_run_cap` as a total capacity limit;
the per-run setting splits the target and does not independently cap its volume.
Daily demand never exceeds the curve maximum, but the rolling target can.
Do not add another daily cap or silently truncate a target to two runs.

### Accounting and data quality

- Rain covers `L` local-day periods ending at the most recent completed provider
  hourly boundary at/before the decision. Use preceding-hour totals and no
  invented partial-hour rain. At 06:30, whole-hour data ends at 06:00.
- Irrigation credit includes the preceding `L - 1` local calendar dates and
  earlier delivery today. Count calibrated Fixed, manual and Smart runs, split
  delivery at boundaries, and exclude late bookkeeping time from known delivery.
  With `L = 1`, only earlier watering today is credited.
- Construct boundaries locally and query in UTC, preserving 23/25-hour DST
  dates. Wednesday in the two-day scheme credits Tuesday and earlier Wednesday,
  not Monday's watering just after the scheduled time.
- Use the admitted instant, not an earlier controller-loop timestamp. A pulse
  completed earlier in the same scheduled minute counts. A conflicting ongoing
  pulse causes a recorded skip; do not wait and start a stale frozen target.
- Keep temperature selection from the preceding 24-hour p90, at least 18 finite
  trusted hourly values, and freshness relative to the weather refresh interval.
  Otherwise use the configured fallback, normally 25 °C, with a visible reason.
- Weather values must be elapsed, with retrieval provenance at/after their
  valid time. Refresh legacy/untrusted rows before using them. Refresh based on
  successful imports and retry throttling, not the newest observation timestamp.
- Backfill the complete required window, including interior gaps, increased
  coverage and default settings on new sites. Use `get_curve_settings()` for
  effective coverage even when the settings instance is not saved.
- Unknown/nonfinite/negative rain provides no credit and produces a separate
  incomplete-rain warning. Do not describe missing weather as proof of no rain.
- Preserve rate snapshots on all new calibrated watering. Legacy uncalibrated
  history remains unknown. Known early closure shortens estimated delivery;
  uncertain openings retain conservative nominal credit, known retry interval
  and an explicit warning about unknown physical delivery.
- Preserve past warnings/decisions while clearing resolved current warnings.
  Open-Meteo values remain model estimates, not rain-gauge measurements.

### Updated examples

With two coverage days, no rain or previous credit, sufficient time in the day,
and no failed/uncertain watering, constant needs of 2, 4 and 7 mm/day produce
initial targets of 4, 8 and 14 mm respectively, followed by zero the next day.
Subsequent decisions repeat the corresponding large/zero pattern while those
conditions remain constant. The previous 6/2 and 6/6 examples depended on the
retired two-run capacity ceiling and are no longer required outcomes.

At 7 mm/hour, a 7 mm target takes one hour of actual watering. The peak target
for a 7 mm/day curve with two coverage days is 14 mm and takes two hours.
Neither figure includes breaks or scheduling allowance. Preview should show
actual calculated demand separately from the calendar's peak reservation.

## 5. Sequential watering, breaks and calendar reservations

The same minimum-break rule applies to single-valve and grouped Smart rules,
including different valve durations, zero demand and members skipped for
missing calibration.

### One deterministic sequence

- Fixed groups make one pass in selected order, with no soak requirement after
  their final/only runs. Existing single-valve Fixed execution stays separate.
- Smart freezes each member's total seconds and resolved per-run limit. Split
  it into full capped runs and a shorter final run where needed.
- Execute round one in valve order, then subsequent rounds in the same order;
  skip fulfilled/unavailable valves. Remove the fixed two-attempt counter and
  any UI text promising exactly/up to two passes.
- Before repeating a valve, require an off-time at least as long as its preceding
  commanded run. Other valves' elapsed watering contributes to that break.
  Maintain order; if the next valve is not ready, wait the remainder.
- Example: A waters 30 minutes, B waters 5, then wait 25 before A repeats. If A
  is the only eligible valve, water 30, rest 30, then water 30. No rest is needed
  after the final pulse merely to complete an occurrence.
- Start the break from confirmed closure. Persist or derive eligibility from
  the previous run's stable closure timestamp and duration. Repeated fresh reads
  must not keep moving that timestamp and extending the break forever.
- Waiting is durable controller state, never a blocking sleep in a service or
  request. On normal ticks, check cancellation, recovery and eligibility. Keep
  the site reservation while resting; display Resting and the next eligible time.
- Equal-duration breaks are an explicit product rule, not a guarantee of soil
  absorption under all conditions. No additional agronomic model is introduced.

### Peak calendar uses the same sequence calculation

Derive each calibrated valve's peak seconds from section 4, split them using
its saved rule limit, and simulate the same ordered sequence and breaks without
hardware or database side effects. Do not keep `2 * sum(member maxima)`.

The same deterministic sequence helper should supply preview/planning and the
peak reservation. Larger per-valve targets must not shorten the peak envelope.
Requiring a minimum break on every repeat preserves that property when another
member has zero demand or is skipped. Conditional breaks only when no other
valve runs do not: A30/B1/A30 takes 61 minutes but A alone needs 90 minutes.

Show **Watering**, **Breaks**, and **Scheduling allowance** separately. The event
spans their total; its duration does not shrink with today's rain or demand.
For one valve, a one-hour peak with a 30-minute run limit needs 90 minutes before
allowance. A two-hour peak needs four runs and three breaks: 210 minutes.

Use a conservative derived allowance, not another setting. For a peak plan:

```text
K = number of pulses
R = number of repeat pulses (K minus valves with a nonzero peak)
I = configured controller interval
A = configured command/retry allowance
scheduling_allowance = (K + R + 1) * I + K * A
reserved_time = peak_sequence_elapsed_time + scheduling_allowance
```

For zero pulses, reserve no watering duration or allowance. For grouped Fixed,
use its one-pass sequence (`R = 0`); account for initial admission and closure
instead of assuming the controller launches exactly on the scheduled second.
Keep the single-valve Fixed event equal to its configured runtime.
The allowance covers nominal tick/command timing; it is not a guarantee against
arbitrary outages. Preserve fresh deadline checks and hold reservations until
closure is confirmed even after the displayed end.

### Bounds and changing configuration

- Validate peak totals and integer pulse counts before constructing pulse lists
  or database rows. Reject a peak watering/allowance lower bound already beyond
  the available local day; stop the small sequence simulation at midnight.
  Tiny positive rates or one-second caps must not cause unbounded allocations.
- Reject new/edited group reservations crossing local midnight or overlapping
  another automatic rule at that site. Keep unrelated single Fixed overlaps.
  Show which reservation conflicts and how to change it.
- Recalculate on changes to rate, curve/coverage, duration, order or cadence.
  Validate at save/activation where applicable and always before admission.
  Existing conflicting configurations must not start silently.
- Already-admitted occurrences keep their frozen target and reservation.
  Safety-relevant reductions/cancellations can stop pending work, never extend
  an active pulse or add newly eligible work to the frozen plan.
- Keep one scheduled occurrence per rule/local date, one event on a repeated
  DST local time, visible skips for nonexistent local times, and no automatic
  catch-up outside the scheduled minute. Construct actual elapsed ends in UTC.

### Missing calibration and peak reservations

New Smart selections require a finite positive measured rate. Retained members
that lose their rate stay editable, visibly N/A, and skip individually. An
already-started run keeps its original rate snapshot and bounded duration.

A flow-derived peak cannot be computed for an uncalibrated member. Therefore
this revision deliberately replaces the previous no-shrink calendar rule:
compute the peak from currently calibrated members and clearly identify the
omitted/unavailable members. If all are unavailable, show a scheduled start
marker with duration N/A and record a skipped occurrence, not zero demand.
Do not invent a rate or store a second hidden calibration/default just to size
an event. Restoring a rate recomputes the envelope and requires conflict checks
before future admission; it never adds pulses to an old skipped occurrence.

## 6. Durable execution and release-blocking fixes

Keep existing group models and individual `IrrigationRun` pulse records.
`GroupedRuleValve.duration_seconds` stores Fixed runtime or Smart per-run limit;
optional Smart input does not require nullable/live-inherited storage.

Scheduled occurrences remain unique by rule/local date; manual Fixed requests
have no scheduled date and deduplicate while pending/active. Pulse identity is
occurrence/valve/round, now permitting more than two rounds. Preserve historical
occurrence snapshots and existing legacy trigger/idempotency semantics.

Fixed group Run now remains controller-owned and requires an enabled rule in
the active schedule. Reject a conflict at submission; a conflict arising before
admission terminates the request visibly rather than queueing a later surprise
start. Smart remains scheduled-only, with Preview and Stop rather than an
unscheduled watering override.

Snapshot the calculation, rate, rule limit, pulse budget, time zone, and sequence
policy once. Historical two-pass occurrences keep their recorded meaning; do
not reinterpret old history or add runs to it. On restart, cancel unfinished
group sequences and reconcile attempts rather than resuming/replaying them.

- Share atomic per-site admission between groups and manual/scheduled single
  openings on SQLite and Postgres. Commit a claim before hardware I/O; never
  hold the database transaction across the network call.
- Admit scheduled work only when conflicts, in-flight openings and uncertain
  closures have been resolved. Otherwise record a skip, not a delayed launch.
- Before each run check cancellation, active schedule, enabled rule/device,
  ownership, saved/current compatible per-run limits, calibration, conflicts
  and a fresh deadline including command allowance.
- Every admitted sequence has a finite persisted pulse budget. Copies and
  schedule changes use the shared recorded irrigation credit; they must not
  replay uncertain or already-attempted work. No new daily ledger is needed.
- Before advancing or beginning rest, obtain fresh closure confirmation. A
  FINISHED row or cached valve state alone is insufficient. Ambiguous opening
  or unconfirmed closure interrupts remaining work and retains the reservation.
- Inspect attempted PLANNED/FAILED as well as RUNNING records during recovery.
  Disabled relay devices still require necessary closure/read operations.
- Stop rule, closing any member, disabling/deleting a group, or switching its
  active schedule cancels remaining work. Persist cancellation through an
  in-flight opening, recheck after it returns, and show Stopping until safe.
- Mode/membership conversion requires no active run/reservation, preserves
  history, and remains transactional. Schedule copy/load and site ownership
  checks apply consistently to both rule storage types.

### Concrete review findings to fix before rollout

1. **Restart versus an in-flight manual opening.** Recovery currently can
   confirm closure before a surviving web request's opening reaches hardware,
   release its claim, and admit a group. Keep cancellation/ownership until the
   outstanding sender is acknowledged or safely reconciled; a pre-send closed
   read is not final confirmation. Do not retain an obsolete closure timestamp
   when a late opening result is processed. Add deterministic interleaving tests.
2. **Stale single-rule admission.** Reload the scheduled Fixed rule inside the
   admission transaction and verify that it still exists, is enabled/due, belongs
   to the active schedule/site, and has the intended current valve/configuration.
   A deleted or Smart-converted rule must not execute from a cached object.
   Preserve deliberate legacy manual Run now semantics separately.
3. **Watchdog versus an in-flight opening.** Recognize valid recent committed
   opening attempts within their bounded recovery interval. Do not close them
   as unexpected merely because their status is still PLANNED, then record a
   full successful delivery. Preserve genuine orphan-open recovery.
4. **Default coverage in weather backfill.** Use the effective settings helper
   for new sites without a stored curve row, including one-day import settings
   with the default two-day Smart coverage.
5. **Site ownership on creation.** A new site must not accept another site's
   active schedule. Enforce the same-site invariant in model/admin validation.

These are fixes to existing reliability, independent of the editor redesign.
They are not optional polish and must remain covered during sequencing changes.

## 7. Implementation order and acceptance checks

1. Fix the three execution races with isolated regression tests on SQLite and
   Postgres. Fix effective weather defaults and site ownership.
2. Improve editor structure, defaults and validation, preserving existing saved
   settings and safe transactional configuration updates.
3. Introduce the shared bounded sequence/peak calculation, revised Smart pulse
   splitting, durable rests, and calendar/conflict validation together. Update
   all model/service/UI checks that assumed two attempts or a valve-default
   absolute ceiling; avoid leaving contradictory enforcement paths.
4. Update previews, curve explanations, dashboard/resting status, logs, README
   and rollout notes. No new dependency or environment variable is planned.
5. Run the complete suites, migration/drift checks, deterministic timing cases,
   and browser interaction checks with mocked hardware and weather.

Required regression coverage includes:

- All three reproduced races in both relevant orderings, including controller
  restart while the web process survives, conversion/disable/delete/schedule
  change between rule selection and admission, and watchdog polling while an
  opening awaits acknowledgement. Test genuine orphan recovery too.
- Fixed runtime labels/defaults, optional Smart overrides, overrides above/below
  the valve default, server fallback without JavaScript, and preservation of
  existing/copied/invalid submitted values. No migration rewrites old durations.
- Blank valve, incomplete added row, missing start time/weekdays, invalid/out-of-
  range duration, duplicate valve, missing calibration, missing management data,
  overlapping window, and invalid deleted row. Assert red summary, linked inline
  errors, highlighted fields, preserved input and no partial save.
- A real browser pass for add/remove/reorder, mode changes, default filling,
  failed-submit focus, hidden-row validation, narrow layout, Preview and Save.
  Django response tests alone do not exercise these interactions.
- Single/multiple valves with one, two and more than two runs; partial final
  runs; zero demand; unequal rates/caps; a short intervening valve; another
  valve skipped or fulfilled; all members unavailable; rate removal/restoration.
- One-hour target/30-minute cap gives 30 water + 30 rest + 30 water. Two-day
  7 mm/day peak at 7 mm/hour gives two hours of watering, four 30-minute runs,
  three breaks and the derived scheduling allowance.
- Peak reservation bounds every reduced per-valve demand, including zero or
  missing members, at both 30- and 60-second cadences. Fresh closure delays,
  late ticks and command delays cannot make early rest eligibility or unsafe
  progression; exhausted deadlines produce a visible unmet target.
- Rest timestamps do not slide on every poll. Cancellation and restart during
  rest preserve ownership/recovery and never replay a sequence.
- Finite arithmetic, tiny rates/caps, zero curve maximum, same-day envelope
  rejection before large allocation, and curve/rate/cadence changes causing
  configuration conflicts. Calendar and planner use the same units/bounds.
- Preserve day-based credit expiry, admitted cutoffs, uncertain delivery,
  midnight/DST handling, weather provenance/backfill/fallback, rate snapshots,
  Dynamic-to-Fixed migration, disabled-device closure, ownership, history, and
  unchanged hardware frames and bounded commands.

Use isolated databases and mocks; never launch a live controller or operate
production valves for automated verification. Typical SQLite command:

```sh
env POSTGRES_HOST='' SQLITE_PATH='' RELAY_SIMULATOR=false \
  CONTROLLER_INTERVAL_SECONDS=60 RELAY_POLL_INTERVAL_SECONDS=60 \
  conda run -n rainwise python manage.py test --verbosity 1
```

Use disposable PostgreSQL for the equivalent full suite and concurrency tests.
Update documentation when behavior changes. Preserve one controller during
upgrade, back up the persistent database, stop old execution and migrate before
starting the upgraded controller. Explicit 30-second cadence overrides remain
supported; otherwise both controller and relay poll defaults are 60 seconds.

## 8. Baseline and verification history

The baseline implementation at `1ed66ba` contains the original Fixed/Smart
release and the approved 25 °C fallback/missing-rate follow-up. Its migrations
normalize Dynamic, preserve run history, add default settings for existing sites,
and retain the nullable valve rate. Separate fallback backfill/schema migrations
avoid the PostgreSQL deferred-trigger upgrade problem.

The latest independent review reran all 172 tests successfully on isolated
SQLite at 60 seconds and disposable PostgreSQL 17.11 at 30 seconds. Fresh
migrations and model-drift checks passed. The three execution races above were
then reproduced on both databases despite that green suite. This is baseline
verification, not evidence that this new revision is implemented or release-ready.

Additional pure calculation checks found no ordinary rolling-balance defect.
The approved minimum-break/peak-planner approach was checked with exhaustive
small and randomized sequences; implementation still needs the acceptance tests
above. No live hardware commissioning or browser interaction was performed
in that review. Preserve the existing weather/relay assumptions documented in
README; these changes add no claim of measured rain or guaranteed soil absorption.
