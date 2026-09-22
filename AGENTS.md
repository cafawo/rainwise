# AGENTS

Repository-specific guidance for Codex agents working in this project.

## Core principles

- **Code style**
  - Python: follow PEP 8.
  - Prefer clarity and readability over cleverness.
  - Be consistent with the surrounding codebase if it clearly deviates from strict PEP 8.

- **Maintainability first**
  - Favor simple, explicit, and idiomatic solutions.
  - Prefer built-in defaults and standard behavior over custom logic when the result is comparable.
  - Avoid premature abstraction or generalization.

- **No hacky or over-engineered solutions**
  - “Hacky” includes:
    - Workarounds, fragile assumptions, or abuse of internals.
    - Over-engineered implementations (e.g. ~10 lines of custom logic where a built-in, default, or idiomatic 1-line solution achieves a similar result).
  - Do **not** force such implementations.
  - If a request would require a hacky or over-engineered solution:
    - **Stop** and explain why.
    - Point out the simpler default / standard library feature / framework behavior.
    - Propose adjusting the requirement to use the simpler approach.

- **When in doubt**
  - If instructions are ambiguous or likely to lead to an inferior design, ask for clarification **before** implementing.
  - If multiple reasonable approaches exist, briefly explain trade-offs and recommend one.

## Simplicity and approval boundaries

- **Default to the smallest change that delivers the requested feature.** Preserve established, working behavior. A feature request or reliability review does not authorize redesigning unrelated systems, including the controller and valve controls.
- **New database models/tables, additional persistent state, and substantive schema changes require explicit user approval before implementation.** First consider existing models, ordinary local logic, and temporary in-memory state where losing that state is acceptable. Persistence must provide substantial, concrete value that justifies its migration and maintenance cost.
- **Additional architectural complexity also requires explicit user approval before implementation.** This includes new queues, workers, coordination mechanisms, recovery protocols, state machines, dependencies, and redesigns of working subsystems. Routine local changes within an already approved design do not require repeated approval, subject to the stricter valve-control boundary below.
- **Present a concrete proposal before requesting approval.** Explain the user requirement it serves, the simplest viable alternative, why that alternative is insufficient, and the added behavior, failure modes, and maintenance cost. Generic claims such as "more robust," "safer," or "future-proof" are not sufficient justification.
- **Respect accepted failure behavior and scope.** Do not add recovery, retry, resumption, or coordination machinery to eliminate a failure mode the user has explicitly accepted. Document unrelated concerns and continue the authorized work; raise any conflict with an explicit safety requirement before changing the design.
- **Approval must cover the specific design change.** An agent-written entry in `PLANS.md`, a general instruction to implement a feature, or silence is not approval to add models or architectural complexity. Record explicit approval in `PLANS.md`; if that design was already approved, do not ask again.
- **Apply these rules to reviews as well as new code.** Existing complexity is not justified merely because it has already been implemented. Identify unnecessary additions and propose a simpler alternative; obtain approval before removals that change agreed behavior or stored data.

## Protected valve-control subsystem

- **Treat valve control as a protected critical subsystem.** Preserve established, tested behavior. Feature work, cleanup, reliability reviews and requests for production readiness do not authorize changes to it. Prefer implementing features through the existing control interface.
- **Protect the behavior, not just a filename.** Protected production files include `apps/irrigation/services.py`, `apps/irrigation/group_services.py` and `apps/irrigation/management/commands/controller.py`. Protection also covers changes elsewhere affecting opening/closing, relay identity or polarity, duration bounds/conversion, timeouts/retries, dispatch, stop/watchdog handling, cancellation, restart behavior or controller cadence. This includes shared configuration, model fields, migrations, dependencies and deployment settings when they affect those behaviors.
- **Obtain specific, informed approval before editing protected production code.** Present a concrete, minimal proposal identifying the affected files, current versus proposed behavior, the requirement it serves, the simplest alternative that preserves valve control, implications during errors/restarts, and planned validation. Explain why a control change is needed; convenience or general claims of safety are insufficient. Record the user's approval and its scope in `PLANS.md`.
- **No exemption for small changes.** Refactors, logging additions and changes described as routine or behavior-preserving still require approval when editing protected production files. A general feature approval or an agent-written plan is insufficient. Existing explicit approval for the specific change remains valid; seek renewed approval only if its scope or implications change. Read-only review, documentation and tests may proceed without additional approval; do not weaken safety assertions to hide a regression.
- **Keep feature failures from weakening valve closure.** UI, Preview, weather, reporting and other feature work must not bypass the timed-opening service, remove an issued relay timeout or suppress existing stop/watchdog handling. Do not add optional feature work as a prerequisite for closing a valve. This is a functional boundary, not an instruction to introduce new processes, queues, persistence or recovery machinery.
- **Preserve accepted failure behavior.** A failed early close may leave watering active until the relay timeout. Manual/controller ordering races remain an accepted limit. Restart abandons unfinished Smart sequences; no replay or resumption. Report unrelated concerns in `docs/ISSUES.md` and continue the authorized work. If a finding conflicts with an explicit safety requirement, explain it and pause the affected implementation for approval rather than silently redesigning control.
- **Make critical changes visible and verifiable.** For approved control changes, test the affected safety behavior with mocked hardware, including relevant failure paths and established Fixed/manual behavior. Report each protected change, its practical implications, validation and remaining limits in the final handoff; state explicitly when valve-control code was unchanged. Tests and documentation are evidence, not a guarantee that hardware cannot fail. Live relay testing requires explicit authorization.

