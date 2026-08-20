> **Historical artifact** — archived from `.agent/` at Runtime Namespace Separation.
> Not a live task file. See `docs/supervisor-protocol.md §0` for ownership.

# .agent/WORKFLOW.md — Autonomous Development State Machine

## Objective

Execute every development request using the following state machine.

Continue automatically from one state to the next unless:

* a genuine blocker exists;
* permission-rules requests approval;
* an irreversible human decision is required;
* the workflow reaches `READY_FOR_HUMAN_REVIEW`.

---

## State 0 — PREFLIGHT

Actions:

1. Read:

   * `AGENTS.md`
   * `TASK.md`
   * `.agent/WORKFLOW.md`
   * existing `.agent/PLAN.md`
   * existing `.agent/STATE.md`
2. Inspect:

   * repository root;
   * git status;
   * current branch;
   * available build/test/lint/typecheck commands;
   * relevant project documentation.
3. Detect existing user changes.
4. Determine whether this is:

   * a new run;
   * a resumed run.

Output:

`STATE = ANALYSIS`

---

## State 1 — ANALYSIS

Determine:

* exact requested behavior;
* acceptance criteria;
* affected modules;
* compatibility constraints;
* risks;
* likely tests;
* external dependencies.

Use read-only subagents in parallel when they can independently investigate useful areas.

Do not modify code before sufficient understanding exists.

Output:

`.agent/PLAN.md`

Then:

`STATE = PLANNING`

---

## State 2 — PLANNING

Break the work into bounded tasks.

Each task records:

* ID;
* description;
* dependencies;
* file/module ownership where predictable;
* acceptance condition;
* status.

Example:

```text
T1 READY       Analyze authentication flow
T2 BLOCKED     Implement refresh token storage — depends T1
T3 READY       Design API tests
T4 PENDING     Integration review — depends T2,T3
```

Resolve dependency state.

Tasks whose dependencies are satisfied become `READY`.

Then:

`STATE = IMPLEMENTATION`

---

## State 3 — IMPLEMENTATION

While unfinished tasks remain:

1. select `READY` tasks;
2. determine whether they can safely run concurrently;
3. delegate suitable work to subagents;
4. mark delegated tasks `IN_PROGRESS`;
5. Parent continues independent useful work while background agents execute;
6. collect results only when necessary;
7. inspect actual resulting changes;
8. integrate safely;
9. mark successfully completed tasks `DONE`;
10. recalculate `READY` tasks.

Parallel editing is allowed only for clearly disjoint ownership.

When all implementation tasks are `DONE`:

`STATE = PARENT_REVIEW`

---

## State 4 — PARENT_REVIEW

Parent reviews the complete implementation.

Check:

* requirement coverage;
* diff scope;
* API compatibility;
* duplicated logic;
* architecture consistency;
* error handling;
* security-sensitive behavior;
* missing tests;
* accidental generated files;
* unrelated modifications.

If material defects exist:

return to:

`STATE = IMPLEMENTATION`

Otherwise:

`STATE = LOCAL_VALIDATION`

---

## State 5 — LOCAL_VALIDATION

Run applicable validation:

```text
targeted tests
    ↓
format check
    ↓
typecheck / compile
    ↓
lint
    ↓
build
    ↓
broader tests
```

Record every executed command and result in `.agent/STATE.md`.

If all pass:

`STATE = INDEPENDENT_REVIEW`

If anything fails:

`STATE = AUTO_REPAIR`

---

## State 6 — AUTO_REPAIR

For the failure:

1. capture exact command and output;
2. classify:

   * product bug;
   * implementation bug;
   * test bug;
   * dependency problem;
   * environment problem;
   * infrastructure problem.
3. investigate root cause;
4. optionally start a fresh debugging subagent;
5. implement the smallest justified repair;
6. rerun the failing command;
7. rerun checks affected by the repair.

Increment:

`local_repair_attempts`

If resolved:

return to:

`STATE = LOCAL_VALIDATION`

If the same underlying failure reaches 3 attempts:

`STATE = BLOCKED`

---

## State 7 — INDEPENDENT_REVIEW

Use an independent review pass.

Run `doublecheck`.

When appropriate, also use a fresh subagent whose job is only to challenge the implementation.

Reviewer should inspect:

* correctness;
* edge cases;
* security;
* regression risk;
* test adequacy;
* unnecessary complexity;
* requirement mismatch.

Reviewer must not modify code unless explicitly delegated a repair.

If blocking findings exist:

`STATE = REVIEW_REPAIR`

Otherwise:

`STATE = GIT_PREPARE`

---

## State 8 — REVIEW_REPAIR

Convert valid review findings into implementation tasks.

Repair.

Run:

* relevant tests;
* affected static checks;
* `doublecheck` again.

Increment:

`review_repair_attempts`

Maximum for one underlying problem:

`3`

When clean:

`STATE = GIT_PREPARE`

When unresolved:

`STATE = BLOCKED`

---

## State 9 — GIT_PREPARE

Inspect:

```bash
git status
git diff
```

Verify:

* no unrelated files;
* no secrets;
* no unexpected generated artifacts;
* all intended files included.

If repository has no git configuration or committing is not part of the task:

skip to `FINALIZE`.

Otherwise create the intended commit.

Then:

`STATE = REMOTE_CHECK`

---

## State 10 — REMOTE_CHECK

Determine whether all are true:

* remote exists;
* pushing is required;
* credentials are available;
* permission-rules allows the action.

If not:

`STATE = FINALIZE`

If yes:

push the branch.

Then:

`STATE = CI`

---

## State 11 — CI

If CI tooling is available, observe the latest run.

If CI succeeds:

`STATE = PR`

If CI fails:

1. inspect failed checks;
2. inspect logs;
3. determine whether failure is reproducible locally;
4. repair;
5. rerun full relevant local validation;
6. commit;
7. push;
8. recheck CI.

Increment:

`ci_repair_attempts`

Maximum automatic repair rounds for the same root cause:

`3`

If CI remains broken:

`STATE = BLOCKED`

---

## State 12 — PR

If the workflow requires a Pull Request and tooling is available:

create or update it.

PR description must include:

* task objective;
* implementation summary;
* validation results;
* significant design decisions;
* known risks.

Then:

`STATE = READY_FOR_HUMAN_REVIEW`

Do not merge automatically.

---

## State 13 — READY_FOR_HUMAN_REVIEW

Stop autonomous execution.

Present:

* PR status;
* validation status;
* CI status;
* unresolved risks.

Wait for human:

* approval;
* requested changes;
* merge instruction.

If changes requested:

`STATE = IMPLEMENTATION`

If explicit merge approval is received and permissions allow:

perform the approved merge operation.

---

## State 14 — FINALIZE

Re-read every acceptance criterion from `TASK.md`.

Create:

`.agent/FINAL_REPORT.md`

Update:

`.agent/PLAN.md`

Update:

`.agent/STATE.md`

Final state must be one of:

```text
COMPLETED
READY_FOR_HUMAN_REVIEW
BLOCKED
```

---

## BLOCKED State

Stop only when continued autonomous work cannot reasonably progress.

Write:

* blocker;
* exact evidence;
* affected task;
* attempts made;
* safest recommended next action;
* exact human decision or input required.

Do not label an ordinary engineering problem as a blocker merely because the first approach failed.
