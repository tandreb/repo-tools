"""Console table and JSON rendering for run results."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .models import ProjectRef, RunSummary

_COLUMNS = ("path", "fetch_url", "sha", "author", "date", "subject")
_HEADERS = ("Path", "Remote URL", "SHA", "Author", "Date", "Subject")


def _shorten(text: str, limit: int = 800) -> str:
    """Flatten and cap a message for console display; git's stderr is multi-line and verbose.

    Only the console output is shortened -- the JSON keeps the full text, since that is what
    anyone diagnosing an unreachable repository actually needs.
    """
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


def render_table(summary: RunSummary) -> str:
    if not summary.hits:
        return "No matching repositories found."

    rows: list[tuple[str, ...]] = []
    for hit in sorted(summary.hits, key=lambda h: h.project.path):
        meta = hit.metadata
        rows.append((
            hit.project.path,
            hit.project.fetch_url,
            hit.sha[:12],
            (meta.author if meta else None) or "-",
            (meta.date if meta else None) or "-",
            (meta.subject if meta else None) or "-",
        ))

    widths = [len(h) for h in _HEADERS]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt_row(cells: tuple[str, ...]) -> str:
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells))

    lines = [fmt_row(_HEADERS), "  ".join("-" * w for w in widths)]
    lines.extend(fmt_row(row) for row in rows)
    return "\n".join(lines)


def render_project_table(projects: list[ProjectRef]) -> str:
    """The manifest's resolved project list, for checking what would actually be searched."""
    if not projects:
        return "Manifest resolved to no projects at all."

    headers = ("Path", "Name", "Revision", "Remote URL", "From")
    rows = [
        (p.path, p.name, p.revision, p.fetch_url, p.source_manifest)
        for p in sorted(projects, key=lambda p: p.path)
    ]
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt_row(cells: tuple[str, ...]) -> str:
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells))

    lines = [fmt_row(headers), "  ".join("-" * w for w in widths)]
    lines.extend(fmt_row(row) for row in rows)
    return "\n".join(lines)


def build_json_payload(summary: RunSummary) -> dict:
    return {
        "branch": summary.branch,
        "manifest_url": summary.manifest_url,
        "manifest_branch": summary.manifest_branch,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "projects_total": summary.projects_total,
        "hits_count": len(summary.hits),
        "errors_count": len(summary.errors),
        "warnings": list(summary.warnings),
        "manifest_complete": not summary.warnings,
        "hits": [
            {
                "path": hit.project.path,
                "name": hit.project.name,
                "remote_alias": hit.project.remote_alias,
                "fetch_url": hit.project.fetch_url,
                "groups": list(hit.project.groups),
                "source_manifest": hit.project.source_manifest,
                "sha": hit.sha,
                "author": hit.metadata.author if hit.metadata else None,
                "date": hit.metadata.date if hit.metadata else None,
                "subject": hit.metadata.subject if hit.metadata else None,
                "metadata_source": hit.metadata.source if hit.metadata else "unavailable",
            }
            for hit in sorted(summary.hits, key=lambda h: h.project.path)
        ],
        "errors": [
            {
                "path": err.project.path,
                "fetch_url": err.project.fetch_url,
                "stage": err.stage,
                "message": err.message,
            }
            for err in summary.errors
        ],
    }


def write_json(summary: RunSummary, path: str | Path) -> None:
    payload = build_json_payload(summary)
    Path(path).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def print_summary_footer(summary: RunSummary, file=None) -> None:
    # Resolved here, not as a default argument: a `file=sys.stderr` default binds the stream at
    # import time, which makes the footer ignore any later redirect of sys.stderr.
    file = sys.stderr if file is None else file
    print(
        f"\n{len(summary.hits)}/{summary.projects_total} repos have branch '{summary.branch}', "
        f"{len(summary.errors)} unreachable/failed.",
        file=file,
    )
    if summary.warnings:
        # The project list itself is incomplete here, so say that plainly rather than letting the
        # counts above read as if the whole manifest had been searched.
        print(
            f"\nWARNING: {len(summary.warnings)} part(s) of the manifest could not be resolved, "
            "so repos below them were NOT searched:",
            file=file,
        )
        for warning in summary.warnings:
            print(f"  - {_shorten(warning)}", file=file)
        print(
            "  Hint: private repos may need --github-token/--gitlab-token or git credentials. "
            "Use --strict-manifest to treat this as a hard error.",
            file=file,
        )
