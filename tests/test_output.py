from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from find_branch_repos.models import BranchResult, CommitMetadata, ProjectRef, RepoError, RunSummary
from find_branch_repos.output import build_json_payload, render_table, write_json


def _summary() -> RunSummary:
    project = ProjectRef(name="proj-a", path="a", remote_alias="origin", fetch_url="https://example.com/a", revision="main")
    hit = BranchResult(
        project=project,
        sha="a" * 40,
        metadata=CommitMetadata(sha="a" * 40, author="Ada", date="2026-01-01", subject="Fix things", source="gitiles"),
    )
    err_project = ProjectRef(name="proj-b", path="b", remote_alias="origin", fetch_url="https://example.com/b", revision="main")
    return RunSummary(
        branch="feature/xyz",
        manifest_url="https://example.com/manifest",
        manifest_branch="master",
        projects_total=2,
        hits=[hit],
        errors=[RepoError(project=err_project, stage="ls-remote", message="timeout")],
    )


class RenderTableTest(unittest.TestCase):
    def test_renders_hit_row(self):
        table = render_table(_summary())
        self.assertIn("a", table)
        self.assertIn("Ada", table)
        self.assertIn("Fix things", table)
        self.assertIn("https://example.com/a", table)

    def test_empty_summary_message(self):
        empty = RunSummary(branch="x", manifest_url="u", manifest_branch="m", projects_total=0)
        self.assertEqual(render_table(empty), "No matching repositories found.")


class JsonPayloadTest(unittest.TestCase):
    def test_payload_contains_hits_and_errors(self):
        payload = build_json_payload(_summary())
        self.assertEqual(payload["branch"], "feature/xyz")
        self.assertEqual(payload["hits_count"], 1)
        self.assertEqual(payload["errors_count"], 1)
        self.assertEqual(payload["hits"][0]["path"], "a")
        self.assertEqual(payload["hits"][0]["author"], "Ada")
        self.assertEqual(payload["errors"][0]["path"], "b")
        self.assertEqual(payload["errors"][0]["stage"], "ls-remote")

    def test_write_json_roundtrips(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "result.json"
            write_json(_summary(), out)
            data = json.loads(out.read_text())
            self.assertEqual(data["hits_count"], 1)


if __name__ == "__main__":
    unittest.main()
