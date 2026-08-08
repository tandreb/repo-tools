"""Recursively resolve a repo-tool manifest tree into a flat list of ProjectRef.

Handles <remote>/<default> inheritance, <include>, <submanifest>, <remove-project>,
and an optional local-manifest directory (read from disk, since local manifests are
not centrally fetchable). Manifest file content itself is read via a FileFetcherChain,
never via a persistent checkout.
"""

from __future__ import annotations

import logging
import posixpath
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field, replace
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


SubmanifestLookup = Callable[[str, str], bytes | None]


@dataclass
class _Context:
    """Resolution-wide state, shared across the nested scopes a submanifest creates."""

    file_fetcher: FileFetcherChain
    strict: bool = False
    warnings: list[str] = field(default_factory=list)
    # Optional (submanifest_path, filename) -> content lookup for submanifests the local
    # workspace has already checked out, avoiding a network round trip for them entirely.
    submanifest_lookup: SubmanifestLookup | None = None


@dataclass
class _LocalSubmanifestFetcher:
    """FileFetcherChain stand-in that serves a checked-out submanifest's files from disk."""

    lookup: SubmanifestLookup
    submanifest_path: str

    def fetch(self, repo_url: str, ref: str, path: str) -> tuple[bytes, str]:
        content = self.lookup(self.submanifest_path, path)
        if content is None:
            raise FileFetchError(f"{path} not found in locally checked out submanifest {self.submanifest_path}")
        return content, "local-submanifest"


def _require(elem: ET.Element, attr: str, context: str) -> str:
    value = elem.attrib.get(attr)
    if not value:
        raise ManifestResolutionError(f"<{elem.tag}> in {context} is missing required attribute '{attr}'")
    return value


def _join_fetch_url(remote_fetch: str, project_name: str) -> str:
    return f"{remote_fetch.rstrip('/')}/{project_name}"


def _resolve_revision(project_revision: str | None, default_revision: str | None, remote_revision: str | None) -> str:
    return project_revision or default_revision or remote_revision or "HEAD"


