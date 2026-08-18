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