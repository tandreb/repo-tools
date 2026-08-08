"""Helper to create a small real local git repo used as a fixture in several tests.

Using a real local repo (instead of mocking `git` itself) gives genuine confidence
that git ls-remote/fetch/log invocations and their argument handling actually work.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def _run(*args: str, cwd: Path) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


def make_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    _run("git", "init", "-q", "-b", "main", cwd=repo)
    _run("git", "config", "user.email", "test@example.com", cwd=repo)
    _run("git", "config", "user.name", "Test User", cwd=repo)

    (repo / "f.txt").write_text("hello\n")
    _run("git", "add", "f.txt", cwd=repo)
    _run("git", "commit", "-q", "-m", "init", cwd=repo)

    _run("git", "checkout", "-q", "-b", "feature/xyz", cwd=repo)
    (repo / "f.txt").write_text("hello again\n")
    _run("git", "commit", "-q", "-am", "feature commit", cwd=repo)
    _run("git", "checkout", "-q", "main", cwd=repo)

    return repo


def rev_parse(repo: Path, ref: str) -> str:
    out = subprocess.run(
        ["git", "rev-parse", ref], cwd=repo, check=True, capture_output=True, text=True
    )
    return out.stdout.strip()
