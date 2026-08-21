"""Fake target repo fixture: real Git + bare remote + real commit/push."""
import subprocess
import shutil
from pathlib import Path


def _run(*args, cwd=None, check=True):
    return subprocess.run(list(args), cwd=str(cwd) if cwd else None, check=check, capture_output=True, text=True)


def make_fake_target(base_tmp: Path, name: str = "target") -> dict:
    repo = base_tmp / name
    remote = base_tmp / f"{name}.remote.git"
    if remote.exists():
        shutil.rmtree(remote, ignore_errors=True)
    if repo.exists():
        shutil.rmtree(repo, ignore_errors=True)
    repo.mkdir(parents=True)
    _run("git", "init", "-q", cwd=repo)
    _run("git", "config", "user.email", "t@t", cwd=repo)
    _run("git", "config", "user.name", "t", cwd=repo)
    (repo / ".gitignore").write_text(".supervisor/\n.agent/\n", encoding="utf-8")
    _run("git", "init", "--bare", "-q", str(remote))
    _run("git", "remote", "add", "origin", str(remote), cwd=repo)
    (repo / "pkg").mkdir(exist_ok=True)
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "app.py").write_text("def add(a,b): return a+b\n", encoding="utf-8")
    (repo / "tests").mkdir(exist_ok=True)
    (repo / "tests" / "test_app.py").write_text("from pkg.app import add\ndef test_add(): assert add(1,2)==3\n", encoding="utf-8")
    # small validation command is `python -m pytest -q`
    (repo / ".supervisor").mkdir(exist_ok=True)
    (repo / ".supervisor" / "task.md").write_text("# Task\n\nImplement feature and make tests pass.\n", encoding="utf-8")
    (repo / "supervisor.toml").write_text(
        'version = 1\n[dsh]\nexecutable = "dsh"\nprofile = "headless"\n\n[task]\nfile = ".supervisor/task.md"\n\n[limits]\nmax_parent_activations=50\nmax_crash_restarts=20\nmax_clean_restarts=50\nmax_timeouts=10\nmax_ci_wakeups=20\nparent_timeout_seconds=30\nterminate_grace_seconds=1\nmax_active_wall_seconds=3600\n\n[restart]\nbackoff_seconds=[0.01]\n\n[ci]\nenabled=false\nprovider="fake"\npoll_seconds=1\ndiscovery_grace_seconds=1\nmax_wait_seconds=30\n\n[human]\npause_active_wall_clock=true\n',
        encoding="utf-8",
    )
    _run("git", "add", "-A", cwd=repo)
    _run("git", "commit", "-q", "-m", "init", cwd=repo)
    sha0 = _run("git", "rev-parse", "HEAD", cwd=repo).stdout.strip()
    _run("git", "push", "-q", "origin", "HEAD", cwd=repo)
    return {"repo": repo, "remote": remote, "sha0": sha0}


def rev_parse(repo: Path, rev: str = "HEAD") -> str:
    return _run("git", "rev-parse", rev, cwd=repo).stdout.strip()


def remote_contains(remote: Path, sha: str) -> bool:
    r = subprocess.run(["git", "--git-dir", str(remote), "cat-file", "-e", sha + "^{commit}"], capture_output=True)
    return r.returncode == 0


def validation_passes(repo: Path) -> bool:
    r = subprocess.run(["python3", "-m", "pytest", "-q"], cwd=str(repo), capture_output=True)
    return r.returncode == 0
