"""Supervisor CLI（M3/M4/M5）。

用法：
    python -m supervisor init [repo]
    python -m supervisor run [repo]
    python -m supervisor parent-once [repo] [--prompt TEXT]
    python -m supervisor status [repo]
    python -m supervisor events [repo] [--tail N] [--event NAME]
    python -m supervisor stop [repo]
    python -m supervisor resume [repo] --event HUMAN_APPROVED
"""

import argparse
import asyncio
import json
import os
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .config import ConfigError, default_config, load_config
from .dsh_runner import DshRunner
from .engine import SupervisorEngine
from .events import EventLog, now_iso
from .git_probe import capture as capture_git
from .lock import LockHeldError, ParentLease, SupervisorLock
from .models import Counters, Limits, RuntimeState, SupervisorStatus
from .process_identity import identity_matches, is_proc_alive
from .prompts import build_prompt
from .storage import Layout, RuntimeStore, atomic_write_json, load_agent_state

DEFAULT_TOML = """\
# supervisor.toml — Autonomous Development Supervisor configuration (V1)
# See docs/supervisor-protocol.md for the full protocol.

version = 1

[dsh]
executable = "dsh"
profile = "headless"

[task]
file = ".supervisor/task.md"

[limits]
max_parent_activations = 20
max_crash_restarts = 5
max_clean_restarts = 10
max_timeouts = 3
max_ci_wakeups = 10

parent_timeout_seconds = 2700
terminate_grace_seconds = 10

max_active_wall_seconds = 14400

[restart]
backoff_seconds = [2, 5, 15, 30, 60]

[ci]
enabled = false
provider = "github"
poll_seconds = 30
discovery_grace_seconds = 180
max_wait_seconds = 7200

[human]
pause_active_wall_clock = true
"""


def _repo(arg: str) -> Path:
    return Path(arg).resolve()


# ---------------------------------------------------------------- commands


def _ensure_task_file(repo: Path, layout: Layout, config) -> None:
    """Ensure the task file exists (CLI --task > [task].file > default).
    Creates parent dirs; if file is missing, writes a placeholder that
    prompts the user to fill it. Never overwrites an existing file."""
    task_path = layout.task_file(config)
    if task_path.exists():
        return
    task_path.parent.mkdir(parents=True, exist_ok=True)
    task_path.write_text(
        "# Task\n\nDescribe the development task for the Parent Agent here.\n",
        encoding="utf-8",
    )


def cmd_init(args) -> int:
    repo = _repo(args.repo)
    layout = Layout(repo)
    layout.ensure_dirs()
    toml = repo / "supervisor.toml"
    created = []
    if not toml.exists():
        toml.write_text(DEFAULT_TOML, encoding="utf-8")
        created.append(str(toml))
    # Seed task file from CLI --task or config default; strictly runtime-owned
    cfg = None
    try:
        cfg = load_config(toml)
    except ConfigError:
        pass
    # CLI --task takes precedence and is persisted to supervisor.toml
    task_arg = getattr(args, "task", None)
    if task_arg:
        from .config import TaskConfig

        cfg = cfg or default_config()
        cfg.task = TaskConfig(file=task_arg)
        # Validate task path (repo-relative, no escape) using the same rule as Layout.task_file
        Layout(repo).task_file(cfg)
        # Persist the overridden task file into supervisor.toml so `run` without
        # --task picks up the same location ("init tells A, run must not go to B")
        try:
            existing_text = toml.read_text(encoding="utf-8")
        except OSError:
            existing_text = ""
        if "[task]" in existing_text:
            # Replace existing [task] file = "..." line
            import re

            new_text, n = re.subn(
                r'(?m)^\s*file\s*=.*$',
                f'file = "{task_arg}"',
                existing_text,
                count=1,
            )
            if n == 0:
                # [task] exists but no file line — insert under [task]
                new_text = existing_text.replace(
                    "[task]", f'[task]\nfile = "{task_arg}"', 1
                )
        else:
            # No [task] section yet — append
            sep = "" if existing_text.endswith("\n") or not existing_text else "\n"
            new_text = existing_text + f"{sep}\n[task]\nfile = \"{task_arg}\"\n"
        if new_text != existing_text:
            toml.write_text(new_text, encoding="utf-8")
    _ensure_task_file(repo, layout, cfg)
    print("supervisor: initialized workspace for %s" % repo)
    for p in (layout.supervisor_dir, layout.runs_dir, layout.inbox_dir):
        if p.exists():
            print("  created: %s" % p)
    if created:
        print("  created: %s" % created[0])
    task_path = layout.task_file(cfg)
    if task_path.exists():
        print("  task: %s" % task_path)
    return 0


