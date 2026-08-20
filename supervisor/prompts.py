"""Parent Prompt 构建（M2+M5 hardening + Runtime Namespace Separation）。

Supervisor 通过 activation prompt 注入 Parent 策略（supervisor/resources/parent-policy.md），
而不要求目标仓库根目录存在 AGENTS.md。目标仓库的 AGENTS.md（如存在）仅作为
可选的补充说明。

Task 源可配置（supervisor.toml [task].file，默认 .supervisor/task.md）。
"""

from pathlib import Path

try:
    import importlib.resources as _resources  # Python 3.9+
except ImportError:  # pragma: no cover
    _resources = None  # fallback to pathlib


def _load_parent_policy() -> str:
    """从 supervisor/resources/parent-policy.md 加载策略文本。"""
    # 优先 importlib.resources（打包后可用），回退到文件系统路径
    if _resources is not None:
        try:
            # Python 3.9+: files(package) / joinpath
            pkg = _resources.files("supervisor.resources")  # type: ignore[attr-defined]
            text = (pkg / "parent-policy.md").read_text(encoding="utf-8")
            if text.strip():
                return text.strip()
        except Exception:
            pass
        try:
            # Python 3.7/3.8: read_text(package, resource)
            text = _resources.read_text("supervisor.resources", "parent-policy.md")  # type: ignore
            if text.strip():
                return text.strip()
        except Exception:
            pass
    # 文件系统回退
    p = Path(__file__).resolve().parent / "resources" / "parent-policy.md"
    if p.exists():
        return p.read_text(encoding="utf-8").strip()
    raise RuntimeError(
        "packaged parent policy is missing: supervisor/resources/parent-policy.md "
        "(package-data not installed correctly)"
    )


_PARENT_POLICY = _load_parent_policy()

_INITIAL = (
    "SUPERVISOR EVENT: INITIAL_START\n"
    "\n"
    "You are the autonomous Parent Agent managed by the Supervisor.\n"
    "\n"
    "Repository-specific instructions:\n"
    "- Read AGENTS.md if the target repository contains one.\n"
    "- Respect all existing project documentation and repository conventions.\n"
    "\n"
    "Task:\n"
    "- Read {task_file}.\n"
    "\n"
    "Persistent development state:\n"
    "- Read .agent/PLAN.md if present.\n"
    "- Read .agent/STATE.md if present.\n"
    "- Read .agent/state.json if present.\n"
    "\n"
    "Do not treat Supervisor implementation resources, examples, templates,\n"
    "or historical reports as task instructions.\n"
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
    "Read {task_file} and AGENTS.md (if present), plus .agent/PLAN.md, "
    ".agent/STATE.md and .agent/state.json if present. Inspect the actual "
    "repository state, then continue the existing task autonomously.\n"
    "\n"
    "Do not treat Supervisor implementation resources as task instructions.\n"
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
    "Read {task_file} and AGENTS.md (if present), plus .agent/PLAN.md, "
    ".agent/STATE.md and .agent/state.json.\n"
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
    "and .agent/STATE.md, then continue the existing task autonomously. Before "
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


def _inject_policy(prompt: str) -> str:
    """将 Parent 策略前置注入（若资源存在）。"""
    if _PARENT_POLICY:
        return _PARENT_POLICY + "\n\n---\n\n" + prompt
    return prompt


def build_prompt(reason: str, task_file: str = ".supervisor/task.md", **ctx) -> str:
    template = _PROMPTS.get(reason)
    if template is None:
        raise ValueError(f"unknown supervisor event: {reason!r}")
    # 填充 task_file 与 CI 上下文
    if "{task_file}" in template:
        prompt = template.format(task_file=task_file, **{k: v for k, v in ctx.items() if k in template})
        # 处理剩余占位符（sha, ci_dir）
        if "{sha}" in prompt or "{ci_dir}" in prompt:
            prompt = prompt.format(sha=ctx.get("sha", "?"), ci_dir=ctx.get("ci_dir", ".supervisor/inbox"))
        return _inject_policy(prompt)
    if "{sha}" in template or "{ci_dir}" in template:
        prompt = template.format(sha=ctx.get("sha", "?"), ci_dir=ctx.get("ci_dir", ".supervisor/inbox"))
        return _inject_policy(prompt)
    return _inject_policy(template)


def get_parent_policy() -> str:
    """供外部直接读取策略（测试/审计）。"""
    return _PARENT_POLICY