def _resolve_fetch_url(fetch_url: str, manifest_url: str) -> str:
    """Resolve a <remote fetch=...> against the manifest repo URL, mirroring repo's own logic.

    Manifests routinely use relative fetch URLs (`fetch=".."` is the AOSP convention), which only
    mean anything relative to the URL the manifest itself was fetched from.
    """
    if not fetch_url:
        return ""
    # urljoin needs a scheme on the base, which local-path manifest URLs don't have; repo works
    # around this with a placeholder scheme, and this mirrors that so both behave identically.
    if manifest_url.find(":") != manifest_url.find("/") - 1:
        joined = urllib.parse.urljoin("gopher://" + manifest_url, fetch_url)
        return joined.removeprefix("gopher://")
    return urllib.parse.urljoin(manifest_url, fetch_url)


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
    ctx: _Context,
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
            scope.remotes[name] = _Remote(
                name=name,
                fetch=_resolve_fetch_url(_require(elem, "fetch", context), repo_url),
                revision=elem.attrib.get("revision"),
            )

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
            included_root = _fetch_xml(ctx.file_fetcher, repo_url, ref, include_name, source_label)
            _process_manifest(
                included_root,
                repo_url=repo_url,
                ref=ref,
                filename=include_name,
                path_prefix=path_prefix,
                visited=visited,
                scope=scope,
                ctx=ctx,
                source_label=source_label,
            )

        elif elem.tag == "submanifest":
            _process_submanifest(
                elem,
                scope=scope,
                ctx=ctx,
                visited=visited,
                path_prefix=path_prefix,
                parent_manifest_url=repo_url,
                context=context,
            )

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
    ctx: _Context,
    visited: frozenset[tuple[str, str, str]],
    path_prefix: str,
    parent_manifest_url: str,
    context: str,
) -> None:
    name = _require(elem, "name", context)
    project_name = elem.attrib.get("project")
    remote_alias = elem.attrib.get("remote")
    if remote_alias and not project_name:
        raise ManifestResolutionError(f"submanifest '{name}' in {context} sets 'remote' but not 'project'")

    if project_name:
        alias = remote_alias or scope.default.remote
        remote = scope.remotes.get(alias)
        if remote is None:
            raise ManifestResolutionError(f"submanifest '{name}' in {context} references unknown remote '{alias}'")
        sub_repo_url = _join_fetch_url(remote.fetch, project_name)
    else:
        # Without a 'project', the submanifest lives in the *parent manifest's own repository*
        # -- deriving a URL from the remote's fetch base and the submanifest name instead
        # invents a repository that does not exist.
        sub_repo_url = parent_manifest_url

    # A submanifest's revision falls back to its name, not to the remote's or default's revision.
    sub_ref = elem.attrib.get("revision") or name
    manifest_name = elem.attrib.get("manifest-name", "default.xml")
    sub_path_prefix = _join_path(path_prefix, elem.attrib.get("path") or sub_ref.split("/")[-1])
    sub_label = f"submanifest:{name}"

    sub_scope = _Scope()
    sub_ctx = ctx
    try:
        local_xml = ctx.submanifest_lookup(sub_path_prefix, manifest_name) if ctx.submanifest_lookup else None
        if local_xml is None:
            sub_root = _fetch_xml(ctx.file_fetcher, sub_repo_url, sub_ref, manifest_name, sub_label)
        else:
            # The workspace already has this submanifest checked out, so read it from there and
            # resolve its own <include>s from the same local checkout instead of over the network.
            logger.debug("using locally checked out submanifest '%s' for %s", name, sub_path_prefix)
            try:
                sub_root = ET.fromstring(local_xml)
            except ET.ParseError as exc:
                raise ManifestResolutionError(f"invalid XML in local submanifest '{name}' ({manifest_name}): {exc}") from exc
            sub_ctx = replace(ctx, file_fetcher=_LocalSubmanifestFetcher(ctx.submanifest_lookup, sub_path_prefix))

        _process_manifest(
            sub_root,
            repo_url=sub_repo_url,
            ref=sub_ref,
            filename=manifest_name,
            path_prefix=sub_path_prefix,
            visited=visited,
            scope=sub_scope,
            ctx=sub_ctx,
            source_label=sub_label,
        )
    except ManifestResolutionError as exc:
        # A submanifest lives in its own repository, which may be private, retired, or simply
        # not readable with the credentials at hand. Losing one of them should not throw away
        # the results for every other repo in the manifest, so degrade to a warning by default.
        if ctx.strict:
            raise
        # Reported through ctx.warnings (which the CLI prints prominently), so only log at debug
        # level here -- logging it again at warning level just duplicates it on the console.
        # The full message is kept; shortening for the console is the caller's job.
        ctx.warnings.append(f"submanifest '{name}' ({sub_repo_url}@{sub_ref}): {exc}")
        logger.debug("skipping submanifest '%s' (%s@%s): %s", name, sub_repo_url, sub_ref, exc)
        return
    scope.projects.update(sub_scope.projects)


def resolve_manifest(
    manifest_repo_url: str,
    manifest_ref: str,
    file_fetcher: FileFetcherChain,
    root_file: str = "default.xml",
    local_manifest_dir: str | Path | None = None,
    root_xml: bytes | None = None,
    strict: bool = False,
    warnings: list[str] | None = None,
    submanifest_lookup: SubmanifestLookup | None = None,
) -> list[ProjectRef]:
    """Resolve a manifest tree into a flat project list.

    `root_xml`, when given, supplies the root manifest's content directly instead of fetching
    `root_file` from the manifest repo. Any <include>/<submanifest> it contains is still resolved
    remotely against manifest_repo_url, which is exactly how repo treats its generated
    .repo/manifest.xml: as if that file lived inside the manifest repo.

    An unreachable <submanifest> is reported through `warnings` and skipped; pass strict=True to
    make it abort instead. <include> failures are always fatal: an include lives in the manifest
    repo we could already read, so failing to read it means the manifest itself is inconsistent.
    """
    ctx = _Context(
        file_fetcher=file_fetcher,
        strict=strict,
        warnings=warnings if warnings is not None else [],
        submanifest_lookup=submanifest_lookup,
    )
    scope = _Scope()
    if root_xml is None:
        root = _fetch_xml(file_fetcher, manifest_repo_url, manifest_ref, root_file, "root")
    else:
        try:
            root = ET.fromstring(root_xml)
        except ET.ParseError as exc:
            raise ManifestResolutionError(f"invalid XML in root manifest {root_file}: {exc}") from exc
    _process_manifest(
        root,
        repo_url=manifest_repo_url,
        ref=manifest_ref,
        filename=root_file,
        path_prefix="",
        visited=frozenset(),
        scope=scope,
        ctx=ctx,
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
