# Fixed and Smart: minimal implementation

Updated 2026-09-24. This supersedes the persisted occurrence/sequence design.
The user approved removing RuleOccurrence and the admission counter, keeping
Smart execution in memory, reducing warnings/weather repair, and applying
configuration edits on the next execution. Preserve v0.1.4 valve controls.

## Unreleased rule-editor cleanup

Requested on 2026-09-24: remove the Run now button from the rule editor for a
future release. Remove its template markup and unused editor URL context; retain
Smart Preview, manual dashboard controls and the existing single-valve POST
endpoint. No controller, hardware, model or migration changes. Leave the
published v0.1.5 tag unchanged and do not publish this change yet.
Implemented; all 58 editor tests and whitespace checks passed. Protected valve
control code is unchanged.

## Protected valve-control instructions

Requested by the user on 2026-09-22: strengthen `AGENTS.md` so that changes to
valve control require specific, informed approval, including small changes and
indirect effects through shared code or configuration. Two independent read-only
reviews identified gaps in the existing approval, timed-opening, manual-service,
restart and deduplication wording. This update changes instructions only; it
does not authorize a controller redesign or alter existing approved behavior.
Preserve relay-timed openings, accepted failure limits and prior specific
approvals. Review the resulting instructions for loopholes and consistency;
no runtime changes or hardware tests are part of this documentation task.
Both reviewers checked the final instructions and found no remaining actionable
gaps. Whitespace validation passed; application tests were not rerun for this
documentation-only change. No valve-control code was changed in this task.

## Approved addition: run appendices and JSON downloads

Approved by the user on 2026-09-22: add exactly one persistent field,
`IrrigationRun.appendix = JSONField(default=dict, blank=True, editable=False)`,
and its additive migration. The user explicitly selected this schema change
and then requested implementation of the complete logging/export plan.

- Preserve the in-memory controller, attempted-only history, and existing
  restart, hardware, scheduling, and deletion behavior. The appendix is
  descriptive history and must never drive execution or recovery.
- Capture versioned rule/site/valve context for new attempts and recovery logs.
  Smart appendices freeze effective settings, accepted weather samples with
  provenance, per-valve credit contributions, demand/results/warnings, and
  pulse metadata from the actual admission calculation. Fixed/manual records
  retain basic context. Old records receive an empty appendix, without backfill.
- Hold per-valve evidence in the existing transient group; attach it when an
  attempted run is inserted, before hardware I/O. Each pulse is self-contained;
  omit other valves' evidence, full sequences, and prior runs' appendices.
  Preview stays read-only and does not collect detailed evidence by default.
- Provide authenticated `/logs/export/` and `?extended=1` JSON downloads for all
  recorded selected-site history. Use explicit fields and streaming iteration;
  extended output adds the appendix. Include export metadata and the existing
  nominal/estimated delivery with uncertainty, using the export time cutoff.
- Defer appendices on routine UI/history/controller reads and refresh only
  operational fields during group ticks. No new workers, tables, dependencies,
  settings, per-tick writes, or decision-time network requests.
- Test frozen evidence, fallback/zero/DST/uncertainty, failed and recovery runs,
  legacy rows, site isolation, all-history export, strict JSON, and lightweight
  routine reads. Run relevant/full Django tests and migration/system checks in
  the rainwise environment with mocked hardware. Document format and limitations
  in README. Zero/skipped decisions and abandoned future pulses stay unrecorded.

Safety review requested by the user: keep each export database read bounded and
fully consumed before yielding to the HTTP client. The original iterator held a
SQLite read cursor across client waits, demonstrably blocking a second writer in
the default rollback-journal mode. Use small descending-ID queryset batches;
preserve streaming and site scope without a new worker or database setting.
Keep recovery closure before bookkeeping, and catch recovery-context/logging
errors per valve so optional diagnostics cannot skip later watchdog closures.
Add failure tests for pre-open capture/INSERT errors, post-open bookkeeping
errors, continued stop/watchdog phases, and concurrent export/controller writes.
The independent finite relay timer remains the physical stop mechanism. Shared
database/disk failures can still delay software actions; this feature does not
claim complete availability isolation or physical relay commissioning.