def cmd_run(args) -> int:
    repo = _repo(args.repo)
    config = load_config(repo / "supervisor.toml")
    # CLI --task overrides config [task].file for this invocation (validated)
    if getattr(args, "task", None):
        from .config import TaskConfig

        config.task = TaskConfig(file=args.task)
        Layout(repo).task_file(config)  # validate repo-relative + no escape
        Layout(repo).task_file(config).parent.mkdir(parents=True, exist_ok=True)
    # Fail fast if task file is missing (do not silently invent a task)
    if not Layout(repo).task_file(config).exists():
        print(
            f"error: task file not found: {Layout(repo).task_file(config)} "
            "(run `supervisor init --task <file>` or create it)",
            file=sys.stderr,
        )
        return 1
    engine = SupervisorEngine(base_dir=repo, config=config)

    async def _amain() -> int:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, engine.request_stop)
        return await engine.run_forever()

    rc = asyncio.run(_amain())
    rt = RuntimeStore(Layout(repo)).load()
    if rt is not None:
        print(
            "supervisor: stopped status=%s stop_reason=%s exit_code=%d"
            % (
                rt.status.value,
                rt.stop_reason.value if rt.stop_reason else None,
                rc,
            )
        )
    return rc


def cmd_parent_once(args) -> int:
    repo = _repo(args.repo)
    config = load_config(repo / "supervisor.toml")
    if getattr(args, "task", None):
        from .config import TaskConfig

        config.task = TaskConfig(file=args.task)
        Layout(repo).task_file(config)  # validate
    layout = Layout(repo)
    # No active task source → never start a Parent (protocol §2 fail-closed)
    task_path = layout.task_file(config)
    if not task_path.exists():
        print(
            f"error: task file not found: {task_path} "
            "(run `supervisor init --task <file>` or create it)",
            file=sys.stderr,
        )
        return 1
    layout.ensure_dirs()
    lock = SupervisorLock(layout.lock_path)
    lock.acquire()
    lease = ParentLease(layout.parent_lock_path)
    if not lease.try_acquire():
        lock.release()
        raise LockHeldError(
            "Parent lease held by a live activation; refusing to spawn a second Parent"
        )
    try:
        rt = RuntimeStore(layout).load()
        activation_id = (rt.counters.parent_activations if rt and rt.counters else 0) + 1
        run_dir = layout.run_dir(activation_id)
        run_dir.mkdir(parents=True, exist_ok=True)

        task_file = task_path.relative_to(repo).as_posix()
        prompt = args.prompt or build_prompt("INITIAL_START", task_file=task_file)
        (run_dir / "prompt.txt").write_text(prompt, encoding="utf-8")

        git_before = capture_git(repo)
        runner = DshRunner(
            executable=config.dsh.executable,
            profile=config.dsh.profile,
            terminate_grace_seconds=config.limits.terminate_grace_seconds,
        )
        result = asyncio.run(
            runner.run(
                repo=repo,
                prompt=prompt,
                activation_id=activation_id,
                timeout_seconds=config.limits.parent_timeout_seconds,
                run_dir=run_dir,
                lease_fd=lease.fd,
            )
        )
        git_after = capture_git(repo)

        try:
            agent_after = load_agent_state(layout.agent_state_path)
        except Exception:
            agent_after = None

        data = {
            "activation_id": activation_id,
            "reason": "parent-once",
            "started_at": result.started_at,
            "ended_at": result.ended_at,
            "duration_seconds": result.duration_seconds,
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "pid": result.pid,
            "process_start_id": result.process_start_id,
            "stdout_path": result.stdout_path,
            "stderr_path": result.stderr_path,
            "git_before": git_before.to_dict(),
            "git_after": git_after.to_dict(),
            "agent_state_after": (
                {"status": agent_after.status.value, "checkpoint_seq": agent_after.checkpoint_seq}
                if agent_after
                else None
            ),
        }
        atomic_write_json(run_dir / "result.json", data)
        atomic_write_json(run_dir / "git-before.json", git_before.to_dict())
        atomic_write_json(run_dir / "git-after.json", git_after.to_dict())

        # 记录本次 activation 数，供后续 run 连续编号
        new_rt = rt or RuntimeState(
            schema_version=1,
            status=SupervisorStatus.STOPPED_OPERATOR,
            task_started_at=now_iso(),
            current_parent=None,
            counters=Counters(parent_activations=activation_id),
            limits=Limits(
                **{
                    "max_parent_activations": config.limits.max_parent_activations,
                    "max_crash_restarts": config.limits.max_crash_restarts,
                    "max_clean_restarts": config.limits.max_clean_restarts,
                    "max_timeouts": config.limits.max_timeouts,
                    "max_ci_wakeups": config.limits.max_ci_wakeups,
                    "max_active_wall_seconds": config.limits.max_active_wall_seconds,
                    "parent_timeout_seconds": config.limits.parent_timeout_seconds,
                    "terminate_grace_seconds": config.limits.terminate_grace_seconds,
                }
            ),
            last_agent_checkpoint_seq=agent_after.checkpoint_seq if agent_after else 0,
            supervisor_pid=None,
            supervisor_process_start_id=None,
            active_budget={"accrued_seconds": 0.0, "last_mark": None},
            stop_reason=None,
        )
        if rt is not None and rt.counters is not None:
            rt.counters.parent_activations = activation_id
            rt.last_agent_checkpoint_seq = (
                agent_after.checkpoint_seq if agent_after else rt.last_agent_checkpoint_seq
            )
            RuntimeStore(layout).save(rt)
        else:
            RuntimeStore(layout).save(new_rt)

        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0 if result.exit_code == 0 else 1
    finally:
        lease.release()
        lock.release()


