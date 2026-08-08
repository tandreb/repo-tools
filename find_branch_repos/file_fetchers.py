"""Read a single file's content from a remote git repo at a given ref, without a persistent clone.

Providers are tried in priority order (pure-HTTP APIs first, minimal shallow
`git fetch` only as a last resort - see MinimalGitFileFetcher).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)

GIT_ALLOWED_PROTOCOLS = "http:https:ssh:git"


class FileFetchError(Exception):
    pass


class FileFetcher(Protocol):
    name: str

    def can_handle(self, fetch_url: str) -> bool: ...

    def fetch(self, fetch_url: str, ref: str, path: str) -> bytes: ...


def _http_get(url: str, headers: dict[str, str] | None = None, timeout: float = 15.0) -> bytes:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise FileFetchError(f"HTTP {exc.code} for {url}") from exc
    except urllib.error.URLError as exc:
        raise FileFetchError(f"network error for {url}: {exc.reason}") from exc


@dataclass
class GitilesFileFetcher:
    """Gitiles raw-file API, typical for AOSP/Gerrit-hosted repo-tool setups."""

    host_markers: tuple[str, ...] = ("googlesource.com",)
    name: str = "gitiles"

    def can_handle(self, fetch_url: str) -> bool:
        host = urllib.parse.urlsplit(fetch_url).netloc
        return any(marker in host for marker in self.host_markers)

    def fetch(self, fetch_url: str, ref: str, path: str) -> bytes:
        url = f"{fetch_url.rstrip('/')}/+/{urllib.parse.quote(ref)}/{path}?format=TEXT"
        raw = _http_get(url)
        try:
            return base64.b64decode(raw)
        except (ValueError, base64.binascii.Error) as exc:
            raise FileFetchError(f"gitiles returned non-base64 content for {url}") from exc


@dataclass
class GitHubApiFileFetcher:
    name: str = "github-api"
    token: str | None = None

    def can_handle(self, fetch_url: str) -> bool:
        return "github.com" in urllib.parse.urlsplit(fetch_url).netloc

    def _owner_repo(self, fetch_url: str) -> tuple[str, str]:
        parts = urllib.parse.urlsplit(fetch_url).path.strip("/").removesuffix(".git").split("/")
        if len(parts) < 2:
            raise FileFetchError(f"cannot parse owner/repo from {fetch_url}")
        return parts[0], parts[1]

    def fetch(self, fetch_url: str, ref: str, path: str) -> bytes:
        owner, repo = self._owner_repo(fetch_url)
        url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}?ref={urllib.parse.quote(ref)}"
        headers = {"Accept": "application/vnd.github.raw+json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        raw = _http_get(url, headers=headers)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            # Accept header above can already return raw bytes on some GH Enterprise versions
            return raw
        if payload.get("encoding") != "base64":
            raise FileFetchError(f"unexpected encoding from GitHub API for {url}")
        return base64.b64decode(payload["content"])


@dataclass
class GitLabApiFileFetcher:
    name: str = "gitlab-api"
    token: str | None = None
    host_markers: tuple[str, ...] = ("gitlab",)

    def can_handle(self, fetch_url: str) -> bool:
        host = urllib.parse.urlsplit(fetch_url).netloc
        return any(marker in host for marker in self.host_markers)

    def fetch(self, fetch_url: str, ref: str, path: str) -> bytes:
        split = urllib.parse.urlsplit(fetch_url)
        project_path = split.path.strip("/").removesuffix(".git")
        project_id = urllib.parse.quote(project_path, safe="")
        file_path = urllib.parse.quote(path, safe="")
        url = (
            f"{split.scheme}://{split.netloc}/api/v4/projects/{project_id}"
            f"/repository/files/{file_path}/raw?ref={urllib.parse.quote(ref)}"
        )
        headers = {}
        if self.token:
            headers["PRIVATE-TOKEN"] = self.token
        return _http_get(url, headers=headers)


@dataclass
class MinimalGitFileFetcher:
    """Last-resort fallback: a minimal, temporary, depth=1 fetch into a throwaway bare repo.

    No working tree is created, nothing is written outside a TemporaryDirectory that is
    removed immediately after use, and the developer's actual checkouts are never touched.
    Used only when no HTTP API provider can serve the request.
    """

    name: str = "git-fetch-fallback"
    timeout: float = 30.0

    def can_handle(self, fetch_url: str) -> bool:
        return True  # universal fallback

    def fetch(self, fetch_url: str, ref: str, path: str) -> bytes:
        with tempfile.TemporaryDirectory(prefix="fbr-manifest-") as tmpdir:
            env = dict(os.environ, GIT_ALLOW_PROTOCOL=GIT_ALLOWED_PROTOCOLS)
            self._run(["git", "init", "--quiet", "--bare", tmpdir], env=env)
            self._run(
                ["git", "-C", tmpdir, "fetch", "--depth=1", "--quiet", fetch_url, ref],
                env=env,
            )
            result = self._run(
                ["git", "-C", tmpdir, "show", f"FETCH_HEAD:{path}"],
                env=env,
                capture=True,
            )
            return result

    def _run(self, cmd: list[str], env: dict[str, str], capture: bool = False) -> bytes:
        try:
            proc = subprocess.run(
                cmd,
                env=env,
                timeout=self.timeout,
                capture_output=True,
                check=True,
            )
        except subprocess.TimeoutExpired as exc:
            raise FileFetchError(f"timeout running {' '.join(cmd)}") from exc
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.decode(errors="replace") if exc.stderr else ""
            raise FileFetchError(f"'{' '.join(cmd)}' failed: {stderr.strip()}") from exc
        return proc.stdout


class FileFetcherChain:
    """Tries providers in order, falling back on failure; records which provider succeeded."""

    def __init__(self, providers: list[FileFetcher]):
        if not providers:
            raise ValueError("at least one provider is required")
        self.providers = providers

    def fetch(self, fetch_url: str, ref: str, path: str) -> tuple[bytes, str]:
        errors: list[str] = []
        for provider in self.providers:
            if not provider.can_handle(fetch_url):
                continue
            try:
                content = provider.fetch(fetch_url, ref, path)
                return content, provider.name
            except FileFetchError as exc:
                logger.debug("provider %s failed for %s: %s", provider.name, fetch_url, exc)
                errors.append(f"{provider.name}: {exc}")
        raise FileFetchError(
            f"all providers failed to fetch {path}@{ref} from {fetch_url}: {'; '.join(errors)}"
        )


def default_chain(github_token: str | None = None, gitlab_token: str | None = None, allow_fetch_fallback: bool = True) -> FileFetcherChain:
    providers: list[FileFetcher] = [
        GitilesFileFetcher(),
        GitHubApiFileFetcher(token=github_token),
        GitLabApiFileFetcher(token=gitlab_token),
    ]
    if allow_fetch_fallback:
        providers.append(MinimalGitFileFetcher())
    return FileFetcherChain(providers)
