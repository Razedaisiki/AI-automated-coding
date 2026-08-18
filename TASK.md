# TASK: Autonomous Development Supervisor (V1)

## Objective

Implement the Supervisor system designed in `AI_automated_coding.md` — a
deterministic process manager that keeps a DSH Parent Agent working on a
software-development task safely, continuously and recoverably.

> Supervisor 决定 WHEN。Parent Agent 决定 HOW。

## Scope of this round (Milestones M0–M5)

1. **M0** — Freeze the protocol: `docs/supervisor-protocol.md`,
   `supervisor.toml`, `.agent/state.schema.example.json`.
2. **M1** — Storage + Lock: `config.py`, `models.py`, `storage.py`,
   `events.py`, `lock.py` (atomic writes, JSONL event log, exclusive lock).
3. **M2** — `DshRunner` + `parent-once` CLI: spawn
   `dsh --profile headless "<prompt>"` in the repo, capture logs, timeout,
   kill the whole process group (SIGTERM → grace → SIGKILL).
4. **M3** — Local Supervisor FSM loop: INITIAL_START, RUNNING/COMPLETED/
   BLOCKED dispatch, exit0+RUNNING → next activation, exit!=0+RUNNING → crash
   restart.
5. **M4** — Limits / timeout / backoff: max activations, crash restarts, clean
   restarts, timeouts, active wall time; `STOPPED_LIMIT` with fixed stop
   reasons; exponential-style backoff.
6. **M5** — Supervisor restart recovery: runtime restore, recorded-PID/process
   identity check, orphan Parent adoption (never start a second Parent),
   operator SIGTERM handling.

Out of scope this round (later milestones): GitHub CI integration (M7),
human gate (M8), token/cost metering, fake/real CI pollers.

## Acceptance

- All milestone tests green (mostly via `ParentRunner` fakes — zero LLM runs).
- `python -m supervisor init/run/status/events/parent-once/stop/resume` work
  as specified in the design doc.
- First-phase success criterion (per design doc §五十三):
  you can kill a Parent, or kill the Supervisor itself, restart the
  Supervisor, and it still resumes the task from
  `.agent/state.json` + Git state without corrupting the repo or spawning a
  duplicate Agent.