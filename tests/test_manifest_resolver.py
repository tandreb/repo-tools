from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from find_branch_repos.file_fetchers import FileFetchError
from find_branch_repos.manifest_resolver import ManifestResolutionError, resolve_manifest


class _FakeFileFetcher:
    """Stands in for a FileFetcherChain: serves fixed XML content from an in-memory dict."""

    def __init__(self, files: dict[tuple[str, str, str], bytes]):
        self.files = files
        self.calls: list[tuple[str, str, str]] = []

    def fetch(self, repo_url: str, ref: str, path: str) -> tuple[bytes, str]:
        key = (repo_url, ref, path)
        self.calls.append(key)
        if key not in self.files:
            raise FileFetchError(f"no such file in fake fetcher: {key}")
        return self.files[key], "fake"


ROOT_URL = "https://example.com/base/manifest"


def _resolve(files: dict[tuple[str, str, str], bytes], **kwargs):
    fetcher = _FakeFileFetcher(files)
    return resolve_manifest(ROOT_URL, "main", fetcher, **kwargs)


class BasicResolutionTest(unittest.TestCase):
    def test_project_remote_default_resolution(self):
        root = b"""
        <manifest>
          <remote name="origin" fetch="https://example.com/base"/>
          <default remote="origin" revision="main"/>
          <project name="proj-a" path="a"/>
          <project name="proj-b" path="b" revision="release"/>
        </manifest>
        """
        projects = _resolve({(ROOT_URL, "main", "default.xml"): root})
        self.assertEqual([p.path for p in projects], ["a", "b"])

        a = next(p for p in projects if p.path == "a")
        self.assertEqual(a.name, "proj-a")
        self.assertEqual(a.fetch_url, "https://example.com/base/proj-a")
        self.assertEqual(a.revision, "main")  # inherited from <default>

        b = next(p for p in projects if p.path == "b")
        self.assertEqual(b.revision, "release")  # project-level override wins

    def test_revision_falls_back_to_remote_then_head(self):
        root = b"""
        <manifest>
          <remote name="origin" fetch="https://example.com/base" revision="dev"/>
          <default remote="origin"/>
          <project name="proj-a" path="a"/>
        </manifest>
        """
        projects = _resolve({(ROOT_URL, "main", "default.xml"): root})
        self.assertEqual(projects[0].revision, "dev")

    def test_missing_remote_raises(self):
        root = b"""
        <manifest>
          <project name="proj-a" path="a" remote="ghost"/>
        </manifest>
        """
        with self.assertRaises(ManifestResolutionError):
            _resolve({(ROOT_URL, "main", "default.xml"): root})

    def test_groups_parsed(self):
        root = b"""
        <manifest>
          <remote name="origin" fetch="https://example.com/base"/>
          <default remote="origin" revision="main"/>
          <project name="proj-a" path="a" groups="core,build"/>
        </manifest>
        """
        projects = _resolve({(ROOT_URL, "main", "default.xml"): root})
        self.assertEqual(projects[0].groups, ("core", "build"))


class IncludeTest(unittest.TestCase):
    def test_include_merges_projects_and_remove_project(self):
        root = b"""
        <manifest>
          <remote name="origin" fetch="https://example.com/base"/>
          <default remote="origin" revision="main"/>
          <project name="proj-a" path="a"/>
          <project name="proj-b" path="b"/>
          <include name="extra.xml"/>
        </manifest>
        """
        extra = b"""
        <manifest>
          <project name="proj-c" path="c"/>
          <remove-project name="proj-b"/>
        </manifest>
        """
        projects = _resolve({
            (ROOT_URL, "main", "default.xml"): root,
            (ROOT_URL, "main", "extra.xml"): extra,
        })
        self.assertEqual(sorted(p.path for p in projects), ["a", "c"])

    def test_cyclic_include_is_detected(self):
        root = b"""<manifest><include name="default.xml"/></manifest>"""
        with self.assertRaises(ManifestResolutionError):
            _resolve({(ROOT_URL, "main", "default.xml"): root})

    def test_missing_include_raises(self):
        root = b"""<manifest><include name="missing.xml"/></manifest>"""
        with self.assertRaises(ManifestResolutionError):
            _resolve({(ROOT_URL, "main", "default.xml"): root})


class SubmanifestTest(unittest.TestCase):
    def test_submanifest_projects_get_prefixed_path_and_own_remote(self):
        root = b"""
        <manifest>
          <remote name="origin" fetch="https://example.com/base"/>
          <default remote="origin" revision="main"/>
          <project name="proj-a" path="a"/>
          <submanifest name="vendor" project="vendor-manifest" path="vendor" revision="main"/>
        </manifest>
        """
        sub_repo_url = "https://example.com/base/vendor-manifest"
        sub = b"""
        <manifest>
          <remote name="vorigin" fetch="https://vendor.example.com/base"/>
          <default remote="vorigin" revision="main"/>
          <project name="lib-x" path="libx"/>
        </manifest>
        """
        projects = _resolve({
            (ROOT_URL, "main", "default.xml"): root,
            (sub_repo_url, "main", "default.xml"): sub,
        })
        paths = sorted(p.path for p in projects)
        self.assertEqual(paths, ["a", "vendor/libx"])

        libx = next(p for p in projects if p.path == "vendor/libx")
        self.assertEqual(libx.fetch_url, "https://vendor.example.com/base/lib-x")


class LocalManifestTest(unittest.TestCase):
    def test_local_manifest_dir_is_merged_in(self):
        root = b"""
        <manifest>
          <remote name="origin" fetch="https://example.com/base"/>
          <default remote="origin" revision="main"/>
          <project name="proj-a" path="a"/>
        </manifest>
        """
        with tempfile.TemporaryDirectory() as tmp:
            local_dir = Path(tmp)
            (local_dir / "local.xml").write_text(
                '<manifest><project name="proj-local" path="local" remote="origin"/></manifest>'
            )
            projects = _resolve(
                {(ROOT_URL, "main", "default.xml"): root},
                local_manifest_dir=local_dir,
            )
        self.assertEqual(sorted(p.path for p in projects), ["a", "local"])

    def test_missing_local_manifest_dir_raises(self):
        root = b"""<manifest><remote name="o" fetch="https://x/base"/></manifest>"""
        with self.assertRaises(ManifestResolutionError):
            _resolve(
                {(ROOT_URL, "main", "default.xml"): root},
                local_manifest_dir="/no/such/directory",
            )


if __name__ == "__main__":
    unittest.main()
