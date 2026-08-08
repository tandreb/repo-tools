"""Derive manifest location (URL, branch, root file) from an existing `repo`-tool workspace.

Only reads local git metadata that `repo init`/`repo sync` already wrote to disk under
.repo/ (the manifests checkout's remote URL and current branch, the manifest.xml symlink
target) -- never repo *content*, and nothing is fetched, cloned, or modified.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


class RepoWorkspaceError(Exception):
    pass


@dataclass
class RepoWorkspaceInfo:
    manifest_url: str
    manifest_branch: str
    manifest_file: str
    local_manifest_dir: str | None


def _find_repo_dir(path: Path) -> Path:
    if path.name == ".repo" and path.is_dir():
        return path
    candidate = path / ".repo"
    if candidate.is_dir():
        return candidate
    raise RepoWorkspaceError(f"no .repo directory found at or under {path}")


def _git(args: list[str], cwd: Path) -> str:
    try:
        result = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=10, check=True
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        raise RepoWorkspaceError(f"git {' '.join(args)} failed in {cwd}: {exc}") from exc
    return result.stdout.strip()


def _git_optional(args: list[str], cwd: Path) -> str:
    """Like _git, but returns "" instead of raising (used for lookups that may legitimately be absent)."""
    try:
        result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=10)
    except OSError:
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def resolve_from_repo_dir(path: str | Path) -> RepoWorkspaceInfo:
    repo_dir = _find_repo_dir(Path(path))

    manifests_dir = repo_dir / "manifests"
    if not manifests_dir.is_dir():
        raise RepoWorkspaceError(f"{manifests_dir} not found -- is this a `repo init`-ed workspace?")

    manifest_url = _git(["remote", "get-url", "origin"], cwd=manifests_dir)

    local_branch = _git(["symbolic-ref", "--short", "-q", "HEAD"], cwd=manifests_dir)
    if not local_branch:
        raise RepoWorkspaceError(
            f"{manifests_dir} has a detached HEAD, so the manifest branch can't be inferred; "
            "pass --manifest-branch explicitly"
        )
    # `repo init` checks .repo/manifests out onto a local branch literally named "default" that
    # tracks the real manifest branch via upstream config -- the local branch name itself is not
    # the manifest branch. Read the real name from `branch.<local>.merge` (set by `repo init`),
    # falling back to the local branch name for non-standard setups where that config is absent.
    merge_ref = _git_optional(["config", "--get", f"branch.{local_branch}.merge"], cwd=manifests_dir)
    branch = merge_ref.removeprefix("refs/heads/") if merge_ref else local_branch

    manifest_link = repo_dir / "manifest.xml"
    if manifest_link.is_symlink():
        try:
            target = manifest_link.resolve(strict=True)
        except OSError as exc:
            raise RepoWorkspaceError(f"could not resolve symlink {manifest_link}: {exc}") from exc
        try:
            # The root manifest file can live in a subdirectory of the manifest repo (e.g.
            # "xml/default.xml") -- taking only the basename would drop that prefix and make
            # us look for the file at the wrong path when fetching from the manifest repo.
            manifest_file = target.relative_to(manifests_dir.resolve(strict=True)).as_posix()
        except ValueError:
            manifest_file = target.name
    elif manifest_link.is_file():
        # Some repo-tool variants write manifest.xml as a plain copy instead of a symlink, which
        # loses the information we need (its path within the manifest repo). We can't safely guess
        # a filename here -- silently defaulting to "default.xml" produced confusing fetch failures
        # when that guess was wrong.
        raise RepoWorkspaceError(
            f"{manifest_link} is a regular file, not a symlink, so the root manifest file name/path "
            "can't be determined automatically; pass --manifest-file explicitly (and --manifest-branch "
            "if it also isn't the correct default)"
        )
    else:
        raise RepoWorkspaceError(
            f"{manifest_link} not found; pass --manifest-file explicitly to specify the root manifest file"
        )

    local_manifests = repo_dir / "local_manifests"
    local_manifest_dir = str(local_manifests) if local_manifests.is_dir() else None

    return RepoWorkspaceInfo(
        manifest_url=manifest_url,
        manifest_branch=branch,
        manifest_file=manifest_file,
        local_manifest_dir=local_manifest_dir,
    )
