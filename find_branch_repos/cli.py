"""CLI orchestration: manifest resolution -> parallel branch checks -> metadata -> output."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from . import file_fetchers, metadata_fetchers
from .branch_checker import InvalidBranchNameError, check_branch_on_all
from .manifest_resolver import ManifestResolutionError, resolve_manifest
from .models import BranchResult, ProjectRef, RepoError, RunSummary
from .output import print_summary_footer, render_table, write_json
from .repo_workspace import RepoWorkspaceError, resolve_from_repo_dir

logger = logging.getLogger(__name__)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="find_branch_repos",
        description="Find which remote repositories of a repo-tool manifest have a given branch, "
        "without checking out or modifying anything locally.",
    )
    parser.add_argument("--branch", required=True, help="Branch name to look for, e.g. feature/xyz")
    parser.add_argument(
        "--manifest-url", default=None,
        help="Fetch URL of the manifest git repository. Required unless --repo-dir is given.",
    )
    parser.add_argument(
        "--repo-dir", default=None,
        help="Path to an existing `repo`-tool workspace (its .repo dir, or the workspace root above it). "
        "Manifest URL/branch/root-file/local-manifest-dir are read from its local .repo metadata instead of "
        "having to be passed explicitly; any of those flags passed alongside --repo-dir still take precedence.",
    )
    parser.add_argument("--manifest-branch", default=None, help="Ref of the manifest repo to read (default: master, or auto-detected from --repo-dir)")
    parser.add_argument("--manifest-file", default=None, help="Root manifest file name (default: default.xml, or auto-detected from --repo-dir)")
    parser.add_argument("--local-manifest-dir", default=None, help="Optional directory with local manifest XML files to merge in (auto-detected from --repo-dir if not given)")
    parser.add_argument("--concurrency", type=int, default=64, help="Max parallel remote queries (default: 64)")
    parser.add_argument("--timeout", type=float, default=15.0, help="Per-repo timeout in seconds (default: 15)")
    parser.add_argument("--skip-metadata", action="store_true", help="Only report SHA, skip author/date/subject lookups")
    parser.add_argument("--json-out", default=None, help="Path to write the JSON result to")
    parser.add_argument("--github-token", default=None, help="Token for the GitHub API provider (default: $GITHUB_TOKEN)")
    parser.add_argument("--gitlab-token", default=None, help="Token for the GitLab API provider (default: $GITLAB_TOKEN)")
    parser.add_argument(
        "--strict-no-fetch",
        action="store_true",
        help="Never fall back to a minimal `git fetch`; only use HTTP APIs (Gitiles/GitHub/GitLab). "
        "Hosts without such an API yield SHA-only results (ls-remote) or no metadata.",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    return parser


async def _fetch_metadata(
    hits: dict[ProjectRef, str],
    branch: str,
    chain: metadata_fetchers.MetadataFetcherChain,
    concurrency: int,
) -> tuple[list[BranchResult], list[RepoError]]:
    semaphore = asyncio.Semaphore(concurrency)
    results: list[BranchResult] = []
    errors: list[RepoError] = []

    async def _run(project: ProjectRef, sha: str) -> None:
        async with semaphore:
            try:
                meta = await asyncio.to_thread(chain.fetch, project.fetch_url, branch, sha)
            except metadata_fetchers.MetadataFetchError as exc:
                errors.append(RepoError(project=project, stage="metadata", message=str(exc)))
                results.append(BranchResult(project=project, sha=sha, metadata=None))
                return
            results.append(BranchResult(project=project, sha=sha, metadata=meta))

    await asyncio.gather(*(_run(p, s) for p, s in hits.items()))
    return results, errors


def _dedupe(projects: list[ProjectRef]) -> list[ProjectRef]:
    seen: set[str] = set()
    deduped: list[ProjectRef] = []
    for project in projects:
        if project.fetch_url in seen:
            continue
        seen.add(project.fetch_url)
        deduped.append(project)
    return deduped


def _resolve_manifest_location(args: argparse.Namespace) -> tuple[str, str, str, str | None]:
    manifest_url = args.manifest_url
    manifest_branch = args.manifest_branch
    manifest_file = args.manifest_file
    local_manifest_dir = args.local_manifest_dir

    if args.repo_dir:
        info = resolve_from_repo_dir(args.repo_dir)
        manifest_url = manifest_url or info.manifest_url
        manifest_branch = manifest_branch or info.manifest_branch
        manifest_file = manifest_file or info.manifest_file
        local_manifest_dir = local_manifest_dir or info.local_manifest_dir
        print(
            f"Detected from --repo-dir: manifest-url={manifest_url} manifest-branch={manifest_branch} "
            f"manifest-file={manifest_file} local-manifest-dir={local_manifest_dir}",
            file=sys.stderr,
        )

    if not manifest_url:
        raise RepoWorkspaceError("either --manifest-url or --repo-dir is required")

    return manifest_url, manifest_branch or "master", manifest_file or "default.xml", local_manifest_dir


async def run(args: argparse.Namespace) -> RunSummary:
    github_token = args.github_token or os.environ.get("GITHUB_TOKEN")
    gitlab_token = args.gitlab_token or os.environ.get("GITLAB_TOKEN")
    allow_fetch_fallback = not args.strict_no_fetch

    manifest_url, manifest_branch, manifest_file, local_manifest_dir = _resolve_manifest_location(args)

    file_chain = file_fetchers.default_chain(github_token, gitlab_token, allow_fetch_fallback)

    projects = resolve_manifest(
        manifest_url,
        manifest_branch,
        file_chain,
        root_file=manifest_file,
        local_manifest_dir=local_manifest_dir,
    )
    projects = _dedupe(projects)

    summary = RunSummary(
        branch=args.branch,
        manifest_url=manifest_url,
        manifest_branch=manifest_branch,
        projects_total=len(projects),
    )

    hits, check_errors = await check_branch_on_all(projects, args.branch, args.concurrency, args.timeout)
    summary.errors.extend(RepoError(project=e.project, stage="ls-remote", message=e.message) for e in check_errors)

    if args.skip_metadata:
        summary.hits = [BranchResult(project=p, sha=sha, metadata=None) for p, sha in hits.items()]
    else:
        metadata_chain = metadata_fetchers.default_chain(github_token, gitlab_token, allow_fetch_fallback)
        results, meta_errors = await _fetch_metadata(hits, args.branch, metadata_chain, args.concurrency)
        summary.hits = results
        summary.errors.extend(meta_errors)

    return summary


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    try:
        summary = asyncio.run(run(args))
    except InvalidBranchNameError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except RepoWorkspaceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except ManifestResolutionError as exc:
        print(f"error: failed to resolve manifest: {exc}", file=sys.stderr)
        return 1

    print(render_table(summary))
    print_summary_footer(summary)

    if args.json_out:
        write_json(summary, args.json_out)
        print(f"JSON written to {args.json_out}", file=sys.stderr)

    return 2 if summary.errors else 0


if __name__ == "__main__":
    sys.exit(main())
