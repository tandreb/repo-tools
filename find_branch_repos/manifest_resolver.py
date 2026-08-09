"""Recursively resolve a repo-tool manifest tree into a flat list of ProjectRef.

Handles <remote>/<default> inheritance, <include>, <submanifest>, <remove-project>,
and an optional local-manifest directory (read from disk, since local manifests are
not centrally fetchable). Manifest file content itself is read via a FileFetcherChain,
never via a persistent checkout.

Resolution mirrors what `repo` itself does (XmlManifest._ParseManifestXml/_ParseManifest):
<include>s are first flattened into one node list, and only then are the nodes processed
*by element type* -- every <remote>, then <default>, then <submanifest>, then <project>,
and <remove-project> last. Element order inside and across manifest files therefore does
not matter, which is exactly what manifests built from `.inc` fragments rely on.
"""

from __future__ import annotations

import logging
import posixpath
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import Callable
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


@dataclass(frozen=True)
class _Node:
    """One manifest element plus where it came from, kept for error messages and reporting.

    <include> is resolved away while collecting, so a node's element is never an <include>.
    """

    elem: ET.Element
    context: str  # human-readable origin, e.g. 'vendor.inc@main (root)'
    source_label: str  # coarse origin recorded on ProjectRef: 'root', 'submanifest:x', ...


SubmanifestLookup = Callable[[str, str], bytes | None]
# (include name, context) -> raw XML; raises ManifestResolutionError if unreadable.
IncludeReader = Callable[[str, str], bytes]


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


def _parse_xml(content: bytes, what: str) -> ET.Element:
    try:
        return ET.fromstring(content)
    except ET.ParseError as exc:
        raise ManifestResolutionError(f"invalid XML in {what}: {exc}") from exc


def _read_remote_file(file_fetcher: FileFetcherChain, repo_url: str, ref: str, filename: str, context: str) -> bytes:
    try:
        content, _provider = file_fetcher.fetch(repo_url, ref, filename)
    except FileFetchError as exc:
        raise ManifestResolutionError(f"could not read {filename}@{ref} from {repo_url} ({context}): {exc}") from exc
    return content


def _collect_nodes(
    root: ET.Element,
    *,
    filename: str,
    source_label: str,
    describe: Callable[[str], str],
    read_include: IncludeReader,
    chain: tuple[str, ...] = (),
) -> list[_Node]:
    """Flatten one manifest file and everything it <include>s into a single node list.

    Nothing is interpreted here on purpose: repo decides what a <project> means only after every
    file is on the table, so a fragment may rely on a <remote> or <default> that is declared in a
    file included later on (or in the parent, below the <include> that pulled the fragment in).
    """
    if filename in chain:
        raise ManifestResolutionError("cyclic <include> detected: " + " -> ".join([*chain, filename]))
    chain = (*chain, filename)
    context = describe(filename)

    nodes: list[_Node] = []
    for elem in root:
        if elem.tag != "include":
            nodes.append(_Node(elem=elem, context=context, source_label=source_label))
            continue
        include_name = _require(elem, "name", context)
        included_root = _parse_xml(read_include(include_name, context), describe(include_name))
        nodes.extend(
            _collect_nodes(
                included_root,
                filename=include_name,
                source_label=source_label,
                describe=describe,
                read_include=read_include,
                chain=chain,
            )
        )
    return nodes


def _process_nodes(
    nodes: list[_Node],
    *,
    scope: _Scope,
    ctx: _Context,
    repo_url: str,
    path_prefix: str,
    visited_units: frozenset[tuple[str, str, str]],
) -> None:
    """Interpret a flattened node list in repo's element-type order, not in document order."""
    for node in nodes:
        if node.elem.tag == "remote":
            name = _require(node.elem, "name", node.context)
            scope.remotes[name] = _Remote(
                name=name,
                fetch=_resolve_fetch_url(_require(node.elem, "fetch", node.context), repo_url),
                revision=node.elem.attrib.get("revision"),
            )

    for node in nodes:
        if node.elem.tag == "default":
            # repo rejects a second, differing <default> outright. Merging attribute-wise is
            # deliberately more forgiving: a fragment that only pins a revision must not blank
            # out the remote and cost us every project that relies on it.
            scope.default = _Default(
                remote=node.elem.attrib.get("remote") or scope.default.remote,
                revision=node.elem.attrib.get("revision") or scope.default.revision,
            )

    for node in nodes:
        if node.elem.tag == "submanifest":
            _process_submanifest(
                node.elem,
                scope=scope,
                ctx=ctx,
                path_prefix=path_prefix,
                parent_manifest_url=repo_url,
                context=node.context,
                visited_units=visited_units,
            )

    for node in nodes:
        if node.elem.tag == "project":
            _add_project(
                node.elem,
                scope=scope,
                path_prefix=path_prefix,
                source_label=node.source_label,
                context=node.context,
            )

    # Last, exactly as repo does it: a <remove-project> drops a project no matter which file
    # added it, including files pulled in by an <include> further down.
    for node in nodes:
        if node.elem.tag == "remove-project":
            _remove_project(node.elem, scope=scope, path_prefix=path_prefix, context=node.context)

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


