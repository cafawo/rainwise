# Remaining issues after the Smart simplification

Reviewed 2026-09-22. This is a triage list, not an implementation mandate.
Optional changes below require approval under AGENTS.md. Do not turn accepted
limits into queues, coordination protocols or additional execution models.

Priority means: P1 = a demonstrated defect needing correction before release;
P2 = a relevant operational limitation or remaining verification;
P3 = optional convenience or an infrequent edge case.

## Corrected in this pass

- Fixed rules already selected by a tick no longer disappear when an earlier
  relay command crosses the minute boundary. The v0.1.4 behavior is restored.
- Dashboard warning rendering no longer plans watering or produces false late-night
  reservation errors. There is one useful fallback-temperature warning.
- A missing old weather observation no longer triggers full-archive repair.
- Occurrence storage, precreated future pulse logs, cancellation state and the
  admission counter are removed. Smart sequences live in controller memory.
- Rule/rate/curve edits apply to the next invocation; current sequences keep their
  original definition. No partial live configuration reconciliation remains.
- Deleting an active valve/run abandons its sequence instead of leaving its site
  permanently blocked in memory.
- Group start deduplication uses site and scheduled instant, so changing membership
  and restarting in the same minute does not start that execution again.

No unresolved P1 defect was found in the final scoped code review. Automated
checks do not establish physical relay or production-container behavior.

## Open items

| Priority | Item and practical effect | Current boundary / smallest future action |
| --- | --- | --- |
| P2 — verification | Docker image build and live relay behavior have not been exercised in this environment. | Docker CLI is unavailable. Timed command framing/polarity/bounds/retries are tested with simulated transport. Before rollout, an ordinary deployment smoke check can verify container startup, persistence and relay timer expiry. No new safety subsystem is proposed. |
| P2 — accepted limit | Manual actions and the controller can issue commands concurrently. Manual watering during a Smart break is also possible because the web process does not share the controller's memory. It can shorten watering, overlap an already issued command, or add water after Smart froze its target. | Every opening remains relay-timed. Disable the group before deliberately overriding it manually. Keep ordinary checks; do not add a shared lock or command queue. |
| P2 — accepted limit | Controller downtime or a stalled tick can miss a start or leave a target partly watered. | Abandon unfinished sequences on restart and never backfill missed minutes. Existing relay timers end issued pulses. There is no proposed recovery/resumption work. |
| P2 — accepted limit | Simultaneous configuration saves are ordinary transactions, without global admission serialization. Conflicting settings can be saved concurrently and then rejected when execution validates them. | Normal save validation and execution-time reservation validation remain. Revisit only if concurrent editing causes real trouble; no lock counter or new model is proposed. |
| P3 — control semantics | Close affects the current valve pulse. During a Smart break there is no running pulse to cancel, so Close alone does not stop later rounds. | Use Disable rule. It also disables future scheduled executions until explicitly re-enabled. Allow a controller tick to observe disabling before re-enabling. This avoids a separate cancellation channel. |
| P3 — expected uncertainty | Missing rain supplies no credit; estimated rates and uncertain transport delivery are not physical flow measurements. This can change the calculated water dose. | Show calculation detail in Preview and actual run logs. Weather refreshes on normal cadence; one missing observation does not justify archive repair or a new alert system. |
| P3 — removed convenience | Grouped Run now, detailed group progress and saved zero/skip decisions are unavailable. | Groups execute on their schedule. Single-valve Fixed Run now and actual watering logs remain; Preview explains current Smart demand. Controller logs report skips. Add a web/controller handoff only if this convenience becomes worth its complexity and is explicitly approved. |
| P3 — existing administration limit | Valve deletion can cascade its watering logs; changing physical relay identity while watering can make historical/current hardware references confusing. | Stop/disable watering before changing hardware configuration or deleting valves. This behavior is not being redesigned as part of Smart. |

## Upgrade boundary

Back up the database, stop old web/controller processes and let timed watering
finish before applying migrations through 0013. The forward cleanup deliberately
removes occurrence-only history and never-attempted future pulse rows. It preserves
rule configuration and actual/attempted watering records. Do not rewrite migrations
already applied to a user's database merely to reduce the migration-file count.
