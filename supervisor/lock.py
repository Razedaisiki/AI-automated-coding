"""仓库级独占锁（M1，Linux V1 用 fcntl.flock）。

防止两个 Supervisor 同时操作同一仓库：
terminal 2 里第二个 `supervisor run` 会直接退出：
    Supervisor already running for this repository.
"""

import fcntl
import os
from pathlib import Path


class LockHeldError(Exception):
    """Another Supervisor already holds the exclusive repository lock."""

    MESSAGE = "Supervisor already running for this repository."


class SupervisorLock:
    def __init__(self, path):
        self.path = Path(path)
        self._fd = None

    def acquire(self) -> "SupervisorLock":
        if self._fd is not None:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise LockHeldError(LockHeldError.MESSAGE)
        self._fd = fd
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        return self

    def release(self) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def __enter__(self) -> "SupervisorLock":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()


class ParentLease:
    """Parent 唯一性租约（M5 hardening P0-1）：flock 独占 `.supervisor/parent.lock`。

    与 `.supervisor/lock`（Supervisor 独占锁）不同，`parent.lock` 保证的是
    **Parent 进程的唯一性**：

    - Supervisor 在每次 spawn 前获取租约；获取失败 = 存在活着的旧 activation
      （其 launcher / exec 后的 DSH 继承持有已锁 FD），**绝不 spawn 第二个 Parent**。
    - 获取后把已锁 FD 通过 `pass_fds` + 环境变量 `SUPERVISOR_PARENT_LOCK_FD`
      传给 launcher → `os.execvp` 后继续由 DSH 持有（exec 不清除继承 FD）。
      因此锁的生命周期与整个 DSH 进程绑定，Supervisor 被杀也不影响；
      DSH 死亡时内核关闭其 FD，flock 自动释放。
    - 作用分工：`process.json` 负责**身份发现**（pid/starttime/token 在哪），
      `parent.lock` 负责**唯一性保证**（旧 activation 是否还活着）。

    锁与 FD 语义：`flock` 锁绑定在 open-file-description 上；子进程继承的 FD
    指向同一 OFD，父进程即使关闭自己的 FD，锁仍由子进程持有。
    """

    def __init__(self, path):
        self.path = Path(path)
        self._fd = None

    def try_acquire(self) -> bool:
        """尝试获取租约（不阻塞）。成功返回 True 且本对象持有；失败返回 False。"""
        if self._fd is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        return True

    def acquire(self) -> "ParentLease":
        """获取租约；被他人持有时抛 LockHeldError（调用方不得 spawn）。"""
        if not self.try_acquire():
            raise LockHeldError(
                "Parent lease held by a live activation; refusing to spawn a second Parent"
            )
        return self

    def release(self) -> None:
        """释放本对象持有的 FD 副本——**只 close，绝不 `LOCK_UN`**（FD handoff）。

        flock 锁绑定在 open-file-description 上：经 fork/pass_fds 继承的 FD 与
        本对象的 FD 共享同一个锁实例。若在子进程（launcher→DSH）仍持有该 OFD 时
        执行 `LOCK_UN`，会把子进程的租约一起解掉——恰好破坏"旧 activation 活着
        时绝不 spawn 第二个 Parent"。

        因此唯一正确的解锁路径是关闭 FD：当本副本被 close 后，锁仍由仍持有该
        OFD 的子进程（DSH/其后代）继续持有；直到最后一个副本关闭（内核自动
        释放锁）。若从未成功 spawn（本副本是唯一持有者），close 后锁即消失。
        """
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    @property
    def fd(self) -> int:
        return self._fd