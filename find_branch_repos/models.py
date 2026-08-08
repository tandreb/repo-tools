from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ProjectRef:
    """A single <project> resolved from the manifest tree, with its clone URL already computed."""

    name: str  # path on the git server, e.g. "platform/frameworks/base"
    path: str  # local checkout path per the manifest
    remote_alias: str
    fetch_url: str
    revision: str
    groups: tuple[str, ...] = ()
    source_manifest: str = ""  # which manifest file/submanifest defined it, for debugging

    @property
    def key(self) -> str:
        # a project can appear at the same path from different submanifests; name+path is unique enough
        return f"{self.path}::{self.name}"


@dataclass
class CommitMetadata:
    sha: str
    author: str | None
    date: str | None
    subject: str | None
    source: str  # provider name that supplied this data, e.g. "gitiles", "github-api", "git-fetch-fallback"


@dataclass
class BranchResult:
    project: ProjectRef
    sha: str
    metadata: CommitMetadata | None = None


@dataclass
class RepoError:
    project: ProjectRef
    stage: str  # "ls-remote" | "metadata"
    message: str


@dataclass
class RunSummary:
    branch: str
    manifest_url: str
    manifest_branch: str
    projects_total: int = 0
    # Every project the manifest resolved to, hit or not. Without this, a run that finds nothing
    # gives no way to tell whether the expected repo was even searched, and under which URL.
    projects: list[ProjectRef] = field(default_factory=list)
    hits: list[BranchResult] = field(default_factory=list)
    errors: list[RepoError] = field(default_factory=list)
    # Parts of the manifest that could not be resolved (e.g. an unreachable submanifest), meaning
    # the project list -- and therefore the result -- is incomplete rather than merely degraded.
    warnings: list[str] = field(default_factory=list)
