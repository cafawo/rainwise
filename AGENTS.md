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
- **Additional architectural complexity also requires explicit user approval before implementation.** This includes new queues, workers, coordination mechanisms, recovery protocols, state machines, dependencies, and redesigns of working subsystems. Routine local changes within an already approved design do not require repeated approval.
- **Present a concrete proposal before requesting approval.** Explain the user requirement it serves, the simplest viable alternative, why that alternative is insufficient, and the added behavior, failure modes, and maintenance cost. Generic claims such as "more robust," "safer," or "future-proof" are not sufficient justification.
- **Respect accepted failure behavior and scope.** Do not add recovery, retry, resumption, or coordination machinery to eliminate a failure mode the user has explicitly accepted. Document unrelated concerns and continue the authorized work; raise any conflict with an explicit safety requirement before changing the design.
- **Approval must cover the specific design change.** An agent-written entry in `PLANS.md`, a general instruction to implement a feature, or silence is not approval to add models or architectural complexity. Record explicit approval in `PLANS.md`; if that design was already approved, do not ask again.
- **Apply these rules to reviews as well as new code.** Existing complexity is not justified merely because it has already been implemented. Identify unnecessary additions and propose a simpler alternative; obtain approval before removals that change agreed behavior or stored data.

## Critical guardrails for this project

- **Never perform Modbus/hardware I/O in HTTP request/response code**
  - Django views must call a *service layer* (e.g., `apps/irrigation/services.py`) which contains the hardware logic.
  - Scheduled execution and watchdog logic must run in the dedicated controller process.

- **Single controller process**
  - This MVP intentionally avoids Celery/Redis.
  - A dedicated Django management command (`manage.py controller`) is the single orchestrator for:
    - starting scheduled runs
    - stopping runs (optimal/max duration)
    - failsafe closures and recovery after restarts
    - periodic weather imports

- **Safety-first behavior**
  - Any valve opening must always have:
    - a planned stop (optimal duration), and
    - a hard stop (max duration) that always closes the valve even after errors.
  - Include watchdog logic to close valves that appear open unexpectedly.

- **Low resource usage is a hard requirement**
  - Avoid busy loops; controller must sleep and run on a 60-second cadence by default.
  - Use conservative network timeouts/retries for Modbus and weather calls.
  - Avoid high-frequency polling, heavy background processing, or unnecessary dependencies.
  - Avoid excessive DB writes (e.g., don’t write status every loop unless it changed).

- **Robustness on TrueNAS / Docker**
  - Containers can restart at any time; the controller must be idempotent and safe to restart.
  - Never create duplicate scheduled runs for the same valve + scheduled minute.
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