The export correction is implemented. On 2026-09-22, after the guard's behavior
and lack of schema changes were explained, the user approved proceeding with
the recovery-log exception guard already implemented in the controller. Its
scope is limited to catching recovery-context/log-write errors after a close
attempt so the watchdog continues checking the remaining valves. Relay commands,
durations, retries and restart behavior are unchanged. No additional model,
migration or controller redesign is authorized; no deployment was changed.

Review follow-up approved on 2026-09-22: omit appendix data from ordinary admin
history queries using Django's standard deferred-field support, and align the
upgrade note with migration 0014. These changes are implemented.
Validation: all 42 focused admin, export and appendix/failure tests passed in
the rainwise environment. Independent diff review and whitespace checks passed.
This follow-up changed no protected production code, models or migrations.

Implemented with migration `0014_irrigationrun_appendix`. Validation: all 265
Django tests passed in the rainwise environment on isolated SQLite, including
the populated appendix upgrade and logging-failure/concurrent-export tests;
model-drift and system checks passed.
The suite uses mocked relay transport (`RELAY_SIMULATOR=false` allows existing
frame-level tests to exercise their fake sockets). Test log:
`/private/tmp/rainwise-logging-safety-full-tests.log`. Independent review found
no additional actionable defects in the safety corrections. The recovery-log
guard changes error handling only; commands, durations and restart behavior are
preserved. No live hardware was used and no existing deployment database was
migrated.

## Existing behavior

- Every opening uses the existing relay command with its finite timeout.
  No untimed opening, new protocol, command queue, worker or dependency.
- Keep immediate manual Open/Close and the existing single-valve Fixed Run now endpoint through services.
  Preserve the original Fixed/manual stop, polling and watchdog behavior.
- Fixed rules selected as due by a tick remain due even if an earlier relay call
  crosses the minute boundary. Restore v0.1.4 behavior here.
- One controller, normally every 60 seconds; explicit 30-second settings work.
- Manual/controller ordering races and failed early closes running until the
  relay timeout are accepted limits. Do not add coordination machinery for them.

## Configuration and watering history

- Keep legacy ScheduleRule IDs/settings. Interpret DYNAMIC as Fixed at its saved
  runtime. Expose only Fixed and Smart.
- Keep GroupedRule and ordered GroupedRuleValve configuration: these represent
  the requested multi-valve feature without restructuring legacy Fixed rules.
- Keep measured valve rates, curve coverage/fallback settings, and rate/attempt
  snapshots needed to estimate actual water delivery.
- Remove RuleOccurrence, its run relationship, persisted pass/order fields,
  sequence cancellation flags and Site.admission_version. Do not replace them
  with another state table, JSON cursor, queue, lock counter or rule fields.
  Use ordinary atomic configuration saves.
- Distinguish attempted group pulses using the existing IrrigationRun.trigger
  field. Create logs only for attempted pulses, never future planned pulses.
- Preserve attempted/finished watering records during forward migration. Remove
  unattempted development-plan rows and occurrence-only decision history.
- Keep applied migrations unchanged; add forward cleanup for released and
  development databases. Stop old processes and let timed watering finish before
  upgrading. No migration may actuate hardware or replay watering.

## Smart calculation and calendar

- Coverage is 1–7 local days, default 2; old deficits expire.
- Daily need uses the existing temperature curve. Missing/unusable temperature
  falls back to the configured value, default 25 °C; a valid 0 °C remains valid.
- target_mm = max(0, coverage_days * daily_need - rain_credit - irrigation_credit).
  Translate mm to whole watering seconds through the measured rate.
- Rain uses completed hours across the coverage window. Irrigation includes the
  preceding coverage-minus-one local dates plus earlier today. Preserve DST-aware
  boundaries, finite inputs and conservative uncertain-delivery credit.
- Catch-up may exceed daily curve peak. No extra daily cap or two-run limit.
- Split each target into bounded runs using the saved per-valve limit. Iterate
  rounds in valve order. Before repeating a valve, rest at least its previous
  commanded duration; other valves' watering contributes to that rest.
- Share the bounded pure sequence calculation with Preview and peak calendar
  reservations, including breaks and ordinary controller/command allowance.
  Reject group reservations beyond midnight or overlapping automatic windows.
  Preserve existing independent Fixed overlaps.
- New Smart members require measured positive rates. Retained missing-rate
  members skip on the next invocation; do not invent calibration.
- Keep valve duration defaults and rule overrides, compact ordered editor,
  red linked validation errors and feedback above headings.

## In-memory execution