## Critical guardrails for this project

- **Keep hardware protocol code in the service layer**
  - Django views must call a *service layer* (e.g., `apps/irrigation/services.py`) which contains the hardware logic.
  - Preserve immediate manual Open/Close and single-valve Fixed Run now through that service. This boundary does not require a command queue or moving those actions to the next controller tick.
  - Scheduled execution and watchdog logic must run in the dedicated controller process.

- **Single controller process**
  - This MVP intentionally avoids Celery/Redis.
  - A dedicated Django management command (`manage.py controller`) is the single orchestrator for:
    - starting scheduled runs
    - stopping runs (optimal/max duration)
    - existing stop/watchdog handling after errors or restarts, without resuming unfinished Smart sequences
    - periodic weather imports

- **Safety-first behavior**
  - Every opening must have a planned finite duration and use the existing relay-side timed opening: the same command both opens the valve and sets its automatic closing timeout. This applies to manual, Fixed, Smart and any future opening path, for both relay polarities.
  - Untimed/latched opening is forbidden, including as a fallback after an error. Opening first and setting a timeout with a second command is also forbidden. Validate supported finite duration bounds before sending an opening; a controller timer is never a substitute for the relay timer.
  - Once the relay accepts a timed opening, timeout closure must not depend on the web app, controller, database, weather, logs or network remaining available. Preserve the existing watchdog as an additional safeguard, not the basis of this timeout.
  - This protection assumes correctly configured and functioning relay/valve hardware. Do not claim software can guarantee closure despite a failed relay or mechanically stuck valve, or that mocked tests prove physical operation.

- **Low resource usage is a hard requirement**
  - Avoid busy loops; controller must sleep and run on a 60-second cadence by default.
  - Use conservative network timeouts/retries for Modbus and weather calls.
  - Avoid high-frequency polling, heavy background processing, or unnecessary dependencies.
  - Avoid excessive DB writes (e.g., don’t write status every loop unless it changed).

- **Robustness on TrueNAS / Docker**
  - Containers can restart at any time; the controller must be idempotent and safe to restart.
  - Never start the same scheduled invocation twice. Intended repeated Smart pulses are separate runs, not duplicate invocations. Preserve the approved deduplication and missed-minute behavior; this rule does not authorize new persistent execution state or catch-up/recovery machinery.
  - Production data must live on a mounted volume or in Postgres (documented in README).

## Platform constraints (Docker on TrueNAS)

- Production runs as Docker containers on TrueNAS SCALE.
- Persist state under a mounted volume (e.g., `/data`) when using SQLite in containers.
- Avoid assumptions requiring systemd/cron on the host.

## Planning workflow (PLANS.md)

- For non-trivial changes, draft or update `PLANS.md` before implementing.
- Treat `PLANS.md` as the current source of truth for design and behavior.
- Do not silently diverge from an approved plan; update `PLANS.md` if direction changes.

## Tests

- Add or update tests when changes affect behavior or logic.
- Prefer small, focused Django tests.
- Always run the relevant test suite after changes. If tests cannot be run, explain why and what would be run in a normal environment.
- If tests cannot be added within scope:
  - Explain why.
  - Suggest concrete next steps for test coverage.

## Documentation

- Update `README.md` when behavior, APIs, configuration, or setup changes.
- Keep dependency specifications in sync (`requirements.txt`, 'environment.yml').
- Update `.env.example` with all environment variables.

## Environment (development)

- Local dev may use a Conda environment named `rainwise`, but production is Docker.
- Keep workflows Docker-first compatible, but do not require Docker for local development.
- Run project commands inside the `rainwise` environment.
