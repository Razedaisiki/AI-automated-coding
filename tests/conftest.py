"""共享测试夹具：fake runner、repo 构造、引擎驱动助手。"""

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from supervisor.config import Config, default_config
from supervisor.events import EventLog
from supervisor.models import AgentState, AgentStatus, ParentResult
from supervisor.storage import Layout

# 把项目根加入导入路径（pytest 的 pythonpath 配置已处理）
PROJECT_DIR = Path(__file__).resolve().parent.parent
FAKE_DSH = PROJECT_DIR / "tests" / "fixtures" / "fake_dsh.py"

TEMPLATE_TOML = (PROJECT_DIR / "supervisor.toml").read_text(encoding="utf-8")


def toml_from_config(cfg: Config) -> str:
    """把 Config 对象序列化成 supervisor.toml 文本（测试用）。"""
    lines = [f"version = {cfg.version}", "", "[dsh]"]
    lines.append(f'executable = "{cfg.dsh.executable}"')
    lines.append(f'profile = "{cfg.dsh.profile}"')
    lines.append("")
    lines.append("[limits]")
    for key, value in cfg.limits.__dict__.items():
        lines.append(f"{key} = {value}")
    lines.append("")
    lines.append("[restart]")
    lines.append(f"backoff_seconds = {json.dumps(cfg.restart.backoff_seconds)}")
    lines.append("")
    lines.append("[ci]")
    lines.append(f"enabled = {'true' if cfg.ci.enabled else 'false'}")
    lines.append(f'provider = "{cfg.ci.provider}"')
    lines.append(f"poll_seconds = {cfg.ci.poll_seconds}")
    lines.append(f"discovery_grace_seconds = {cfg.ci.discovery_grace_seconds}")
    lines.append(f"max_wait_seconds = {cfg.ci.max_wait_seconds}")
    lines.append("")
    lines.append("[human]")
    lines.append(f"pause_active_wall_clock = {'true' if cfg.human.pause_active_wall_clock else 'false'}")
    return "\n".join(lines) + "\n"


def write_repo_toml(repo: Path, cfg: Config, fake_dsh: bool = False) -> Path:
    if fake_dsh:
        cfg.dsh.executable = str(FAKE_DSH)
    path = repo / "supervisor.toml"
    path.write_text(toml_from_config(cfg), encoding="utf-8")
    return path


