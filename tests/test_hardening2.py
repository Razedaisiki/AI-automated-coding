"""M5 hardening 第二轮回归测试（评审 P0-1 / P0-2 / P1）。

覆盖点：
- P0-1 Parent lease：`.supervisor/parent.lock` flock 由 launcher→exec 后的 DSH 继承持有；
  重启的 Supervisor 拿不到锁（= 存在活着的旧 activation）时**绝不 spawn 第二个 Parent**；
  锁释放（= 旧进程死）后才允许重 spawn。
- P0-2 整组终止：SIGTERM→grace→SIGKILL 后必须确认**整个 PGID** 消失
  （含忽略 SIGTERM 的子进程），而不是只看 leader PID。
- P1 收养 timeout：必须走 RECOVER_AFTER_PARENT_TIMEOUT + 退避（不是默认 CONTINUE），
  且不被当作终态 StopReason.MAX_TIMEOUTS。
- P1 orphan 自退且退出状态未知：保守按 RECOVER_AFTER_PARENT_CRASH 恢复（不是 CONTINUE）。
- P1/P2 STOPPING：operator-stop 收尾期间落盘 STOPPING；在终止宽限期内崩溃后重启，
  新 Supervisor 完成收尾（绝不 spawn）。
"""

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from supervisor.config import default_config
from supervisor.engine import SupervisorEngine
from supervisor.events import EventLog
from supervisor.lock import ParentLease
from supervisor.models import StopReason, SupervisorStatus
from supervisor.process_identity import process_group_alive, read_start_id
from supervisor.storage import Layout, RuntimeStore, atomic_write_json

from conftest import FAKE_DSH, FakeParentRunner, StepScript, event_names, run_engine, wait_until

from test_hardening import _pgid_alive, _runtime_dict, _spawn_orphan, _write_agent_state, _write_runtime


def _iso_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _counter_dict(activations=4):
    return {
        "parent_activations": activations,
        "crash_restarts": 0,
        "clean_restarts": 0,
        "timeouts": 0,
        "ci_wakeups": 0,
    }


def _proc_state(pid):
    """/proc/<pid>/stat 的 state 字段；进程不存在 → None。"""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        return stat[stat.rindex(")") + 2 :].split()[0]
    except (OSError, IndexError, ValueError):
        return None


def _same_group_child(pgid):
    """找出与 pgid 同组、非 leader、非僵尸的成员 pid（模拟"忽略 SIGTERM 的子进程"）。"""
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) == pgid:
            continue
        try:
            stat = Path(f"/proc/{entry}/stat").read_text(encoding="utf-8", errors="replace")
            fields = stat[stat.rindex(")") + 2 :].split()
            if len(fields) > 2 and fields[2] == str(pgid) and fields[0] != "Z":
                return int(entry)
        except (OSError, ValueError, IndexError):
            continue
    return None


