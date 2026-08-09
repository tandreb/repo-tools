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

    def test_revision_falls_back_to_submanifest_name(self):
        """repo uses `revision or name` here -- not the remote's or the <default>'s revision."""
        root = b"""
        <manifest>
          <remote name="origin" fetch="https://example.com/base"/>
          <default remote="origin" revision="release-42"/>
          <submanifest name="vendor-branch" project="vendor-manifest" path="vendor"/>
        </manifest>
        """
        sub = b"""
        <manifest>
          <remote name="origin" fetch="https://example.com/base"/>
          <default remote="origin" revision="release-42"/>
          <project name="lib-x" path="libx"/>
        </manifest>
        """
        fetcher = _FakeFileFetcher({
            (ROOT_URL, "main", "default.xml"): root,
            ("https://example.com/base/vendor-manifest", "vendor-branch", "default.xml"): sub,
        })
        projects = resolve_manifest(ROOT_URL, "main", fetcher, strict=True)
        self.assertEqual(sorted(p.path for p in projects), ["vendor/libx"])

    def test_without_project_the_submanifest_lives_in_the_parent_manifest_repo(self):
        """The bug that made every submanifest 404: repo uses the parent manifest's own URL when
        no `project` is given, rather than deriving one from the remote and the submanifest name."""
        root = b"""
        <manifest>
          <remote name="origin" fetch="https://example.com/base"/>
          <default remote="origin" revision="main"/>
          <submanifest name="feature-x" path="featx"/>
        </manifest>
        """
        sub = b"""
        <manifest>
          <remote name="origin" fetch="https://example.com/base"/>
          <default remote="origin" revision="main"/>
          <project name="lib-x" path="libx"/>
        </manifest>
        """
        fetcher = _FakeFileFetcher({
            (ROOT_URL, "main", "default.xml"): root,
            (ROOT_URL, "feature-x", "default.xml"): sub,  # same repo, submanifest's own revision
        })
        projects = resolve_manifest(ROOT_URL, "main", fetcher, strict=True)
        self.assertEqual([p.path for p in projects], ["featx/libx"])

    def test_remote_without_project_is_rejected(self):
        root = b"""
        <manifest>
          <remote name="origin" fetch="https://example.com/base"/>
          <default remote="origin" revision="main"/>
          <submanifest name="vendor" remote="origin"/>
        </manifest>
        """
        with self.assertRaises(ManifestResolutionError):
            _resolve({(ROOT_URL, "main", "default.xml"): root})

    def test_path_defaults_to_last_component_of_revision(self):
        root = b"""
        <manifest>
          <remote name="origin" fetch="https://example.com/base"/>
          <default remote="origin" revision="main"/>
          <submanifest name="vendor" revision="releases/r42"/>
        </manifest>
        """
        sub = b"""
        <manifest>
          <remote name="origin" fetch="https://example.com/base"/>
          <default remote="origin" revision="main"/>
          <project name="lib-x" path="libx"/>
        </manifest>
        """
        fetcher = _FakeFileFetcher({
            (ROOT_URL, "main", "default.xml"): root,
            (ROOT_URL, "releases/r42", "default.xml"): sub,
        })
        projects = resolve_manifest(ROOT_URL, "main", fetcher, strict=True)
        self.assertEqual([p.path for p in projects], ["r42/libx"])


class UnreachableSubmanifestTest(unittest.TestCase):
    """An unreachable submanifest repo (private, retired, wrong URL) must not discard the rest."""

    ROOT = b"""
    <manifest>
      <remote name="origin" fetch="https://example.com/base"/>
      <default remote="origin" revision="main"/>
      <project name="proj-a" path="a"/>
      <project name="proj-b" path="b"/>
      <submanifest name="vendor" project="missing-manifest" path="vendor" revision="main"/>
    </manifest>
    """

    def test_skips_submanifest_and_keeps_other_projects(self):
        warnings: list[str] = []
        projects = _resolve({(ROOT_URL, "main", "default.xml"): self.ROOT}, warnings=warnings)

        self.assertEqual(sorted(p.path for p in projects), ["a", "b"])
        self.assertEqual(len(warnings), 1)
        self.assertIn("vendor", warnings[0])
        self.assertIn("missing-manifest", warnings[0])

    def test_strict_mode_still_raises(self):
        with self.assertRaises(ManifestResolutionError):
            _resolve({(ROOT_URL, "main", "default.xml"): self.ROOT}, strict=True)

    def test_include_failure_stays_fatal(self):
        """Unlike a submanifest, an include lives in the manifest repo we could already read."""
        root = b"""
        <manifest>
          <remote name="origin" fetch="https://example.com/base"/>
          <default remote="origin" revision="main"/>
          <include name="missing.xml"/>
        </manifest>
        """
        with self.assertRaises(ManifestResolutionError):
            _resolve({(ROOT_URL, "main", "default.xml"): root})


