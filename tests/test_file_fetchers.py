from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from find_branch_repos import file_fetchers
from find_branch_repos.file_fetchers import (
    FileFetchError,
    FileFetcherChain,
    GitHubApiFileFetcher,
    GitilesFileFetcher,
    GitLabApiFileFetcher,
    MinimalGitFileFetcher,
)
from tests.gitutil import make_repo


class _FakeResponse:
    def __init__(self, data: bytes):
        self._data = data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._data


class GitilesFileFetcherTest(unittest.TestCase):
    def test_can_handle_googlesource(self):
        fetcher = GitilesFileFetcher()
        self.assertTrue(fetcher.can_handle("https://android.googlesource.com/platform/manifest"))
        self.assertFalse(fetcher.can_handle("https://github.com/foo/bar"))

    @patch("find_branch_repos.file_fetchers.urllib.request.urlopen")
    def test_fetch_decodes_base64(self, mock_urlopen):
        mock_urlopen.return_value = _FakeResponse(base64.b64encode(b"<manifest/>"))
        fetcher = GitilesFileFetcher()
        content = fetcher.fetch("https://android.googlesource.com/platform/manifest", "main", "default.xml")
        self.assertEqual(content, b"<manifest/>")
        requested_url = mock_urlopen.call_args[0][0].full_url
        self.assertIn("/+/main/default.xml?format=TEXT", requested_url)


class GitHubApiFileFetcherTest(unittest.TestCase):
    def test_can_handle_github(self):
        fetcher = GitHubApiFileFetcher()
        self.assertTrue(fetcher.can_handle("https://github.com/org/repo"))
        self.assertFalse(fetcher.can_handle("https://gitlab.com/org/repo"))

    @patch("find_branch_repos.file_fetchers.urllib.request.urlopen")
    def test_fetch_decodes_json_base64(self, mock_urlopen):
        payload = json.dumps({"encoding": "base64", "content": base64.b64encode(b"hi").decode()}).encode()
        mock_urlopen.return_value = _FakeResponse(payload)
        fetcher = GitHubApiFileFetcher(token="secret")
        content = fetcher.fetch("https://github.com/org/repo.git", "main", "path/to/file.xml")
        self.assertEqual(content, b"hi")
        request = mock_urlopen.call_args[0][0]
        self.assertIn("api.github.com/repos/org/repo/contents/path/to/file.xml", request.full_url)
        self.assertEqual(request.headers.get("Authorization"), "Bearer secret")

    def test_bad_url_raises(self):
        fetcher = GitHubApiFileFetcher()
        with self.assertRaises(FileFetchError):
            fetcher.fetch("https://github.com/onlyorg", "main", "x")


class GitLabApiFileFetcherTest(unittest.TestCase):
    @patch("find_branch_repos.file_fetchers.urllib.request.urlopen")
    def test_fetch_returns_raw_bytes(self, mock_urlopen):
        mock_urlopen.return_value = _FakeResponse(b"<manifest/>")
        fetcher = GitLabApiFileFetcher()
        content = fetcher.fetch("https://gitlab.example.com/group/project", "main", "default.xml")
        self.assertEqual(content, b"<manifest/>")
        request = mock_urlopen.call_args[0][0]
        self.assertIn("/api/v4/projects/group%2Fproject/repository/files/default.xml/raw?ref=main", request.full_url)


class MinimalGitFileFetcherTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = make_repo(Path(self._tmp.name))
        self._patch = patch.object(file_fetchers, "GIT_ALLOWED_PROTOCOLS", "http:https:ssh:git:file")
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_fetches_file_content_at_branch(self):
        fetcher = MinimalGitFileFetcher()
        content = fetcher.fetch(str(self.repo), "feature/xyz", "f.txt")
        self.assertEqual(content, b"hello again\n")

    def test_unknown_branch_raises(self):
        fetcher = MinimalGitFileFetcher()
        with self.assertRaises(FileFetchError):
            fetcher.fetch(str(self.repo), "no-such-branch", "f.txt")

    def test_unknown_path_raises(self):
        fetcher = MinimalGitFileFetcher()
        with self.assertRaises(FileFetchError):
            fetcher.fetch(str(self.repo), "main", "no-such-file.txt")


class FileFetcherChainTest(unittest.TestCase):
    def test_falls_back_to_next_provider_on_failure(self):
        class Failing:
            name = "failing"

            def can_handle(self, url):
                return True

            def fetch(self, url, ref, path):
                raise FileFetchError("boom")

        class Working:
            name = "working"

            def can_handle(self, url):
                return True

            def fetch(self, url, ref, path):
                return b"ok"

        chain = FileFetcherChain([Failing(), Working()])
        content, provider = chain.fetch("https://example.com/x", "main", "f")
        self.assertEqual(content, b"ok")
        self.assertEqual(provider, "working")

    def test_raises_when_all_providers_fail(self):
        class Failing:
            name = "failing"

            def can_handle(self, url):
                return True

            def fetch(self, url, ref, path):
                raise FileFetchError("boom")

        chain = FileFetcherChain([Failing()])
        with self.assertRaises(FileFetchError):
            chain.fetch("https://example.com/x", "main", "f")


if __name__ == "__main__":
    unittest.main()
