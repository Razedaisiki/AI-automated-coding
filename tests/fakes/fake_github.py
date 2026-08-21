"""Persistent Fake PR store for Parent test env (file-backed, not Supervisor-owned)."""
import json
import os
import fcntl
from contextlib import contextmanager
from pathlib import Path
from typing import Optional


class FakePrStore:
    def __init__(self, store_dir: Path):
        self.dir = Path(store_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = self.dir / ".lock"

    @contextmanager
    def _locked(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self._lock), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except Exception:
                pass
            os.close(fd)

    def _index_path(self) -> Path:
        return self.dir / "index.json"

    def _pr_path(self, number: int) -> Path:
        return self.dir / f"pr-{number:06d}.json"

    def _load_index(self) -> dict:
        p = self._index_path()
        if not p.exists():
            return {"next_number": 1, "prs": []}
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {"next_number": 1, "prs": []}

    def _save_index(self, idx: dict):
        tmp = self._index_path().with_suffix(".json.tmp")
        tmp.write_text(json.dumps(idx, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self._index_path())

    def list(self):
        idx = self._load_index()
        out = []
        for n in idx.get("prs", []):
            p = self._pr_path(n)
            if p.exists():
                try:
                    out.append(json.loads(p.read_text(encoding="utf-8")))
                except Exception:
                    continue
        return out

    def create(self, branch: str, head_sha: str, title: str = "PR", body: str = "") -> dict:
        with self._locked():
            idx = self._load_index()
            number = int(idx.get("next_number", 1))
            pr = {"number": number, "branch": branch, "head_sha": head_sha, "title": title, "body": body, "state": "OPEN"}
            tmp = self._pr_path(number).with_suffix(".json.tmp")
            tmp.write_text(json.dumps(pr, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, self._pr_path(number))
            idx["prs"] = sorted(set(idx.get("prs", []) + [number]))
            idx["next_number"] = number + 1
            self._save_index(idx)
            return pr

    def find_by_branch(self, branch: str) -> Optional[dict]:
        for pr in self.list():
            if pr.get("branch") == branch:
                return pr
        return None

    def update(self, number: int, head_sha: Optional[str] = None, body: Optional[str] = None) -> Optional[dict]:
        with self._locked():
            p = self._pr_path(number)
            if not p.exists():
                return None
            pr = json.loads(p.read_text(encoding="utf-8"))
            if head_sha is not None:
                pr["head_sha"] = head_sha
            if body is not None:
                pr["body"] = body
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(pr, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, p)
            return pr

    def read(self, number: int) -> Optional[dict]:
        p = self._pr_path(number)
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))

    def count(self) -> int:
        return len(self.list())
