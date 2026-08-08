from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from find_branch_repos import branch_checker
from find_branch_repos.models import ProjectRef
from tests.gitutil import make_repo


class ValidateBranchNameTest(unittest.TestCase):
    def test_accepts_normal_names(self):
        for name in ("main", "feature/xyz-123", "release/2026.08", "a.b_c"):
            self.assertEqual(branch_checker.validate_branch_name(name), name)

    def test_rejects_leading_dash(self):
        with self.assertRaises(branch_checker.InvalidBranchNameError):
            branch_checker.validate_branch_name("--upload-pack=evil")

    def test_rejects_empty(self):
        with self.assertRaises(branch_checker.InvalidBranchNameError):
            branch_checker.validate_branch_name("")

    def test_rejects_path_traversal(self):
        with self.assertRaises(branch_checker.InvalidBranchNameError):
            branch_checker.validate_branch_name("../etc/passwd")

    def test_rejects_whitespace(self):
        with self.assertRaises(branch_checker.InvalidBranchNameError):
            branch_checker.validate_branch_name("feature xyz")


class CheckBranchTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = make_repo(Path(self._tmp.name))
        self._patch = patch.object(branch_checker, "GIT_ALLOWED_PROTOCOLS", "http:https:ssh:git:file")
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def _project(self):
        return ProjectRef(name="repo", path="repo", remote_alias="origin", fetch_url=str(self.repo), revision="main")

    def test_existing_branch_returns_full_sha(self):
        async def go():
            sem = asyncio.Semaphore(1)
            return await branch_checker.check_branch(self._project(), "feature/xyz", sem, timeout=10)

        sha = asyncio.run(go())
        self.assertIsNotNone(sha)
        self.assertEqual(len(sha), 40)

    def test_missing_branch_returns_none(self):
        async def go():
            sem = asyncio.Semaphore(1)
            return await branch_checker.check_branch(self._project(), "does-not-exist", sem, timeout=10)

        self.assertIsNone(asyncio.run(go()))

    def test_unreachable_repo_raises(self):
        bad = ProjectRef(
            name="x", path="x", remote_alias="o",
            fetch_url=str(Path(self._tmp.name) / "does-not-exist"), revision="main",
        )

        async def go():
            sem = asyncio.Semaphore(1)
            return await branch_checker.check_branch(bad, "main", sem, timeout=10)

        with self.assertRaises(branch_checker.BranchCheckError):
            asyncio.run(go())


class CheckBranchOnAllTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.good_repo = make_repo(Path(self._tmp.name))
        self._patch = patch.object(branch_checker, "GIT_ALLOWED_PROTOCOLS", "http:https:ssh:git:file")
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_mixes_hits_and_errors(self):
        good = ProjectRef(name="good", path="good", remote_alias="o", fetch_url=str(self.good_repo), revision="main")
        bad = ProjectRef(
            name="bad", path="bad", remote_alias="o",
            fetch_url=str(Path(self._tmp.name) / "nope"), revision="main",
        )
        hits, errors = asyncio.run(
            branch_checker.check_branch_on_all([good, bad], "feature/xyz", concurrency=4, timeout=10)
        )
        self.assertEqual(list(hits.keys()), [good])
        self.assertEqual(len(errors), 1)
        self.assertIs(errors[0].project, bad)

    def test_invalid_branch_name_raises_before_any_query(self):
        good = ProjectRef(name="good", path="good", remote_alias="o", fetch_url=str(self.good_repo), revision="main")
        with self.assertRaises(branch_checker.InvalidBranchNameError):
            asyncio.run(branch_checker.check_branch_on_all([good], "--evil", concurrency=4, timeout=10))


if __name__ == "__main__":
    unittest.main()