- A controller-owned GroupRunner holds active sequences by site: frozen valve
  objects/rates, finite remaining pulses, deadline, current run and rest times.
  Advance on normal ticks; never sleep while waiting for a valve's rest.
- Edits to rules, rates, durations, ordering and curve settings take effect next
  time. Running sequences keep their admitted definition. Do not reconcile
  every pulse against partially edited limits/order.
- Disabling/deleting a rule or changing the active schedule stops future pulses
  and attempts an ordinary early close of its current pulse. Failed early close
  may finish at the relay timeout. Disabled relays prevent new openings.
- Restart discards unfinished sequences. Do not reconstruct or resume them.
  Attempted logs remain; original relay timers/watchdog still apply.
- Deduplicate scheduled starts using actual attempted logs and in-memory dates
  already considered. Never replay a recorded attempt or backfill missed minutes.
  Repeated DST times share one logical scheduled start. Zero/skipped decisions
  need no persistent record.
- Group completion/rest uses command-return time plus commanded duration as the
  conservative relay deadline. No closure acknowledgement, recovery protocol or
  persistent progress cursor is required.
- Only the controller sequences automatic groups. Web manual controls cannot
  observe an in-memory reservation during a break. Manual intervention remains
  an accepted limitation, documented rather than hidden behind a new lock.

## Website and weather

- Remove occurrence history, detailed group progress and persisted zero/skip
  decisions. Actual watering logs remain; Smart Preview remains.
- Disable a group by clearing Enabled in the rule editor and clicking Save,
  saving existing enabled=False. No separate Disable rule button is needed.
  The controller stops the sequence on its next tick. Future scheduled
  executions remain disabled until Enabled is selected and saved again.
- Remove Run now from the rule editor; keep the existing single-valve endpoint
  for compatibility. Grouped Run now remains removed. Groups run on schedule.
- Show one useful fallback-temperature warning. Do not run watering planners
  merely to produce dashboard warnings. Detail belongs in Preview/logs.
- Remove the unused old editor form and coordination-only Admin restrictions.
- Keep successful-import freshness and elapsed-observation provenance.
- Refresh weather on normal cadence with existing retry throttling. Do not
  repair individual archive gaps or refetch a month for one missing observation.
  Initial import backfills history/coverage; later refreshes update the recent
  temperature/coverage window. Retain old history. Missing rain gives no credit.
- No additional settings or dependencies.

## Verification and unresolved issues

- Test the Fixed minute-boundary regression against released behavior.
- Test ordered rounds, rests, edits applying next run, disable/delete, restart
  abandonment/dedup, midnight bounds and exclusively timed hardware commands.
- Test populated v0.1.4/development upgrades preserving configuration and actual
  watering, while removing obsolete execution state.
- Test editor/feedback simplifications and ordinary weather refresh cadence.
- Run focused/full suites on isolated SQLite and PostgreSQL, model/system
  checks, and browser checks where available. No production hardware I/O.
- Document remaining issues in docs/ISSUES.md with priority, impact, accepted
  boundary and smallest possible future remedy. Do not implement optional
  remedies in this pass. Accepted limits are not release blockers.

## Verification completed

- 230 tests passed on SQLite at 60-second cadence (7.522 s) and PostgreSQL
  17.11 at 30-second cadence (8.257 s). Logs:
  /tmp/rainwise-memory-sqlite-final.log and /tmp/rainwise-memory-postgres.log.
- Populated release/development upgrades and fresh migrations through 0013 pass.
  Model-drift, Django system, dependency and whitespace checks pass.
- Independent review verified the Fixed minute-boundary behavior, exclusively
  timed relay protocol, frozen sequences, restart abandonment and deduplication.
  Two local lifecycle/dedup defects found during review were corrected and tested.
- Isolated Chromium passed desktop and 390-pixel checks for compact editing,
  preserved invalid input and red errors, warning placement, Preview, Disable rule,
  removed occurrence/group Run now UI, and immediate timed manual controls.
  No JavaScript errors or page overflow. Artifacts: /tmp/rainwise-memory-browser/.
- Temporary PostgreSQL/browser services are stopped. Tests used mocked hardware
  and disposable data. Docker CLI is unavailable; no container build or live-relay
  check was performed. No production deployment or release tag was created.
- Remaining concerns and accepted limits are triaged in docs/ISSUES.md. They are
  not authorization to add further infrastructure.