def _remove_project(elem: ET.Element, *, scope: _Scope, path_prefix: str, context: str) -> None:
    """Drop projects matched by <remove-project>, which selects by name and/or path.

    repo accepts either attribute ("remove-project must have name and/or path"); requiring
    `name` rejected perfectly valid manifests that remove a project by path alone.
    """
    name = elem.attrib.get("name")
    path = elem.attrib.get("path")
    if not name and not path:
        raise ManifestResolutionError(f"<remove-project> in {context} needs a 'name' and/or a 'path'")

    target_path = _join_path(path_prefix, path) if path else None
    for project_path, project in list(scope.projects.items()):
        if target_path is not None and project_path != target_path:
            continue
        if name and project.name != name:
            continue
        del scope.projects[project_path]
    # A remove-project matching nothing is left alone on purpose: repo errors out unless
    # optional="true", but for a read-only report that would only turn a harmless manifest
    # quirk into a lost repository list.


def _process_submanifest(
    elem: ET.Element,
    *,
    scope: _Scope,
    ctx: _Context,
    path_prefix: str,
    parent_manifest_url: str,
    context: str,
    visited_units: frozenset[tuple[str, str, str]],
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
    unit = (sub_repo_url, sub_ref, manifest_name)

    sub_scope = _Scope()
    try:
        if unit in visited_units:
            raise ManifestResolutionError(f"cyclic <submanifest>: {manifest_name}@{sub_ref} from {sub_repo_url}")

        local_xml = ctx.submanifest_lookup(sub_path_prefix, manifest_name) if ctx.submanifest_lookup else None
        if local_xml is None:
            sub_fetcher: FileFetcherChain = ctx.file_fetcher
            content = _read_remote_file(sub_fetcher, sub_repo_url, sub_ref, manifest_name, sub_label)
        else:
            # The workspace already has this submanifest checked out, so read it from there and
            # resolve its own <include>s from the same local checkout instead of over the network.
            logger.debug("using locally checked out submanifest '%s' for %s", name, sub_path_prefix)
            sub_fetcher = _LocalSubmanifestFetcher(ctx.submanifest_lookup, sub_path_prefix)
            content = local_xml

        def describe(fn: str, _ref: str = sub_ref, _label: str = sub_label) -> str:
            return f"{fn}@{_ref} ({_label})"

        def read_include(include_name: str, include_context: str) -> bytes:
            return _read_remote_file(sub_fetcher, sub_repo_url, sub_ref, include_name, include_context)

        nodes = _collect_nodes(
            _parse_xml(content, describe(manifest_name)),
            filename=manifest_name,
            source_label=sub_label,
            describe=describe,
            read_include=read_include,
        )
        _process_nodes(
            nodes,
            scope=sub_scope,
            ctx=ctx,
            repo_url=sub_repo_url,
            path_prefix=sub_path_prefix,
            visited_units=visited_units | {unit},
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


def _collect_local_manifest_nodes(directory: Path) -> list[_Node]:
    """Flatten .repo/local_manifests/*.xml (and whatever they <include>) into nodes.

    These are merged into the same node list as the main manifest, which is what repo does too --
    so a local manifest can use a <remote> from the main manifest, and its own <include>s work.
    """
    if not directory.is_dir():
        raise ManifestResolutionError(f"local manifest directory not found: {directory}")

    # repo resolves a local manifest's includes against the manifest checkout; in practice
    # fragments are just as often dropped next to the local manifest itself, so accept both.
    search_dirs = [directory, directory.parent / "manifests"]

    nodes: list[_Node] = []
    for xml_file in sorted(directory.glob("*.xml")):
        label = f"local manifest {xml_file.name}"

        def describe(fn: str, _file: str = xml_file.name, _label: str = label) -> str:
            return _label if fn == _file else f"{fn} (included from {_label})"

        def read_include(include_name: str, include_context: str) -> bytes:
            for base in search_dirs:
                candidate = base / include_name
                if candidate.is_file():
                    try:
                        return candidate.read_bytes()
                    except OSError as exc:
                        raise ManifestResolutionError(f"could not read {candidate} ({include_context}): {exc}") from exc
            searched = " or ".join(str(d) for d in search_dirs)
            raise ManifestResolutionError(f"<include name='{include_name}'> in {include_context} not found in {searched}")

        try:
            content = xml_file.read_bytes()
        except OSError as exc:
            raise ManifestResolutionError(f"could not read local manifest {xml_file}: {exc}") from exc
        nodes.extend(
            _collect_nodes(
                _parse_xml(content, label),
                filename=xml_file.name,
                source_label=label,
                describe=describe,
                read_include=read_include,
            )
        )
    return nodes


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
        content = _read_remote_file(file_fetcher, manifest_repo_url, manifest_ref, root_file, "root")
        root_what = f"{root_file}@{manifest_ref} (root)"
    else:
        content = root_xml
        root_what = f"root manifest {root_file}"

    def describe(fn: str) -> str:
        return f"{fn}@{manifest_ref} (root)"

    def read_include(include_name: str, include_context: str) -> bytes:
        return _read_remote_file(file_fetcher, manifest_repo_url, manifest_ref, include_name, include_context)

    nodes = _collect_nodes(
        _parse_xml(content, root_what),
        filename=root_file,
        source_label="root",
        describe=describe,
        read_include=read_include,
    )
    if local_manifest_dir:
        nodes.extend(_collect_local_manifest_nodes(Path(local_manifest_dir)))

    _process_nodes(
        nodes,
        scope=scope,
        ctx=ctx,
        repo_url=manifest_repo_url,
        path_prefix="",
        visited_units=frozenset(),
    )

    return sorted(scope.projects.values(), key=lambda p: p.path)
