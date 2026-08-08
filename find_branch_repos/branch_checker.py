"""Parallel, read-only `git ls-remote` checks for whether a branch exists on many remotes."""

from __future__ import annotations

import asyncio
import logging
import os
import re

from .models import ProjectRef

logger = logging.getLogger(__name__)

GIT_ALLOWED_PROTOCOLS = "http:https:ssh:git"

# refs/heads/<branch> component: no leading '-' (would be parsed as a git option), no whitespace,
# no shell/control characters. Deliberately conservative rather than exhaustive.
_SAFE_BRANCH_RE = re.compile(r"^(?!-)[A-Za-z0-9._/-]+$")


class InvalidBranchNameError(ValueError):
    pass


def validate_branch_name(branch: str) -> str:
    if not branch or not _SAFE_BRANCH_RE.match(branch) or branch.startswith("/") or ".." in branch:
        raise InvalidBranchNameError(f"refusing to use unsafe branch name: {branch!r}")
    return branch


class BranchCheckError(Exception):
    def __init__(self, project: ProjectRef, message: str):
        super().__init__(message)
        self.project = project
        self.message = message


async def check_branch(project: ProjectRef, branch: str, semaphore: asyncio.Semaphore, timeout: float) -> str | None:
    """Returns the branch's SHA if it exists on the remote, else None. Raises BranchCheckError on failure."""
    env = dict(os.environ, GIT_ALLOW_PROTOCOL=GIT_ALLOWED_PROTOCOLS)
    async with semaphore:
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "ls-remote", "--heads", "--", project.fetch_url, f"refs/heads/{branch}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except OSError as exc:
            raise BranchCheckError(project, f"could not start git: {exc}") from exc

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise BranchCheckError(project, f"timed out after {timeout}s")

        if proc.returncode != 0:
            raise BranchCheckError(project, stderr.decode(errors="replace").strip() or f"git exited with code {proc.returncode}")

    line = stdout.decode(errors="replace").strip()
    if not line:
        return None
    sha = line.split(maxsplit=1)[0]
    return sha


async def check_branch_on_all(
    projects: list[ProjectRef],
    branch: str,
    concurrency: int,
    timeout: float,
) -> tuple[dict[ProjectRef, str], list[BranchCheckError]]:
    branch = validate_branch_name(branch)
    semaphore = asyncio.Semaphore(concurrency)
    hits: dict[ProjectRef, str] = {}
    errors: list[BranchCheckError] = []

    async def _run(project: ProjectRef) -> None:
        try:
            sha = await check_branch(project, branch, semaphore, timeout)
        except BranchCheckError as exc:
            errors.append(exc)
            return
        if sha is not None:
            hits[project] = sha

    await asyncio.gather(*(_run(p) for p in projects))
    return hits, errors