class RemoveProjectTest(unittest.TestCase):
    """repo accepts "name and/or path" here; requiring name rejected valid manifests."""

    BASE = """
      <remote name="origin" fetch="https://example.com/base"/>
      <default remote="origin" revision="main"/>
      <project name="proj-a" path="a"/>
      <project name="proj-b" path="b"/>
    """

    def _paths(self, removal: str) -> list[str]:
        root = f"<manifest>{self.BASE}{removal}</manifest>".encode()
        return sorted(p.path for p in _resolve({(ROOT_URL, "main", "default.xml"): root}))

    def test_remove_by_path_alone(self):
        self.assertEqual(self._paths('<remove-project path="a"/>'), ["b"])

    def test_remove_by_name_alone(self):
        self.assertEqual(self._paths('<remove-project name="proj-b"/>'), ["a"])

    def test_remove_by_name_and_path(self):
        self.assertEqual(self._paths('<remove-project name="proj-a" path="a"/>'), ["b"])

    def test_mismatched_name_and_path_removes_nothing(self):
        self.assertEqual(self._paths('<remove-project name="proj-a" path="b"/>'), ["a", "b"])

    def test_removing_a_missing_project_is_tolerated(self):
        self.assertEqual(self._paths('<remove-project name="not-here"/>'), ["a", "b"])

    def test_without_name_or_path_raises(self):
        with self.assertRaises(ManifestResolutionError):
            self._paths("<remove-project/>")

    def test_path_inside_submanifest_is_relative_to_that_submanifest(self):
        root = b"""
        <manifest>
          <remote name="origin" fetch="https://example.com/base"/>
          <default remote="origin" revision="main"/>
          <submanifest name="vendor" path="vendor"/>
        </manifest>
        """
        sub = b"""
        <manifest>
          <remote name="origin" fetch="https://example.com/base"/>
          <default remote="origin" revision="main"/>
          <project name="lib-x" path="libx"/>
          <project name="lib-y" path="liby"/>
          <remove-project path="libx"/>
        </manifest>
        """
        fetcher = _FakeFileFetcher({
            (ROOT_URL, "main", "default.xml"): root,
            (ROOT_URL, "vendor", "default.xml"): sub,
        })
        projects = resolve_manifest(ROOT_URL, "main", fetcher, strict=True)
        self.assertEqual([p.path for p in projects], ["vendor/liby"])


class RelativeFetchUrlTest(unittest.TestCase):
    """`fetch=".."` is the AOSP convention and only means anything relative to the manifest URL."""

    def test_relative_fetch_is_resolved_against_manifest_url(self):
        root = b"""
        <manifest>
          <remote name="origin" fetch=".."/>
          <default remote="origin" revision="main"/>
          <project name="proj-a" path="a"/>
        </manifest>
        """
        projects = _resolve({(ROOT_URL, "main", "default.xml"): root})
        # Same shape as AOSP: manifest at <host>/platform/manifest with fetch=".." puts projects
        # at <host>/<project name>. Here ROOT_URL is https://example.com/base/manifest.
        self.assertEqual(projects[0].fetch_url, "https://example.com/proj-a")

    def test_absolute_fetch_is_left_alone(self):
        root = b"""
        <manifest>
          <remote name="origin" fetch="https://other.example.com/x"/>
          <default remote="origin" revision="main"/>
          <project name="proj-a" path="a"/>
        </manifest>
        """
        projects = _resolve({(ROOT_URL, "main", "default.xml"): root})
        self.assertEqual(projects[0].fetch_url, "https://other.example.com/x/proj-a")