def cmd_status(args) -> int:
    repo = _repo(args.repo)
    layout = Layout(repo)
    rt = RuntimeStore(layout).load()
    if rt is None:
        print("supervisor: not started (no .supervisor/runtime.json)")
        return 0
    print("supervisor status  : %s" % rt.status.value)
    print("task started at    : %s" % rt.task_started_at)
    print(
        "stop reason        : %s" % (rt.stop_reason.value if rt.stop_reason else None)
    )
    if rt.current_parent:
        print(
            "current parent     : activation=%s pid=%s reason=%s"
            % (
                rt.current_parent.activation_id,
                rt.current_parent.pid,
                rt.current_parent.reason,
            )
        )
    if rt.counters:
        c = rt.counters
        print(
            "counters           : activations=%s crash=%s clean=%s timeouts=%s"
            % (c.parent_activations, c.crash_restarts, c.clean_restarts, c.timeouts)
        )
    print("last agent ckpt seq: %d" % rt.last_agent_checkpoint_seq)
    agent = load_agent_state(layout.agent_state_path)
    if agent is not None:
        print(
            "agent state        : status=%s checkpoint_seq=%d updated_at=%s"
            % (agent.status.value, agent.checkpoint_seq, agent.updated_at)
        )
    else:
        print("agent state        : (none)")
    return 0


def cmd_events(args) -> int:
    repo = _repo(args.repo)
    log = EventLog(Layout(repo).events_path)
    rows = log.read_all()
    if args.event:
        rows = [r for r in rows if r.get("event") == args.event]
    if args.tail is not None:
        rows = rows[-args.tail:]
    for r in rows:
        print(json.dumps(r, ensure_ascii=False))
    return 0


def cmd_stop(args) -> int:
    repo = _repo(args.repo)
    rt = RuntimeStore(Layout(repo)).load()
    pid = rt.supervisor_pid if rt else None
    sid = rt.supervisor_process_start_id if rt else None
    if (
        not pid
        or not is_proc_alive(pid)
        or not sid
        or not identity_matches(pid, sid)
    ):
        print("supervisor: not running")
        return 1
    os.kill(pid, signal.SIGTERM)
    print("supervisor: SIGTERM sent to pid %d" % pid)
    return 0


def cmd_resume(args) -> int:
    repo = _repo(args.repo)
    if args.event not in ("HUMAN_APPROVED", "HUMAN_CHANGES_REQUESTED"):
        print("error: --event must be HUMAN_APPROVED or HUMAN_CHANGES_REQUESTED", file=sys.stderr)
        return 1
    layout = Layout(repo)
    layout.ensure_dirs()
    atomic_write_json(layout.resume_path, {"event": args.event, "ts": now_iso()})
    print("supervisor: resume marker written (%s)" % args.event)
    return 0


# ------------------------------------------------------------------ main


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="supervisor",
        description="Autonomous development supervisor for DSH Parent Agents.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create .supervisor/ layout and supervisor.toml")
    p.add_argument("repo", nargs="?", default=".")
    p.add_argument("--task", default=None, help="task file path (repo-relative, default .supervisor/task.md)")

    p = sub.add_parser("run", help="run the supervisor loop (foreground)")
    p.add_argument("repo", nargs="?", default=".")
    p.add_argument("--task", default=None, help="task file path override (repo-relative)")

    p = sub.add_parser("parent-once", help="run exactly one DSH parent activation")
    p.add_argument("repo", nargs="?", default=".")
    p.add_argument("--prompt", default=None, help="override the bootstrap prompt")
    p.add_argument("--task", default=None, help="task file path override")

    p = sub.add_parser("status", help="show runtime + agent state summary")
    p.add_argument("repo", nargs="?", default=".")

    p = sub.add_parser("events", help="show the append-only event log")
    p.add_argument("repo", nargs="?", default=".")
    p.add_argument("--tail", type=int, default=None)
    p.add_argument("--event", default=None)

    p = sub.add_parser("stop", help="SIGTERM the running supervisor")
    p.add_argument("repo", nargs="?", default=".")

    p = sub.add_parser("resume", help="resume a WAIT_HUMAN state")
    p.add_argument("repo", nargs="?", default=".")
    p.add_argument("--event", required=True)

    args = parser.parse_args(argv)

    handlers = {
        "init": cmd_init,
        "run": cmd_run,
        "parent-once": cmd_parent_once,
        "status": cmd_status,
        "events": cmd_events,
        "stop": cmd_stop,
        "resume": cmd_resume,
    }
    try:
        return handlers[args.command](args)
    except LockHeldError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    except ConfigError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    except NotImplementedError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1