"""End-to-end test of the whole pipeline (manifest resolution -> ls-remote -> metadata ->
output) against real local git repos standing in for the manifest repo and two project repos.
No network is used; the local-path "file" transport is allowed only for the duration of
this test (production runs restrict GIT_ALLOW_PROTOCOL to http/https/ssh/git)."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from find_branch_repos import branch_checker, cli, file_fetchers, metadata_fetchers


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=path)
    _git("config", "user.email", "test@example.com", cwd=path)
    _git("config", "user.name", "Test User", cwd=path)


def _commit_file(path: Path, filename: str, content: str, message: str) -> None:
    (path / filename).write_text(content)
    _git("add", filename, cwd=path)
    _git("commit", "-q", "-m", message, cwd=path)


class CliEndToEndTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

        self.manifest_repo = self.root / "manifest"
        _init_repo(self.manifest_repo)
        manifest_xml = f"""<manifest>
          <remote name="origin" fetch="{self.root}"/>
          <default remote="origin" revision="main"/>
          <project name="repo-a" path="a"/>
          <project name="repo-b" path="b"/>
        </manifest>"""
        _commit_file(self.manifest_repo, "default.xml", manifest_xml, "add manifest")

        self.repo_a = self.root / "repo-a"
        _init_repo(self.repo_a)
        _commit_file(self.repo_a, "f.txt", "hi\n", "init")
        _git("checkout", "-q", "-b", "feature/xyz", cwd=self.repo_a)
        _commit_file(self.repo_a, "f.txt", "hi2\n", "feature commit")
        _git("checkout", "-q", "main", cwd=self.repo_a)

        self.repo_b = self.root / "repo-b"
        _init_repo(self.repo_b)
        _commit_file(self.repo_b, "f.txt", "hi\n", "init")
        # repo-b intentionally has no feature/xyz branch

        self._patches = [
            patch.object(file_fetchers, "GIT_ALLOWED_PROTOCOLS", "http:https:ssh:git:file"),
            patch.object(metadata_fetchers, "GIT_ALLOWED_PROTOCOLS", "http:https:ssh:git:file"),
            patch.object(branch_checker, "GIT_ALLOWED_PROTOCOLS", "http:https:ssh:git:file"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def test_finds_only_repo_with_branch_and_reports_metadata(self):
        json_out = self.root / "result.json"
        argv = [
            "--branch", "feature/xyz",
            "--manifest-url", str(self.manifest_repo),
            "--manifest-branch", "main",
            "--json-out", str(json_out),
        ]
        buf = StringIO()
        with redirect_stdout(buf):
            exit_code = cli.main(argv)

        self.assertEqual(exit_code, 0)
        self.assertIn("a", buf.getvalue())
        self.assertNotIn("https://example.com", buf.getvalue())  # sanity: no leaked placeholder

        payload = json.loads(json_out.read_text())
        self.assertEqual(payload["hits_count"], 1)
        self.assertEqual(payload["projects_total"], 2)
        hit = payload["hits"][0]
        self.assertEqual(hit["path"], "a")
        self.assertEqual(len(hit["sha"]), 40)
        self.assertEqual(hit["author"], "Test User")
        self.assertEqual(hit["subject"], "feature commit")
        self.assertEqual(hit["metadata_source"], "git-fetch-fallback")
        self.assertEqual(payload["errors"], [])

    def test_skip_metadata_flag_omits_commit_details(self):
        json_out = self.root / "result2.json"
        argv = [
            "--branch", "feature/xyz",
            "--manifest-url", str(self.manifest_repo),
            "--manifest-branch", "main",
            "--json-out", str(json_out),
            "--skip-metadata",
        ]
        buf = StringIO()
        with redirect_stdout(buf):
            exit_code = cli.main(argv)
        self.assertEqual(exit_code, 0)

        payload = json.loads(json_out.read_text())
        self.assertEqual(payload["hits"][0]["metadata_source"], "unavailable")
        self.assertIsNone(payload["hits"][0]["author"])

    def test_branch_absent_everywhere_yields_no_hits(self):
        argv = [
            "--branch", "no-such-branch",
            "--manifest-url", str(self.manifest_repo),
            "--manifest-branch", "main",
            "--skip-metadata",
        ]
        buf = StringIO()
        with redirect_stdout(buf):
            exit_code = cli.main(argv)
        self.assertEqual(exit_code, 0)
        self.assertIn("No matching repositories found", buf.getvalue())

    def test_bad_manifest_url_exits_1(self):
        argv = [
            "--branch", "feature/xyz",
            "--manifest-url", str(self.root / "does-not-exist"),
            "--manifest-branch", "main",
        ]
        buf = StringIO()
        with redirect_stdout(buf):
            exit_code = cli.main(argv)
        self.assertEqual(exit_code, 1)


if __name__ == "__main__":
    unittest.main()
