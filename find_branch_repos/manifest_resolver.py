"""Recursively resolve a repo-tool manifest tree into a flat list of ProjectRef.

Handles <remote>/<default> inheritance, <include>, <submanifest>, <remove-project>,
and an optional local-manifest directory (read from disk, since local manifests are
not centrally fetchable). Manifest file content itself is read via a FileFetcherChain,
never via a persistent checkout.
"""

from __future__ import annotations

import logging
import posixpath
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from .file_fetchers import FileFetchError, FileFetcherChain
from .models import ProjectRef

logger = logging.getLogger(__name__)


class ManifestResolutionError(Exception):
    pass


@dataclass
class _Remote:
    name: str
    fetch: str
    revision: str | None = None


@dataclass
class _Default:
    remote: str | None = None
    revision: str | None = None


@dataclass
class _Scope:
    remotes: dict[str, _Remote] = field(default_factory=dict)
    default: _Default = field(default_factory=_Default)
    projects: dict[str, ProjectRef] = field(default_factory=dict)  # keyed by local path


def _require(elem: ET.Element, attr: str, context: str) -> str:
    value = elem.attrib.get(attr)
    if not value:
        raise ManifestResolutionError(f"<{elem.tag}> in {context} is missing required attribute '{attr}'")
    return value


def _join_fetch_url(remote_fetch: str, project_name: str) -> str:
    return f"{remote_fetch.rstrip('/')}/{project_name}"


def _resolve_revision(project_revision: str | None, default_revision: str | None, remote_revision: str | None) -> str:
    return project_revision or default_revision or remote_revision or "HEAD"


def _join_path(prefix: str, path: str) -> str:
    return posixpath.normpath(posixpath.join(prefix, path)) if prefix else path


def _fetch_xml(file_fetcher: FileFetcherChain, repo_url: str, ref: str, filename: str, context: str) -> ET.Element:
    try:
        content, _provider = file_fetcher.fetch(repo_url, ref, filename)
    except FileFetchError as exc:
        raise ManifestResolutionError(f"could not read {filename}@{ref} from {repo_url} ({context}): {exc}") from exc
    try:
        return ET.fromstring(content)
    except ET.ParseError as exc:
        raise ManifestResolutionError(f"invalid XML in {filename}@{ref} from {repo_url} ({context}): {exc}") from exc


def _process_manifest(
    root: ET.Element,
    *,
    repo_url: str,
    ref: str,
    filename: str,
    path_prefix: str,
    visited: frozenset[tuple[str, str, str]],
    scope: _Scope,
    file_fetcher: FileFetcherChain,
    source_label: str,
) -> None:
    key = (repo_url, ref, filename)
    if key in visited:
        chain = " -> ".join(f"{f}@{r}" for _, r, f in visited)
        raise ManifestResolutionError(f"cyclic <include> detected: {filename}@{ref} (chain: {chain} -> {filename}@{ref})")
    visited = visited | {key}
    context = f"{filename}@{ref} ({source_label})"

    for elem in root:
        if elem.tag == "remote":
            name = _require(elem, "name", context)
            scope.remotes[name] = _Remote(name=name, fetch=_require(elem, "fetch", context), revision=elem.attrib.get("revision"))

        elif elem.tag == "default":
            scope.default = _Default(remote=elem.attrib.get("remote"), revision=elem.attrib.get("revision"))

        elif elem.tag == "project":
            _add_project(elem, scope=scope, path_prefix=path_prefix, source_label=source_label, context=context)

        elif elem.tag == "remove-project":
            name = _require(elem, "name", context)
            for path, proj in list(scope.projects.items()):
                if proj.name == name:
                    del scope.projects[path]

        elif elem.tag == "include":
            include_name = _require(elem, "name", context)
            included_root = _fetch_xml(file_fetcher, repo_url, ref, include_name, source_label)
            _process_manifest(
                included_root,
                repo_url=repo_url,
                ref=ref,
                filename=include_name,
                path_prefix=path_prefix,
                visited=visited,
                scope=scope,
                file_fetcher=file_fetcher,
                source_label=source_label,
            )

        elif elem.tag == "submanifest":
            _process_submanifest(elem, scope=scope, file_fetcher=file_fetcher, visited=visited, path_prefix=path_prefix, context=context)

        # other tags (<copyfile>, <linkfile>, <extend-project>, <repo-hooks>, ...) don't affect
        # which repos/branches exist and are intentionally ignored