class LocallyCheckedOutSubmanifestTest(unittest.TestCase):
    """A submanifest already synced into the workspace needs no network access at all."""

    ROOT = b"""
    <manifest>
      <remote name="origin" fetch="https://example.com/base"/>
      <default remote="origin" revision="main"/>
      <submanifest name="vendor" path="vendor"/>
    </manifest>
    """
    SUB = b"""
    <manifest>
      <remote name="vorigin" fetch="https://vendor.example.com/base"/>
      <default remote="vorigin" revision="main"/>
      <project name="lib-x" path="libx"/>
    </manifest>
    """

    def test_local_checkout_is_preferred_over_fetching(self):
        fetcher = _FakeFileFetcher({(ROOT_URL, "main", "default.xml"): self.ROOT})  # sub not fetchable
        projects = resolve_manifest(
            ROOT_URL, "main", fetcher, strict=True,
            submanifest_lookup=lambda path, name: self.SUB if (path, name) == ("vendor", "default.xml") else None,
        )
        self.assertEqual([p.path for p in projects], ["vendor/libx"])
        self.assertNotIn(("https://example.com/base", "vendor", "default.xml"), fetcher.calls)

    def test_includes_inside_a_local_submanifest_are_also_read_locally(self):
        root_sub = b'<manifest><include name="more.xml"/></manifest>'
        files = {("vendor", "default.xml"): root_sub, ("vendor", "more.xml"): self.SUB}
        fetcher = _FakeFileFetcher({(ROOT_URL, "main", "default.xml"): self.ROOT})
        projects = resolve_manifest(
            ROOT_URL, "main", fetcher, strict=True,
            submanifest_lookup=lambda path, name: files.get((path, name)),
        )
        self.assertEqual([p.path for p in projects], ["vendor/libx"])

    def test_falls_back_to_fetching_when_not_checked_out(self):
        fetcher = _FakeFileFetcher({
            (ROOT_URL, "main", "default.xml"): self.ROOT,
            (ROOT_URL, "vendor", "default.xml"): self.SUB,
        })
        projects = resolve_manifest(
            ROOT_URL, "main", fetcher, strict=True, submanifest_lookup=lambda path, name: None
        )
        self.assertEqual([p.path for p in projects], ["vendor/libx"])


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

    def test_include_inside_a_local_manifest_is_resolved(self):
        """A local manifest pulling its projects in via an <include> must not silently lose them."""
        root = b"""
        <manifest>
          <remote name="origin" fetch="https://example.com/base"/>
          <default remote="origin" revision="main"/>
          <project name="proj-a" path="a"/>
        </manifest>
        """
        with tempfile.TemporaryDirectory() as tmp:
            local_dir = Path(tmp)
            (local_dir / "local.xml").write_text('<manifest><include name="vendor.inc"/></manifest>')
            (local_dir / "vendor.inc").write_text('<manifest><project name="proj-vendor" path="vendor"/></manifest>')
            projects = _resolve(
                {(ROOT_URL, "main", "default.xml"): root},
                local_manifest_dir=local_dir,
            )
        # proj-vendor inherits <default remote="origin"> from the main manifest, as it does in repo
        self.assertEqual(sorted(p.path for p in projects), ["a", "vendor"])

    def test_local_manifest_include_is_also_looked_up_next_to_the_manifest_checkout(self):
        root = b"""
        <manifest>
          <remote name="origin" fetch="https://example.com/base"/>
          <default remote="origin" revision="main"/>
        </manifest>
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            local_dir = repo_dir / "local_manifests"
            local_dir.mkdir()
            (repo_dir / "manifests").mkdir()
            (local_dir / "local.xml").write_text('<manifest><include name="extra.inc"/></manifest>')
            (repo_dir / "manifests" / "extra.inc").write_text('<manifest><project name="proj-x" path="x"/></manifest>')
            projects = _resolve({(ROOT_URL, "main", "default.xml"): root}, local_manifest_dir=local_dir)
        self.assertEqual([p.path for p in projects], ["x"])

    def test_missing_local_manifest_include_raises(self):
        root = b"""<manifest><remote name="o" fetch="https://x/base"/></manifest>"""
        with tempfile.TemporaryDirectory() as tmp:
            local_dir = Path(tmp)
            (local_dir / "local.xml").write_text('<manifest><include name="gone.inc"/></manifest>')
            with self.assertRaises(ManifestResolutionError):
                _resolve({(ROOT_URL, "main", "default.xml"): root}, local_manifest_dir=local_dir)

    def test_relative_fetch_in_local_manifest_is_resolved_against_manifest_url(self):
        root = b"""
        <manifest>
          <remote name="origin" fetch="https://example.com/base"/>
          <default remote="origin" revision="main"/>
        </manifest>
        """
        with tempfile.TemporaryDirectory() as tmp:
            local_dir = Path(tmp)
            (local_dir / "local.xml").write_text(
                '<manifest><remote name="extra" fetch=".."/>'
                '<project name="proj-x" path="x" remote="extra"/></manifest>'
            )
            projects = _resolve({(ROOT_URL, "main", "default.xml"): root}, local_manifest_dir=local_dir)
        self.assertEqual(projects[0].fetch_url, "https://example.com/proj-x")

    def test_local_manifest_can_remove_a_project_from_the_main_manifest(self):
        root = b"""
        <manifest>
          <remote name="origin" fetch="https://example.com/base"/>
          <default remote="origin" revision="main"/>
          <project name="proj-a" path="a"/>
          <project name="proj-b" path="b"/>
        </manifest>
        """
        with tempfile.TemporaryDirectory() as tmp:
            local_dir = Path(tmp)
            (local_dir / "local.xml").write_text('<manifest><remove-project name="proj-b"/></manifest>')
            projects = _resolve({(ROOT_URL, "main", "default.xml"): root}, local_manifest_dir=local_dir)
        self.assertEqual([p.path for p in projects], ["a"])


class ElementOrderTest(unittest.TestCase):
    """repo flattens all <include>s first and only then processes nodes by element type.

    Manifests split into `.inc` fragments lean on that hard: a fragment routinely uses a <remote>
    or <default> that is declared in another file, or below the <include> that pulled it in.
    """

    def test_include_before_remote_and_default(self):
        root = b"""
        <manifest>
          <include name="projects.inc"/>
          <remote name="origin" fetch="https://example.com/base"/>
          <default remote="origin" revision="main"/>
        </manifest>
        """
        projects = _resolve({
            (ROOT_URL, "main", "default.xml"): root,
            (ROOT_URL, "main", "projects.inc"): b'<manifest><project name="proj-a" path="a"/></manifest>',
        })
        self.assertEqual([p.path for p in projects], ["a"])
        self.assertEqual(projects[0].revision, "main")

    def test_project_can_use_a_remote_declared_in_a_later_include(self):
        root = b"""
        <manifest>
          <default remote="vendor" revision="main"/>
          <project name="proj-a" path="a"/>
          <include name="remotes.inc"/>
        </manifest>
        """
        projects = _resolve({
            (ROOT_URL, "main", "default.xml"): root,
            (ROOT_URL, "main", "remotes.inc"): b'<manifest><remote name="vendor" fetch="https://example.com/base"/></manifest>',
        })
        self.assertEqual(projects[0].fetch_url, "https://example.com/base/proj-a")

    def test_remove_project_applies_to_later_includes(self):
        root = b"""
        <manifest>
          <remote name="origin" fetch="https://example.com/base"/>
          <default remote="origin" revision="main"/>
          <remove-project name="proj-a"/>
          <include name="projects.inc"/>
        </manifest>
        """
        projects = _resolve({
            (ROOT_URL, "main", "default.xml"): root,
            (ROOT_URL, "main", "projects.inc"): (
                b'<manifest><project name="proj-a" path="a"/><project name="proj-b" path="b"/></manifest>'
            ),
        })
        self.assertEqual([p.path for p in projects], ["b"])

    def test_partial_default_in_a_fragment_does_not_drop_the_remote(self):
        root = b"""
        <manifest>
          <remote name="origin" fetch="https://example.com/base"/>
          <default remote="origin" revision="main"/>
          <include name="pin.inc"/>
          <project name="proj-a" path="a"/>
        </manifest>
        """
        projects = _resolve({
            (ROOT_URL, "main", "default.xml"): root,
            (ROOT_URL, "main", "pin.inc"): b'<manifest><default revision="release"/></manifest>',
        })
        self.assertEqual(projects[0].remote_alias, "origin")
        self.assertEqual(projects[0].revision, "release")

    def test_same_fragment_included_from_two_files_is_not_a_cycle(self):
        root = b"""
        <manifest>
          <remote name="origin" fetch="https://example.com/base"/>
          <default remote="origin" revision="main"/>
          <include name="a.inc"/>
          <include name="b.inc"/>
        </manifest>
        """
        projects = _resolve({
            (ROOT_URL, "main", "default.xml"): root,
            (ROOT_URL, "main", "a.inc"): b'<manifest><include name="common.inc"/></manifest>',
            (ROOT_URL, "main", "b.inc"): b'<manifest><include name="common.inc"/></manifest>',
            (ROOT_URL, "main", "common.inc"): b'<manifest><project name="proj-c" path="c"/></manifest>',
        })
        self.assertEqual([p.path for p in projects], ["c"])


if __name__ == "__main__":
    unittest.main()