@pytest.fixture
def tmp_repo(tmp_path):
    """带 .supervisor/ 的空仓库目录。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".supervisor").mkdir(parents=True)
    return repo


# ----------------------------------------------------------- fake runner


class Step:
    """Fake 一次 Parent activation 的行为。"""

    def __init__(
        self,
        status=None,          # AgentStatus 或字符串；None 表示不写状态
        exit_code=0,
        seq=None,             # 显式 checkpoint_seq；None 自动 +1
        stale=False,          # 用上一个 seq（模拟无新 checkpoint）
        delete_state=False,   # 模拟崩溃后状态缺失
        timed_out=False,
        hang=False,           # 模拟永久运行（取消时中断）
        delay=0.0,
    ):
        self.status = status
        self.exit_code = exit_code
        self.seq = seq
        self.stale = stale
        self.delete_state = delete_state
        self.timed_out = timed_out
        self.hang = hang
        self.delay = delay

    def __repr__(self):
        return f"Step(status={self.status}, exit={self.exit_code}, timed_out={self.timed_out})"


class FakeParentRunner:
    """实现 ParentRunner 协议；脚本走完复用最后一步。"""

    def __init__(self, steps, layout: Layout):
        self.steps = list(steps)
        self.layout = layout
        self.last = None
        self.calls = []        # activation ids
        self.prompts = []      # 每个 activation 收到的 prompt
        self._seq = 0

    def _next(self):
        if self.steps:
            self.last = self.steps.pop(0)
        return self.last

    def _write_state(self, step: Step):
        if step.stale:
            seq = max(self._seq, 1)  # 不增加
        elif step.seq is not None:
            seq = step.seq
            self._seq = seq
        else:
            self._seq += 1
            seq = self._seq
        state = AgentState(
            schema_version=1,
            status=AgentStatus(step.status),
            checkpoint_seq=seq,
            updated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        self.layout.agent_state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.layout.agent_state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state.to_dict()), encoding="utf-8")
        tmp.replace(self.layout.agent_state_path)

    async def run(self, *, repo, prompt, activation_id, timeout_seconds, run_dir, on_start=None):
        step = self._next()
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
        (run_dir / "stdout.log").write_text("fake stdout\n", encoding="utf-8")
        (run_dir / "stderr.log").write_text("", encoding="utf-8")
        self.calls.append(activation_id)
        self.prompts.append(prompt)
        if on_start is not None:
            on_start(999999, "fake-start")

        if step.delay:
            await asyncio.sleep(step.delay)
        if step.delete_state:
            if self.layout.agent_state_path.exists():
                self.layout.agent_state_path.unlink()
        elif step.status is not None:
            self._write_state(step)
        if step.hang:
            while True:
                await asyncio.sleep(60)

        started = "2026-08-18T06:00:00Z"
        ended = "2026-08-18T06:03:00Z"
        if step.timed_out:
            exit_code = -9
        elif step.exit_code is not None:
            exit_code = step.exit_code
        else:
            exit_code = 0
        return ParentResult(
            activation_id=activation_id,
            exit_code=exit_code,
            timed_out=step.timed_out,
            started_at=started,
            ended_at=ended,
            duration_seconds=step.delay,
            stdout_path=str(run_dir / "stdout.log"),
            stderr_path=str(run_dir / "stderr.log"),
            run_dir=str(run_dir),
            pid=999999,
            process_start_id="fake-start",
        )


class StepScript:
    """方便构建多步脚本的辅助类。"""

    @staticmethod
    def completed(exit_code=0):
        return Step(status="COMPLETED", exit_code=exit_code)

    @staticmethod
    def blocked(exit_code=0):
        return Step(status="BLOCKED", exit_code=exit_code)

    @staticmethod
    def running(exit_code=0, stale=False, delay=0.0, timed_out=False, delete_state=False):
        return Step(
            status="RUNNING",
            exit_code=exit_code,
            stale=stale,
            delay=delay,
            timed_out=timed_out,
            delete_state=delete_state,
        )

    @staticmethod
    def wait_ci(exit_code=0):
        return Step(status="WAIT_CI", exit_code=exit_code)

    @staticmethod
    def wait_human(exit_code=0):
        return Step(status="WAIT_HUMAN", exit_code=exit_code)


# ------------------------------------------------------------ 驱动助手


def run_engine(engine, control=None, timeout=15.0):
    """把 engine 与测试控制协程跑进同一事件循环。

    - 有 control：先跑 control（断言/等待），结束后等待 engine 收尾。
    - 无 control：直接等 engine 自行结束；若超时则请求停止并报错。
    """

    async def _main():
        eng_task = asyncio.ensure_future(engine.run_forever())
        ctl_exc = None
        try:
            if control is not None:
                await asyncio.wait_for(control(engine), timeout=timeout)
        except BaseException as exc:
            ctl_exc = exc
        if eng_task.done() and eng_task.exception() is not None:
            raise eng_task.exception()  # 引擎异常优先暴露
        if ctl_exc is not None:
            raise ctl_exc
        try:
            return await asyncio.wait_for(eng_task, timeout=timeout + 5)
        except asyncio.TimeoutError:
            engine.request_stop()
            if eng_task.done() and eng_task.exception() is not None:
                raise eng_task.exception()
            raise

    return asyncio.run(_main())


async def wait_until(predicate, timeout=5.0, step=0.02, what="condition"):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if predicate():
            return
        await asyncio.sleep(step)
    raise TimeoutError(f"timed out waiting for {what}")


def events_of(engine) -> list:
    return EventLog(engine.layout.events_path).read_all()


def event_names(engine) -> list:
    return [e["event"] for e in events_of(engine)]