def _add_project(elem: ET.Element, *, scope: _Scope, path_prefix: str, source_label: str, context: str) -> None:
    name = _require(elem, "name", context)
    path = elem.attrib.get("path", name)
    remote_alias = elem.attrib.get("remote") or scope.default.remote
    if not remote_alias:
        raise ManifestResolutionError(f"project '{name}' in {context} has no remote and no <default remote=...> is set")
    remote = scope.remotes.get(remote_alias)
    if remote is None:
        raise ManifestResolutionError(f"project '{name}' in {context} references unknown remote '{remote_alias}'")

    revision = _resolve_revision(elem.attrib.get("revision"), scope.default.revision, remote.revision)
    groups = tuple(g for g in elem.attrib.get("groups", "").split(",") if g)
    full_path = _join_path(path_prefix, path)
    scope.projects[full_path] = ProjectRef(
        name=name,
        path=full_path,
        remote_alias=remote_alias,
        fetch_url=_join_fetch_url(remote.fetch, name),
        revision=revision,
        groups=groups,
        source_manifest=source_label,
    )


def _process_submanifest(
    elem: ET.Element,
    *,
    scope: _Scope,
    file_fetcher: FileFetcherChain,
    visited: frozenset[tuple[str, str, str]],
    path_prefix: str,
    context: str,
) -> None:
    name = _require(elem, "name", context)
    remote_alias = elem.attrib.get("remote") or scope.default.remote
    remote = scope.remotes.get(remote_alias)
    if remote is None:
        raise ManifestResolutionError(f"submanifest '{name}' in {context} references unknown remote '{remote_alias}'")

    project_name = elem.attrib.get("project", name)
    sub_repo_url = _join_fetch_url(remote.fetch, project_name)
    sub_ref = elem.attrib.get("revision") or remote.revision or "HEAD"
    manifest_name = elem.attrib.get("manifest-name", "default.xml")
    sub_path_prefix = _join_path(path_prefix, elem.attrib.get("path", name))
    sub_label = f"submanifest:{name}"

    sub_scope = _Scope()
    sub_root = _fetch_xml(file_fetcher, sub_repo_url, sub_ref, manifest_name, sub_label)
    _process_manifest(
        sub_root,
        repo_url=sub_repo_url,
        ref=sub_ref,
        filename=manifest_name,
        path_prefix=sub_path_prefix,
        visited=visited,
        scope=sub_scope,
        file_fetcher=file_fetcher,
        source_label=sub_label,
    )
    scope.projects.update(sub_scope.projects)


def resolve_manifest(
    manifest_repo_url: str,
    manifest_ref: str,
    file_fetcher: FileFetcherChain,
    root_file: str = "default.xml",
    local_manifest_dir: str | Path | None = None,
) -> list[ProjectRef]:
    scope = _Scope()
    root = _fetch_xml(file_fetcher, manifest_repo_url, manifest_ref, root_file, "root")
    _process_manifest(
        root,
        repo_url=manifest_repo_url,
        ref=manifest_ref,
        filename=root_file,
        path_prefix="",
        visited=frozenset(),
        scope=scope,
        file_fetcher=file_fetcher,
        source_label="root",
    )

    if local_manifest_dir:
        _apply_local_manifests(Path(local_manifest_dir), scope)

    return sorted(scope.projects.values(), key=lambda p: p.path)


def _apply_local_manifests(directory: Path, scope: _Scope) -> None:
    if not directory.is_dir():
        raise ManifestResolutionError(f"local manifest directory not found: {directory}")
    for xml_file in sorted(directory.glob("*.xml")):
        try:
            root = ET.fromstring(xml_file.read_bytes())
        except ET.ParseError as exc:
            raise ManifestResolutionError(f"invalid XML in local manifest {xml_file}: {exc}") from exc
        context = f"local manifest {xml_file.name}"
        # local manifests are read straight from disk; include/submanifest are not supported here
        for elem in root:
            if elem.tag == "remote":
                name = _require(elem, "name", context)
                scope.remotes[name] = _Remote(name=name, fetch=_require(elem, "fetch", context), revision=elem.attrib.get("revision"))
            elif elem.tag == "project":
                _add_project(elem, scope=scope, path_prefix="", source_label=context, context=context)
            elif elem.tag == "remove-project":
                name = _require(elem, "name", context)
                for path, proj in list(scope.projects.items()):
                    if proj.name == name:
                        del scope.projects[path]
