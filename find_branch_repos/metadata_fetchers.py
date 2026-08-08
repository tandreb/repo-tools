"""Fetch commit metadata (author/date/subject) for a known branch, without a persistent clone.

Same provider-chain idea as file_fetchers.py. Only called for repos where the branch
was already confirmed to exist via `git ls-remote`, so the (rare) minimal-fetch fallback
only ever runs against actual hits, not the whole fleet.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Protocol

from .models import CommitMetadata

logger = logging.getLogger(__name__)

GIT_ALLOWED_PROTOCOLS = "http:https:ssh:git"


class MetadataFetchError(Exception):
    pass


class MetadataFetcher(Protocol):
    name: str

    def can_handle(self, fetch_url: str) -> bool: ...

    def fetch(self, fetch_url: str, branch: str, sha: str) -> CommitMetadata: ...


def _http_get_json(url: str, headers: dict[str, str] | None = None, timeout: float = 15.0, strip_prefix: str | None = None) -> dict:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise MetadataFetchError(f"HTTP {exc.code} for {url}") from exc
    except urllib.error.URLError as exc:
        raise MetadataFetchError(f"network error for {url}: {exc.reason}") from exc
    text = raw.decode("utf-8", errors="replace")
    if strip_prefix and text.startswith(strip_prefix):
        text = text[len(strip_prefix):]
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise MetadataFetchError(f"invalid JSON from {url}: {exc}") from exc


@dataclass
class GitilesLogFetcher:
    host_markers: tuple[str, ...] = ("googlesource.com",)
    name: str = "gitiles"

    def can_handle(self, fetch_url: str) -> bool:
        host = urllib.parse.urlsplit(fetch_url).netloc
        return any(marker in host for marker in self.host_markers)

    def fetch(self, fetch_url: str, branch: str, sha: str) -> CommitMetadata:
        url = f"{fetch_url.rstrip('/')}/+log/{urllib.parse.quote(branch)}?n=1&format=JSON"
        # Gitiles prefixes JSON responses with ")]}'" as an XSSI guard
        payload = _http_get_json(url, strip_prefix=")]}'")
        entries = payload.get("log") or []
        if not entries:
            raise MetadataFetchError(f"empty log from {url}")
        entry = entries[0]
        author = entry.get("author", {})
        subject = (entry.get("message") or "").splitlines()[0] if entry.get("message") else None
        return CommitMetadata(
            sha=entry.get("commit", sha),
            author=author.get("name") or author.get("email"),
            date=author.get("time"),
            subject=subject,
            source=self.name,
        )


@dataclass
class GitHubApiCommitFetcher:
    name: str = "github-api"
    token: str | None = None

    def can_handle(self, fetch_url: str) -> bool:
        return "github.com" in urllib.parse.urlsplit(fetch_url).netloc

    def _owner_repo(self, fetch_url: str) -> tuple[str, str]:
        parts = urllib.parse.urlsplit(fetch_url).path.strip("/").removesuffix(".git").split("/")
        if len(parts) < 2:
            raise MetadataFetchError(f"cannot parse owner/repo from {fetch_url}")
        return parts[0], parts[1]

    def fetch(self, fetch_url: str, branch: str, sha: str) -> CommitMetadata:
        owner, repo = self._owner_repo(fetch_url)
        url = f"https://api.github.com/repos/{owner}/{repo}/commits/{urllib.parse.quote(branch)}"
        headers = {"Accept": "application/vnd.github+json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        payload = _http_get_json(url, headers=headers)
        commit = payload.get("commit", {})
        author = commit.get("author", {})
        message = commit.get("message") or ""
        return CommitMetadata(
            sha=payload.get("sha", sha),
            author=author.get("name") or author.get("email"),
            date=author.get("date"),
            subject=message.splitlines()[0] if message else None,
            source=self.name,
        )


@dataclass
class GitLabApiCommitFetcher:
    name: str = "gitlab-api"
    token: str | None = None
    host_markers: tuple[str, ...] = ("gitlab",)

    def can_handle(self, fetch_url: str) -> bool:
        host = urllib.parse.urlsplit(fetch_url).netloc
        return any(marker in host for marker in self.host_markers)

    def fetch(self, fetch_url: str, branch: str, sha: str) -> CommitMetadata:
        split = urllib.parse.urlsplit(fetch_url)
        project_path = split.path.strip("/").removesuffix(".git")
        project_id = urllib.parse.quote(project_path, safe="")
        url = f"{split.scheme}://{split.netloc}/api/v4/projects/{project_id}/repository/commits/{urllib.parse.quote(branch)}"
        headers = {}
        if self.token:
            headers["PRIVATE-TOKEN"] = self.token
        payload = _http_get_json(url, headers=headers)
        return CommitMetadata(
            sha=payload.get("id", sha),
            author=payload.get("author_name"),
            date=payload.get("committed_date"),
            subject=(payload.get("title") or (payload.get("message") or "").splitlines()[0] if payload.get("message") else payload.get("title")),
            source=self.name,
        )


@dataclass
class MinimalGitCommitFetcher:
    """Last-resort fallback: single-commit depth=1 fetch of an already-known sha into a throwaway bare repo.

    Only invoked for confirmed branch hits (never for the whole fleet), and never leaves
    anything behind: everything happens inside a TemporaryDirectory removed right after use.
    """

    name: str = "git-fetch-fallback"
    timeout: float = 30.0
    _FIELD_SEP: str = "\x1f"

    def can_handle(self, fetch_url: str) -> bool:
        return True

    def fetch(self, fetch_url: str, branch: str, sha: str) -> CommitMetadata:
        with tempfile.TemporaryDirectory(prefix="fbr-meta-") as tmpdir:
            env = dict(os.environ, GIT_ALLOW_PROTOCOL=GIT_ALLOWED_PROTOCOLS)
            self._run(["git", "init", "--quiet", "--bare", tmpdir], env=env)
            self._run(["git", "-C", tmpdir, "fetch", "--depth=1", "--quiet", fetch_url, sha], env=env)
            fmt = f"%H{self._FIELD_SEP}%an{self._FIELD_SEP}%aI{self._FIELD_SEP}%s"
            out = self._run(["git", "-C", tmpdir, "log", "-1", f"--format={fmt}", "FETCH_HEAD"], env=env, capture=True)
            parts = out.decode("utf-8", errors="replace").strip("\n").split(self._FIELD_SEP)
            if len(parts) != 4:
                raise MetadataFetchError(f"unexpected git log output: {out!r}")
            commit_sha, author, date, subject = parts
            return CommitMetadata(sha=commit_sha, author=author, date=date, subject=subject, source=self.name)

    def _run(self, cmd: list[str], env: dict[str, str], capture: bool = False) -> bytes:
        try:
            proc = subprocess.run(cmd, env=env, timeout=self.timeout, capture_output=True, check=True)
        except subprocess.TimeoutExpired as exc:
            raise MetadataFetchError(f"timeout running {' '.join(cmd)}") from exc
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.decode(errors="replace") if exc.stderr else ""
            raise MetadataFetchError(f"'{' '.join(cmd)}' failed: {stderr.strip()}") from exc
        return proc.stdout


class MetadataFetcherChain:
    def __init__(self, providers: list[MetadataFetcher]):
        if not providers:
            raise ValueError("at least one provider is required")
        self.providers = providers

    def fetch(self, fetch_url: str, branch: str, sha: str) -> CommitMetadata:
        errors: list[str] = []
        for provider in self.providers:
            if not provider.can_handle(fetch_url):
                continue
            try:
                return provider.fetch(fetch_url, branch, sha)
            except MetadataFetchError as exc:
                logger.debug("metadata provider %s failed for %s: %s", provider.name, fetch_url, exc)
                errors.append(f"{provider.name}: {exc}")
        raise MetadataFetchError(f"all providers failed to fetch metadata for {branch}@{fetch_url}: {'; '.join(errors)}")


def default_chain(github_token: str | None = None, gitlab_token: str | None = None, allow_fetch_fallback: bool = True) -> MetadataFetcherChain:
    providers: list[MetadataFetcher] = [
        GitilesLogFetcher(),
        GitHubApiCommitFetcher(token=github_token),
        GitLabApiCommitFetcher(token=gitlab_token),
    ]
    if allow_fetch_fallback:
        providers.append(MinimalGitCommitFetcher())
    return MetadataFetcherChain(providers)
