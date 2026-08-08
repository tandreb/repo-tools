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
    # Content of a locally generated .repo/manifest.xml, when that file is a real file rather
    # than a symlink into .repo/manifests/ (see resolve_from_repo_dir). It is used as the root
    # manifest directly; its <include>s still resolve against the remote manifest repo.
    root_manifest_xml: bytes | None = None


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

    # `symbolic-ref -q` exits non-zero on a detached HEAD, so this must not use the checking
    # variant -- otherwise the detached case dies with a raw git error instead of the hint below.
    local_branch = _git_optional(["symbolic-ref", "--short", "-q", "HEAD"], cwd=manifests_dir)
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

    manifest_path = repo_dir / "manifest.xml"
    root_manifest_xml: bytes | None = None
    if manifest_path.is_symlink():
        # Older repo versions symlink .repo/manifest.xml into .repo/manifests/.
        try:
            target = manifest_path.resolve(strict=True)
        except OSError as exc:
            raise RepoWorkspaceError(f"could not resolve symlink {manifest_path}: {exc}") from exc
        try:
            # The root manifest file can live in a subdirectory of the manifest repo (e.g.
            # "xml/default.xml") -- taking only the basename would drop that prefix and make
            # us look for the file at the wrong path when fetching from the manifest repo.
            manifest_file = target.relative_to(manifests_dir.resolve(strict=True)).as_posix()
        except ValueError:
            manifest_file = target.name
    elif manifest_path.is_file():
        # Current repo versions no longer symlink; they generate a small stub that points at the
        # real manifest via <include name="..."/>. repo documents that this file "is interpreted
        # as if it existed inside the manifest repo", so using its content as the root manifest
        # makes those includes resolve against the manifest repo exactly as repo intends -- and
        # it works regardless of what the real file is called or where it lives.
        try:
            root_manifest_xml = manifest_path.read_bytes()
        except OSError as exc:
            raise RepoWorkspaceError(f"could not read {manifest_path}: {exc}") from exc
        manifest_file = str(manifest_path)
    else:
        raise RepoWorkspaceError(
            f"{manifest_path} not found -- is this a `repo init`-ed workspace? "
            "Pass --manifest-file (and --manifest-url) explicitly instead."
        )

    local_manifests = repo_dir / "local_manifests"
    local_manifest_dir = str(local_manifests) if local_manifests.is_dir() else None

    return RepoWorkspaceInfo(
        manifest_url=manifest_url,
        manifest_branch=branch,
        manifest_file=manifest_file,
        local_manifest_dir=local_manifest_dir,
        root_manifest_xml=root_manifest_xml,
    )
