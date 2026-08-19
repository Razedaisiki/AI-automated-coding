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


def process_group_alive(pgid: int) -> bool:
    """PGID 是否还有**非僵尸**成员（P0-2 整组终止的判定依据）。

    `os.killpg(pgid, 0)` 只证明"组里至少还有一个进程"——僵尸也算，直到被
    reap。而僵尸不持有 FD、不能执行任何代码，对"进程组是否还在干活"无意义。
    因此：先看 killpg 是否成功；若组里只剩僵尸，扫描 /proc 后返回 False。
    """
    pgid = int(pgid)
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    # 组还存在：扫描 /proc，找同组且非僵尸（state != 'Z'）的成员
    try:
        entries = os.listdir("/proc")
    except OSError:
        return True  # 无法读 /proc → 保守视为存活
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            stat = (_PROC / entry / "stat").read_text(encoding="utf-8", errors="replace")
            after = stat[stat.rindex(")") + 2 :]
            fields = after.split()
            # fields[0]=state, fields[1]=ppid, fields[2]=pgrp
            if len(fields) > 2 and fields[2] == str(pgid) and fields[0] != "Z":
                return True
        except (OSError, ValueError, IndexError):
            continue
    return False