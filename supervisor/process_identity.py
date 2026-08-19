"""Linux /proc 进程身份（M2 供 DshRunner，M5 供崩溃恢复）。

PID 可能被系统复用，不能只用 os.kill(pid, 0) 判断"还是不是那个进程"。
身份 = (pid, /proc/<pid>/stat 的 starttime, cmdline)。
"""

import os
from pathlib import Path

_PROC = Path("/proc")


def read_start_id(pid: int):
    """/proc/<pid>/stat 的第 22 字段 starttime（进程生命周期内唯一）。"""
    try:
        stat = (_PROC / str(pid) / "stat").read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        # comm 字段可能含空格/括号：从最后一个 ')' 之后取字段
        after = stat[stat.rindex(")") + 2:]
        tokens = after.split()
        # 字段 3=state 之后的索引 19 即第 22 字段 starttime
        return tokens[19]
    except (ValueError, IndexError):
        return None


def read_cmdline(pid: int):
    try:
        return (_PROC / str(pid) / "cmdline").read_bytes().decode(errors="replace")
    except OSError:
        return None


def is_proc_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # 存在但无权限信号：视为活着
        pass
    except OSError:
        return False
    # 僵尸进程（Z）虽然 kill(0) 成功，但已无实际执行体，视为死亡
    try:
        stat = (_PROC / str(pid) / "stat").read_text(encoding="utf-8")
        after = stat[stat.rindex(")") + 2 :]
        state = after.split()[0] if after.split() else ""
        if state == "Z":
            return False
    except OSError:
        return False
    return True


def identity_matches(pid: int, start_id) -> bool:
    """同一 pid 且 starttime 一致才认为还是同一个进程。"""
    if start_id is None:
        return False
    return read_start_id(pid) == str(start_id)


def looks_like_dsh_cmdline(cmdline) -> bool:
    """cmdline 是否像 DSH 进程（含 'dsh' 或 headless profile 标志）。"""
    if not cmdline:
        return False
    return "dsh" in cmdline or "headless" in cmdline


def is_dsh_process(pid: int) -> bool:
    """pid 活着、starttime 存在且 cmdline 像 DSH（恢复路径的完整判定）。"""
    if not is_proc_alive(pid):
        return False
    if read_start_id(pid) is None:
        return False
    cmdline = read_cmdline(pid)
    if not cmdline:
        return False
    # supervisor 允许的同族进程：DSH 本体、以及未 exec 的 launcher（exec 前后同 pid）
    if looks_like_dsh_cmdline(cmdline):
        return True
    return "launcher" in cmdline