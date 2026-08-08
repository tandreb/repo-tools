from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from find_branch_repos import metadata_fetchers
from find_branch_repos.metadata_fetchers import (
    GitHubApiCommitFetcher,
    GitilesLogFetcher,
    GitLabApiCommitFetcher,
    MetadataFetchError,
    MetadataFetcherChain,
    MinimalGitCommitFetcher,
)
from tests.gitutil import make_repo, rev_parse


class _FakeResponse:
    def __init__(self, data: bytes):
        self._data = data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._data


class GitilesLogFetcherTest(unittest.TestCase):
    @patch("find_branch_repos.metadata_fetchers.urllib.request.urlopen")
    def test_fetch_strips_xssi_prefix_and_parses(self, mock_urlopen):
        body = {
            "log": [
                {
                    "commit": "abc123",
                    "author": {"name": "Ada", "email": "ada@example.com", "time": "Mon Jan 1 00:00:00 2026"},
                    "message": "Fix the thing\n\nlonger body\n",
                }
            ]
        }
        mock_urlopen.return_value = _FakeResponse((")]}'\n" + json.dumps(body)).encode())
        fetcher = GitilesLogFetcher()
        meta = fetcher.fetch("https://android.googlesource.com/platform/x", "feature/xyz", "fallback-sha")
        self.assertEqual(meta.sha, "abc123")
        self.assertEqual(meta.author, "Ada")
        self.assertEqual(meta.subject, "Fix the thing")
        self.assertEqual(meta.source, "gitiles")

    @patch("find_branch_repos.metadata_fetchers.urllib.request.urlopen")
    def test_empty_log_raises(self, mock_urlopen):
        mock_urlopen.return_value = _FakeResponse((")]}'\n" + json.dumps({"log": []})).encode())
        fetcher = GitilesLogFetcher()
        with self.assertRaises(MetadataFetchError):
            fetcher.fetch("https://android.googlesource.com/platform/x", "feature/xyz", "sha")


class GitHubApiCommitFetcherTest(unittest.TestCase):
    @patch("find_branch_repos.metadata_fetchers.urllib.request.urlopen")
    def test_fetch_parses_commit(self, mock_urlopen):
        body = {
            "sha": "deadbeef",
            "commit": {
                "author": {"name": "Bob", "date": "2026-01-01T00:00:00Z"},
                "message": "Add feature\n\nbody",
            },
        }
        mock_urlopen.return_value = _FakeResponse(json.dumps(body).encode())
        fetcher = GitHubApiCommitFetcher(token="tok")
        meta = fetcher.fetch("https://github.com/org/repo", "feature/xyz", "fallback")
        self.assertEqual(meta.sha, "deadbeef")
        self.assertEqual(meta.author, "Bob")
        self.assertEqual(meta.subject, "Add feature")
        request = mock_urlopen.call_args[0][0]
        self.assertEqual(request.headers.get("Authorization"), "Bearer tok")


class GitLabApiCommitFetcherTest(unittest.TestCase):
    @patch("find_branch_repos.metadata_fetchers.urllib.request.urlopen")
    def test_fetch_parses_commit(self, mock_urlopen):
        body = {"id": "cafef00d", "author_name": "Carol", "committed_date": "2026-01-01", "title": "Do the thing"}
        mock_urlopen.return_value = _FakeResponse(json.dumps(body).encode())
        fetcher = GitLabApiCommitFetcher()
        meta = fetcher.fetch("https://gitlab.example.com/group/project", "feature/xyz", "fallback")
        self.assertEqual(meta.sha, "cafef00d")
        self.assertEqual(meta.author, "Carol")
        self.assertEqual(meta.subject, "Do the thing")


class MinimalGitCommitFetcherTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = make_repo(Path(self._tmp.name))
        self._patch = patch.object(metadata_fetchers, "GIT_ALLOWED_PROTOCOLS", "http:https:ssh:git:file")
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_fetch_reads_real_commit_metadata(self):
        sha = rev_parse(self.repo, "feature/xyz")
        fetcher = MinimalGitCommitFetcher()
        meta = fetcher.fetch(str(self.repo), "feature/xyz", sha)
        self.assertEqual(meta.sha, sha)
        self.assertEqual(meta.author, "Test User")
        self.assertEqual(meta.subject, "feature commit")
        self.assertEqual(meta.source, "git-fetch-fallback")

    def test_unknown_sha_raises(self):
        fetcher = MinimalGitCommitFetcher()
        with self.assertRaises(MetadataFetchError):
            fetcher.fetch(str(self.repo), "feature/xyz", "0" * 40)


class MetadataFetcherChainTest(unittest.TestCase):
    def test_falls_back_on_failure(self):
        class Failing:
            name = "failing"

            def can_handle(self, url):
                return True

            def fetch(self, url, branch, sha):
                raise MetadataFetchError("boom")

        class Working:
            name = "working"

            def can_handle(self, url):
                return True

            def fetch(self, url, branch, sha):
                from find_branch_repos.models import CommitMetadata

                return CommitMetadata(sha=sha, author="x", date="y", subject="z", source="working")

        chain = MetadataFetcherChain([Failing(), Working()])
        meta = chain.fetch("https://example.com/x", "main", "sha")
        self.assertEqual(meta.source, "working")


if __name__ == "__main__":
    unittest.main()
