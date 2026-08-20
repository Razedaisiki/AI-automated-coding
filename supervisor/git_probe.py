"""只读 Git 机械探测（M6 V1 — GitSnapshot V1）.

Supervisor 只看结构：is_git_repo / branch / HEAD / detached / dirty / remote。
绝不看 diff 内容，绝不执行 add/commit/checkout/reset/merge/rebase/push。
"""

import subprocess
from pathlib import Path
from typing import Optional

from .models import GitSnapshot, compare_git_snapshots


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
        return GitSnapshot(is_git_repo=False)

    branch = None
    detached_head = False
    r = _git(repo, "symbolic-ref", "--short", "-q", "HEAD")
    if r is not None and r.returncode == 0 and r.stdout.strip():
        branch = r.stdout.strip()
    else:
        # Could be detached HEAD or empty repo — check if HEAD exists
        detached_head = True
        # Verify HEAD actually exists (has commits); if not, keep detached_head True but head=None
        branch = None

    head = None
    r = _git(repo, "rev-parse", "-q", "HEAD")
    if r is not None and r.returncode == 0 and r.stdout.strip():
        head = r.stdout.strip()
    else:
        head = None

    # If no head (empty repo), detached_head is not meaningful — set False
    if head is None:
        detached_head = False
    elif branch is not None:
        detached_head = False

    dirty = False
    r = _git(repo, "status", "--porcelain")
    if r is not None and r.returncode == 0 and r.stdout.strip():
        dirty = True

    has_remote = False
    remote_url = None
    # Check origin first
    r = _git(repo, "remote", "get-url", "origin")
    if r is not None and r.returncode == 0 and r.stdout.strip():
        has_remote = True
        remote_url = r.stdout.strip()
    else:
        # Check any remote exists (not just origin)
        r2 = _git(repo, "remote")
        if r2 is not None and r2.returncode == 0 and r2.stdout.strip():
            has_remote = True
            # Deterministic: first remote sorted alphabetically
            remotes = sorted(r2.stdout.strip().splitlines())
            first = remotes[0].strip() if remotes else None
            if first:
                r3 = _git(repo, "remote", "get-url", first)
                if r3 is not None and r3.returncode == 0 and r3.stdout.strip():
                    remote_url = r3.stdout.strip()

    return GitSnapshot(
        is_git_repo=True,
        branch=branch,
        head=head,
        detached_head=detached_head,
        dirty=dirty,
        has_remote=has_remote,
        remote_url=remote_url,
    )
