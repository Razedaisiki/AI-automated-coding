"""只读 Git 机械探测（M6 轻量版）。

Supervisor 只看结构：branch / HEAD / dirty / remote。绝不看 diff 内容，
绝不执行 add/commit/checkout/reset/merge/rebase/push。
"""

import subprocess
from pathlib import Path

from .models import GitSnapshot


def _git(repo, *args):
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def capture(repo) -> GitSnapshot:
    repo = Path(repo)
    inside = _git(repo, "rev-parse", "--is-inside-work-tree")
    if inside is None or inside.returncode != 0 or inside.stdout.strip() != "true":
        return GitSnapshot()  # 非 git 仓库：全空

    branch = None
    r = _git(repo, "symbolic-ref", "--short", "-q", "HEAD")
    if r is not None and r.returncode == 0 and r.stdout.strip():
        branch = r.stdout.strip()

    head = None
    r = _git(repo, "rev-parse", "-q", "HEAD")
    if r is not None and r.returncode == 0 and r.stdout.strip():
        head = r.stdout.strip()

    dirty = False
    r = _git(repo, "status", "--porcelain")
    if r is not None and r.returncode == 0 and r.stdout.strip():
        dirty = True

    has_remote = False
    remote_url = None
    r = _git(repo, "remote", "get-url", "origin")
    if r is not None and r.returncode == 0 and r.stdout.strip():
        has_remote = True
        remote_url = r.stdout.strip()

    return GitSnapshot(
        branch=branch,
        head=head,
        dirty=dirty,
        has_remote=has_remote,
        remote_url=remote_url,
    )