class TestParentLease:
    def _hold_lease(self, repo):
        """起一个持有 parent.lock flock 的进程（模拟还活着的旧 launcher/DSH）。"""
        code = (
            "import fcntl,os,sys,time\n"
            "fd=os.open(sys.argv[1], os.O_CREAT|os.O_RDWR, 0o644)\n"
            "fcntl.flock(fd, fcntl.LOCK_EX)\n"
            "print('held', flush=True)\n"
            "while True: time.sleep(60)\n"
        )
        return subprocess.Popen(
            [sys.executable, "-c", code, str(repo / ".supervisor" / "parent.lock")],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

    def test_lease_held_prevents_respawn_then_releases(self, tmp_repo):
        """P0-1 崩溃窗口：runtime 只有 STARTING_PARENT+token、无 process.json，
        但 lease 被活着的旧进程占用 → 重启的 Supervisor 绝不能 spawn；
        只有当 lease 释放后才允许重 spawn。"""
        token = "tok-lease"
        holder = self._hold_lease(tmp_repo)
        try:
            holder.stdout.readline()  # 等它真正持有锁
            _write_runtime(
                tmp_repo,
                _runtime_dict(
                    tmp_repo,
                    status="STARTING_PARENT",
                    current_parent={
                        "activation_id": 3,
                        "activation_token": token,
                        "pid": None,
                        "process_start_id": None,
                        "started_at": _iso_now(),
                        "reason": "INITIAL_START",
                    },
                    counters=_counter_dict(activations=2),
                ),
            )
            _write_agent_state(tmp_repo, seq=1)
            cfg = default_config()
            cfg.limits.terminate_grace_seconds = 0.5  # 缩短宽限窗口
            cfg.restart.backoff_seconds = [0.01]
            runner = FakeParentRunner([StepScript.completed()], Layout(tmp_repo))
            engine = SupervisorEngine(base_dir=tmp_repo, config=cfg, runner=runner)

            async def control(eng):
                # 等待引擎走过宽限并发现 lease 被占用
                await wait_until(lambda: "PARENT_LEASE_HELD" in event_names(eng))
                await asyncio.sleep(0.3)
                # lease 被占用期间：绝不 spawn 第二个 Parent
                assert not EventLog(eng.layout.events_path).events_named("PARENT_STARTED")
                # 旧进程死掉（lease 释放）→ 引擎此时才允许安全重 spawn
                holder.kill()
                holder.wait()
                await wait_until(
                    lambda: bool(EventLog(eng.layout.events_path).events_named("PARENT_STARTED"))
                )

            rc = run_engine(engine, control)
            assert rc == 0
            assert runner.calls == [3]  # 激活号不重复，未产生新激活
            names = event_names(engine)
            assert "PARENT_LEASE_HELD" in names
            assert "PARENT_SPAWN_UNCONFIRMED" in names
        finally:
            if holder.poll() is None:
                holder.kill()
                holder.wait()

    def test_release_is_fd_handoff_not_unlock(self, tmp_repo, monkeypatch):
        """P0-R4：Supervisor 结束激活时只 close 自己的 FD 副本（handoff），
        **绝不 LOCK_UN** —— 子进程/DSH 仍持有锁；子进程死后锁自动释放。"""
        from supervisor.dsh_runner import DshRunner

        monkeypatch.setenv("FAKE_DSH_MODE", "hang")
        lease_path = tmp_repo / ".supervisor" / "parent.lock"
        lease = ParentLease(lease_path)
        lease.acquire()
        child = None

        async def go():
            nonlocal child
            runner = DshRunner(
                executable=str(FAKE_DSH), profile="headless", terminate_grace_seconds=1
            )
            task = asyncio.ensure_future(
                runner.run(
                    repo=tmp_repo,
                    prompt="handoff test",
                    activation_id=1,
                    timeout_seconds=30,
                    run_dir=Layout(tmp_repo).run_dir(1),
                    activation_token="tok-handoff",
                    lease_fd=lease.fd,
                )
            )
            await asyncio.sleep(1.5)
            child = runner.last_pid
            # 模拟激活结束：Supervisor 只 close 自己的 FD 副本（不做 LOCK_UN）
            lease.release()
            # 子进程仍活着 → 租约必须仍被占用（否则破坏"旧 activation 活着绝不 spawn"）
            assert not ParentLease(lease_path).try_acquire(), "handoff wrongly released child lease"
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        asyncio.run(go())
        # 子进程已死 → 不再需要任何 release，租约自动释放
        self._wait_lease_free(lease_path)
        fresh = ParentLease(lease_path)
        assert fresh.try_acquire() is True
        fresh.release()
        # 清理任何残留
        if child:
            try:
                os.killpg(child, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    @staticmethod
    def _wait_lease_free(lease_path, timeout=5.0):
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            fresh = ParentLease(lease_path)
            if fresh.try_acquire():
                fresh.release()
                return
            time.sleep(0.05)

    def test_lease_fd_inherited_by_spawned_child_and_auto_released(self, tmp_repo):
        """P0-1 机制：Supervisor 获得 lease 后把已锁 FD 传给 launcher→exec 后的 DSH；
        子进程存活期间 lease 必须被占用；子进程死亡后 lease 自动释放。"""
        from supervisor.dsh_runner import DshRunner

        lease_path = tmp_repo / ".supervisor" / "parent.lock"
        lease = ParentLease(lease_path)
        lease.acquire()
        env = dict(os.environ, FAKE_DSH_MODE="hang")
        proceed = {}

        async def go():
            runner = DshRunner(
                executable=str(FAKE_DSH), profile="headless", terminate_grace_seconds=1
            )
            task = asyncio.ensure_future(
                runner.run(
                    repo=tmp_repo,
                    prompt="lease test",
                    activation_id=1,
                    timeout_seconds=30,
                    run_dir=Layout(tmp_repo).run_dir(1),
                    activation_token="tok-lease-fd",
                    lease_fd=lease.fd,
                )
            )
            await asyncio.sleep(1.5)
            # launcher 已把 fd 写进 process.json（审计）
            record = json.loads(
                (Layout(tmp_repo).run_dir(1) / "process.json").read_text(encoding="utf-8")
            )
            assert record.get("activation_token") == "tok-lease-fd"
            assert record.get("parent_lock_fd") is not None
            # 子进程（fake_dsh hang）活着 → lease 必须被占用（有进程继承持有）
            assert not ParentLease(lease_path).try_acquire(), "lease not held by child"
            proceed["child_held"] = True
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        asyncio.run(go())
        assert proceed.get("child_held") is True
        # 子进程已死 → 现在我们自己的 FD 是唯一持有者，释放后 lease 空闲
        lease.release()
        fresh = ParentLease(lease_path)
        assert fresh.try_acquire() is True
        fresh.release()


class TestWholeGroupTermination:
    def test_boot_cleans_stale_group_when_leader_identity_verifiable(self, tmp_repo):
        """P0-2：记录的 leader 已死（僵尸，starttime 可验证）但组内子进程还活着
        → 启动时清理残留组（STALE_GROUP_CLEANUP），绝不让旧子进程留在仓库里跑。"""
        env = dict(os.environ, FAKE_DSH_MODE="hang_with_ignoring_child")
        parent = subprocess.Popen(
            [sys.executable, str(FAKE_DSH), "orphan-task"],
            cwd=str(tmp_repo),
            env=env,
            start_new_session=True,
        )
        try:
            sid = None
            for _ in range(50):
                sid = read_start_id(parent.pid)
                if sid is not None:
                    try:
                        if Path(f"/proc/{parent.pid}/cmdline").read_bytes():
                            break
                    except OSError:
                        pass
                time.sleep(0.05)
            child_pid = None
            for _ in range(50):
                child_pid = _same_group_child(parent.pid)
                if child_pid is not None:
                    break
                time.sleep(0.05)
            assert child_pid is not None, "no in-group child spawned"
            # 只杀 leader（不杀组）：leader 成僵尸，子进程继续运行
            os.kill(parent.pid, signal.SIGKILL)
            for _ in range(50):
                if _proc_state(parent.pid) == "Z":
                    break
                time.sleep(0.05)
            assert _proc_state(parent.pid) == "Z", "leader did not become zombie"
            assert process_group_alive(parent.pid), "stale child should still be in group"
            _write_runtime(
                tmp_repo,
                _runtime_dict(
                    tmp_repo,
                    current_parent={
                        "activation_id": 5,
                        "pid": parent.pid,
                        "process_start_id": sid,
                        "started_at": _iso_now(),
                        "reason": "CONTINUE",
                    },
                    counters=_counter_dict(activations=4),
                ),
            )
            _write_agent_state(tmp_repo, seq=5)
            cfg = default_config()
            cfg.limits.terminate_grace_seconds = 1
            cfg.restart.backoff_seconds = [0.01]
            runner = FakeParentRunner([StepScript.completed()], Layout(tmp_repo))
            engine = SupervisorEngine(base_dir=tmp_repo, config=cfg, runner=runner)

            rc = run_engine(engine)
            assert rc == 0
            killed = [
                e
                for e in EventLog(engine.layout.events_path).read_all()
                if e["event"] == "PARENT_KILLED"
            ]
            assert any(e.get("reason") == "STALE_GROUP_CLEANUP" for e in killed)
            try:
                parent.wait(timeout=5)  # reap 僵尸
            except subprocess.TimeoutExpired:
                pass
            assert not process_group_alive(parent.pid)
            st = _proc_state(child_pid)
            assert st is None or st == "Z", f"stale child still running (state={st})"
        finally:
            try:
                os.killpg(parent.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                parent.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass

    def test_operator_stop_kills_ignoring_grandchild(self, tmp_repo):
        """P0-2：收养孤儿时 operator stop → SIGTERM 后 leader 死了但忽略 SIGTERM 的
        子进程还活着 → 必须 SIGKILL 整组并确认整个 PGID 消失。"""
        env = dict(os.environ, FAKE_DSH_MODE="hang_with_ignoring_child")
        parent = subprocess.Popen(
            [sys.executable, str(FAKE_DSH), "orphan-task"],
            cwd=str(tmp_repo),
            env=env,
            start_new_session=True,
        )
        try:
            # 等 /proc 就绪
            sid = None
            for _ in range(50):
                sid = read_start_id(parent.pid)
                if sid is not None:
                    try:
                        if Path(f"/proc/{parent.pid}/cmdline").read_bytes():
                            break
                    except OSError:
                        pass
                time.sleep(0.05)
            child_pid = None
            for _ in range(50):  # 等 leader 把子进程 spawn 出来
                child_pid = _same_group_child(parent.pid)
                if child_pid is not None:
                    break
                time.sleep(0.05)
            assert child_pid is not None, "fixture did not spawn an in-group child"
            assert child_pid != parent.pid
            _write_runtime(
                tmp_repo,
                _runtime_dict(
                    tmp_repo,
                    current_parent={
                        "activation_id": 5,
                        "pid": parent.pid,
                        "process_start_id": sid,
                        "started_at": _iso_now(),
                        "reason": "CONTINUE",
                    },
                    counters=_counter_dict(activations=4),
                ),
            )
            _write_agent_state(tmp_repo, seq=5)
            cfg = default_config()
            cfg.limits.terminate_grace_seconds = 1
            runner = FakeParentRunner([], Layout(tmp_repo))
            engine = SupervisorEngine(base_dir=tmp_repo, config=cfg, runner=runner)

            async def control(eng):
                await wait_until(lambda: "ORPHAN_ADOPTED" in event_names(eng))
                await asyncio.sleep(0.2)
                eng.request_stop()

            rc = run_engine(engine, control)
            assert rc == 0
            rt = RuntimeStore(Layout(tmp_repo)).load()
            assert rt.status == SupervisorStatus.STOPPED_OPERATOR
            # 整个进程组必须消失（含忽略 SIGTERM 的子进程）
            try:
                parent.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            assert not process_group_alive(parent.pid), "process group still alive"
            # 子进程不允许继续存活（旧实现只查 leader，会漏掉它）
            st = _proc_state(child_pid)
            assert st is None or st == "Z", f"ignoring child still running (state={st})"
        finally:
            try:
                os.killpg(parent.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                parent.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass


class TestAdoptionTimeoutRecovery:
    def test_adoption_timeout_next_prompt_is_timeout_recovery(self, tmp_repo):
        """P1：收养期 Parent timeout → 杀进程组、计数、然后下一轮必须走
        RECOVER_AFTER_PARENT_TIMEOUT + 退避，而不是默认 CONTINUE；
        也不得把本次 per-parent kill 当成终态 StopReason.MAX_TIMEOUTS。"""
        orphan, start_id = _spawn_orphan(tmp_repo)
        _write_runtime(
            tmp_repo,
            _runtime_dict(
                tmp_repo,
                current_parent={
                    "activation_id": 5,
                    "pid": orphan.pid,
                    "process_start_id": start_id,
                    "started_at": _iso_now(),
                    "reason": "CONTINUE",
                },
                counters=_counter_dict(activations=4),
            ),
        )
        _write_agent_state(tmp_repo, seq=5)
        cfg = default_config()
        cfg.limits.parent_timeout_seconds = 1
        cfg.limits.terminate_grace_seconds = 1
        cfg.limits.max_timeouts = 5  # 不触发整体限额
        cfg.restart.backoff_seconds = [0.01]
        runner = FakeParentRunner([StepScript.completed()], Layout(tmp_repo))
        engine = SupervisorEngine(base_dir=tmp_repo, config=cfg, runner=runner)

        rc = run_engine(engine)
        assert rc == 0
        rt = RuntimeStore(Layout(tmp_repo)).load()
        assert rt.status == SupervisorStatus.STOPPED_SUCCESS
        assert rt.stop_reason == StopReason.TASK_COMPLETED
        assert rt.counters.timeouts >= 1
        names = event_names(engine)
        assert "PARENT_TIMEOUT" in names
        assert "PARENT_KILLED" in names
        assert "RESTART_BACKOFF" in names  # 退避发生
        # 本轮 per-parent kill 不属于终态 stop reason——下一轮正常执行
        assert rt.stop_reason != StopReason.MAX_TIMEOUTS
        # 下一轮 prompt 必须是 timeout 恢复语义
        p6 = (tmp_repo / ".supervisor" / "runs" / "activation-000006" / "prompt.txt").read_text(
            encoding="utf-8"
        )
        assert "RECOVER_AFTER_PARENT_TIMEOUT" in p6
        try:
            orphan.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
        assert not _pgid_alive(orphan.pid)


class TestOrphanSelfExitRecovery:
    def test_orphan_self_exit_uses_crash_recovery_prompt(self, tmp_repo):
        """P1：orphan 自己消失且退出状态未知（带 RUNNING 状态）
        → 保守按 RECOVER_AFTER_PARENT_CRASH 恢复，而不是默认 CONTINUE。"""
        orphan, start_id = _spawn_orphan(tmp_repo)
        _write_runtime(
            tmp_repo,
            _runtime_dict(
                tmp_repo,
                current_parent={
                    "activation_id": 5,
                    "pid": orphan.pid,
                    "process_start_id": start_id,
                    "started_at": _iso_now(),
                    "reason": "CONTINUE",
                },
                counters=_counter_dict(activations=4),
            ),
        )
        _write_agent_state(tmp_repo, seq=5)
        cfg = default_config()
        cfg.restart.backoff_seconds = [0.01]
        runner = FakeParentRunner([StepScript.completed()], Layout(tmp_repo))
        engine = SupervisorEngine(base_dir=tmp_repo, config=cfg, runner=runner)

        async def control(eng):
            await wait_until(lambda: "ORPHAN_ADOPTED" in event_names(eng))
            await asyncio.sleep(0.3)
            # 模拟 orphan 自己退出（不可见退出码）
            os.killpg(orphan.pid, signal.SIGKILL)
            orphan.wait()
            await wait_until(
                lambda: bool(EventLog(eng.layout.events_path).events_named("PARENT_STARTED"))
            )

        rc = run_engine(engine, control)
        assert rc == 0
        names = event_names(engine)
        assert "ORPHAN_EXITED" in names
        p6 = (tmp_repo / ".supervisor" / "runs" / "activation-000006" / "prompt.txt").read_text(
            encoding="utf-8"
        )
        assert "RECOVER_AFTER_PARENT_CRASH" in p6  # 保守恢复，不是 CONTINUE


class TestStoppingStatus:
    def test_stopping_persisted_during_operator_stop(self, tmp_repo):
        """P2：operator-stop 收尾期间 runtime 必须落盘 STOPPING（终止宽限期窗口）。"""
        orphan, start_id = _spawn_orphan(tmp_repo)
        _write_runtime(
            tmp_repo,
            _runtime_dict(
                tmp_repo,
                current_parent={
                    "activation_id": 5,
                    "pid": orphan.pid,
                    "process_start_id": start_id,
                    "started_at": _iso_now(),
                    "reason": "CONTINUE",
                },
                counters=_counter_dict(activations=4),
            ),
        )
        _write_agent_state(tmp_repo, seq=5)
        cfg = default_config()
        cfg.limits.terminate_grace_seconds = 1
        # 让 orphan 忽略 SIGTERM → 终止宽限期真正持续 ≥1s，STOPPING 窗口可观测
        os.killpg(orphan.pid, signal.SIGSTOP)  # 冻结它，确保无法配合退出
        try:
            runner = FakeParentRunner([], Layout(tmp_repo))
            engine = SupervisorEngine(base_dir=tmp_repo, config=cfg, runner=runner)

            async def control(eng):
                await wait_until(lambda: "ORPHAN_ADOPTED" in event_names(eng))
                await asyncio.sleep(0.1)
                eng.request_stop()
                # 终止宽限期内 STOPPING 必须落盘
                await wait_until(
                    lambda: RuntimeStore(Layout(eng.base)).load().status
                    == SupervisorStatus.STOPPING
                )

            rc = run_engine(engine, control)
            assert rc == 0
            rt = RuntimeStore(Layout(tmp_repo)).load()
            assert rt.status == SupervisorStatus.STOPPED_OPERATOR
            assert rt.stop_reason == StopReason.OPERATOR_STOP
            try:
                os.killpg(orphan.pid, signal.SIGCONT)
            except ProcessLookupError:
                pass
            try:
                orphan.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            assert not _pgid_alive(orphan.pid)
        finally:
            try:
                os.killpg(orphan.pid, signal.SIGCONT)
            except ProcessLookupError:
                pass

    def test_stopping_crash_with_starting_parent_recovers_via_record(self, tmp_repo):
        """P2 边界：STOPPING 崩溃时 Parent 还没 on_start（pid 未知）→ 重启后通过
        process.json（token 匹配）找回真身并杀组，绝不 spawn。"""
        orphan, start_id = _spawn_orphan(tmp_repo)
        token = "tok-recover-stop"
        run_dir = Layout(tmp_repo).run_dir(3)
        run_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            run_dir / "process.json",
            {
                "pid": orphan.pid,
                "process_start_id": start_id,
                "activation_id": 3,
                "activation_token": token,
                "written_at": _iso_now(),
            },
        )
        _write_runtime(
            tmp_repo,
            _runtime_dict(
                tmp_repo,
                status="STOPPING",
                current_parent={
                    "activation_id": 3,
                    "activation_token": token,
                    "pid": None,
                    "process_start_id": None,
                    "started_at": _iso_now(),
                    "reason": "CONTINUE",
                },
                counters=_counter_dict(activations=2),
            ),
        )
        _write_agent_state(tmp_repo, seq=3)
        cfg = default_config()
        cfg.limits.terminate_grace_seconds = 1
        runner = FakeParentRunner([StepScript.completed()], Layout(tmp_repo))
        engine = SupervisorEngine(base_dir=tmp_repo, config=cfg, runner=runner)
        rc = run_engine(engine)
        assert rc == 0
        assert runner.calls == [], f"supervisor spawned after interrupted stop: {runner.calls}"
        rt = RuntimeStore(Layout(tmp_repo)).load()
        assert rt.status == SupervisorStatus.STOPPED_OPERATOR
        assert rt.stop_reason == StopReason.OPERATOR_STOP
        try:
            orphan.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        assert not process_group_alive(orphan.pid), "orphan survived record-based stop recovery"

    def test_stopping_crash_recovery_completes_stop_without_spawn(self, tmp_repo):
        """P2：runtime 处于 STOPPING + 存活 Parent（收尾途中崩溃）→ 重启的
        Supervisor 完成收尾（杀组 → STOPPED_OPERATOR），**绝不 spawn**。"""
        orphan, start_id = _spawn_orphan(tmp_repo)
        _write_runtime(
            tmp_repo,
            _runtime_dict(
                tmp_repo,
                status="STOPPING",
                current_parent={
                    "activation_id": 5,
                    "pid": orphan.pid,
                    "process_start_id": start_id,
                    "started_at": _iso_now(),
                    "reason": "CONTINUE",
                },
                counters=_counter_dict(activations=4),
            ),
        )
        _write_agent_state(tmp_repo, seq=5)
        cfg = default_config()
        cfg.limits.terminate_grace_seconds = 1
        cfg.limits.parent_timeout_seconds = 1  # 若没走"完成收尾"路径会很快触发 timeout 并重启
        runner = FakeParentRunner([StepScript.completed()], Layout(tmp_repo))
        engine = SupervisorEngine(base_dir=tmp_repo, config=cfg, runner=runner)

        rc = run_engine(engine)
        assert rc == 0
        assert runner.calls == [], f"supervisor spawned after interrupted stop: {runner.calls}"
        rt = RuntimeStore(Layout(tmp_repo)).load()
        assert rt.status == SupervisorStatus.STOPPED_OPERATOR
        assert rt.stop_reason == StopReason.OPERATOR_STOP
        try:
            orphan.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        assert not process_group_alive(orphan.pid), "orphan survived interrupted-stop recovery"