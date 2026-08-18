"""Parent Prompt 构建（M2）。

Supervisor 只发送控制协议，不含开发逻辑（怎么 test/review/修代码由
AGENTS.md 决定）。只设置 durable boundary 的契约。

事件：INITIAL_START / CONTINUE / RECOVER_AFTER_PARENT_CRASH /
      RECOVER_AFTER_PARENT_TIMEOUT / CI_FAILED / CI_SUCCEEDED /
      HUMAN_APPROVED / HUMAN_CHANGES_REQUESTED
"""

_INITIAL = (
    "SUPERVISOR EVENT: INITIAL_START\n"
    "\n"
    "You are the Parent Agent responsible for the software-development task "
    "in this repository.\n"
    "\n"
    "Read:\n"
    "- TASK.md\n"
    "- AGENTS.md\n"
    "- .agent/plan.md if it exists\n"
    "- .agent/state.json if it exists\n"
    "\n"
    "Inspect the actual repository state.\n"
    "\n"
    "You own all software-engineering decisions and execution.\n"
    "The Supervisor only manages your process lifecycle and does not decide "
    "how implementation should be performed.\n"
    "\n"
    "Continue autonomously until you reach a durable boundary.\n"
    "\n"
    "Before ending this activation, atomically update .agent/state.json "
    "(write tmp, flush, rename).\n"
    "\n"
    "Valid durable statuses are:\n"
    "RUNNING | WAIT_CI | WAIT_HUMAN | COMPLETED | BLOCKED\n"
    "\n"
    "If more development work remains and you choose to end this activation, "
    "write RUNNING.\n"
    "If remote CI must complete before useful work can continue, write WAIT_CI "
    "and record the exact commit SHA.\n"
    "If human action is required, write WAIT_HUMAN.\n"
    "Only write COMPLETED when the development task is actually complete.\n"
    "Only write BLOCKED when further autonomous progress is genuinely impossible."
)

_CONTINUE = (
    "SUPERVISOR EVENT: CONTINUE\n"
    "\n"
    "The previous Parent activation ended normally but the task is not yet in "
    "a stable terminal state.\n"
    "\n"
    "Read TASK.md, AGENTS.md, .agent/plan.md and .agent/state.json, inspect "
    "the actual repository state, then continue the existing task "
    "autonomously.\n"
    "\n"
    "Before ending, atomically checkpoint .agent/state.json with a strictly "
    "incremented checkpoint_seq."
)

_RECOVER_CRASH = (
    "SUPERVISOR EVENT: RECOVER_AFTER_PARENT_CRASH\n"
    "\n"
    "The previous Parent Agent terminated unexpectedly.\n"
    "\n"
    "Do not restart the development task from scratch.\n"
    "\n"
    "Read TASK.md, AGENTS.md, .agent/plan.md and .agent/state.json.\n"
    "Inspect the actual repository state, including: current branch, HEAD, "
    "git status, working-tree changes, existing commits.\n"
    "Persisted agent state may lag behind the repository because the previous "
    "process terminated unexpectedly. Reconcile the documented state with the "
    "actual repository.\n"
    "\n"
    "Continue the existing task autonomously. Before ending, atomically "
    "checkpoint .agent/state.json."
)

_RECOVER_TIMEOUT = (
    "SUPERVISOR EVENT: RECOVER_AFTER_PARENT_TIMEOUT\n"
    "\n"
    "The previous Parent Agent was terminated by the Supervisor because it "
    "exceeded the per-activation time budget.\n"
    "\n"
    "It may have left uncommitted working-tree changes or a partially written "
    ".agent/state.json.\n"
    "\n"
    "Inspect the actual repository state, reconcile it with .agent/state.json "
    "and .agent/plan.md, then continue the existing task autonomously. Before "
    "ending, atomically checkpoint .agent/state.json."
)

_CI_FAILED = (
    "SUPERVISOR EVENT: CI_FAILED\n"
    "\n"
    "The external CI run for commit {sha} failed.\n"
    "Supervisor-collected CI material is available under: {ci_dir}\n"
    "\n"
    "Resume the existing development task. You are responsible for "
    "determining the cause and deciding how to fix it."
)

_CI_SUCCEEDED = (
    "SUPERVISOR EVENT: CI_SUCCEEDED\n"
    "\n"
    "External CI for commit {sha} succeeded.\n"
    "\n"
    "Resume the existing task and continue from the persisted state."
)

_HUMAN_APPROVED = (
    "SUPERVISOR EVENT: HUMAN_APPROVED\n"
    "\n"
    "A human approved the current state. Resume the existing task."
)

_HUMAN_CHANGES_REQUESTED = (
    "SUPERVISOR EVENT: HUMAN_CHANGES_REQUESTED\n"
    "\n"
    "A human requested changes. Inspect the feedback recorded by the human, "
    "then resume the existing task."
)

_PROMPTS = {
    "INITIAL_START": _INITIAL,
    "CONTINUE": _CONTINUE,
    "RECOVER_AFTER_PARENT_CRASH": _RECOVER_CRASH,
    "RECOVER_AFTER_PARENT_TIMEOUT": _RECOVER_TIMEOUT,
    "CI_FAILED": _CI_FAILED,
    "CI_SUCCEEDED": _CI_SUCCEEDED,
    "HUMAN_APPROVED": _HUMAN_APPROVED,
    "HUMAN_CHANGES_REQUESTED": _HUMAN_CHANGES_REQUESTED,
}


def build_prompt(reason: str, **ctx) -> str:
    template = _PROMPTS.get(reason)
    if template is None:
        raise ValueError(f"unknown supervisor event: {reason!r}")
    if "{sha}" in template or "{ci_dir}" in template:
        return template.format(sha=ctx.get("sha", "?"), ci_dir=ctx.get("ci_dir", ".supervisor/inbox"))
    return template