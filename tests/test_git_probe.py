"""M6（轻量）— 只读 Git 探针测试。"""

import subprocess
from pathlib import Path

from supervisor.git_probe import GitSnapshot, capture


class TestGitProbe:
    def test_non_git_repo_all_none(self, tmp_path):
        snap = capture(tmp_path)
        assert snap.branch is None
        assert snap.head is None
        assert snap.dirty is False
        assert snap.has_remote is False
        assert snap.remote_url is None

    def test_git_repo_snapshot(self, tmp_path):
        repo = tmp_path / "g"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(repo), check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=str(repo), check=True)
        (repo / "f.txt").write_text("hello\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(repo), check=True)
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True
        ).stdout.strip()

        snap = capture(repo)
        assert snap.branch == "master" or snap.branch == "main"
        assert snap.head == head
        assert snap.dirty is False
        assert snap.has_remote is False

        (repo / "f.txt").write_text("changed\n", encoding="utf-8")
        snap2 = capture(repo)
        assert snap2.dirty is True
        assert snap2.head == head  # 未提交 → HEAD 不变

    def test_to_dict_json_serializable(self, tmp_path):
        snap = capture(tmp_path)
        import json

        json.dumps(snap.to_dict())