"""Stage 2: build one set of release notes out of two drafts.

Replaces the `pbpaste | sed 's|(#\\(....\\)|...'` one-liner from the release note.
That sed matched exactly four digits, so it silently left five-digit PR references
unlinked; the rewrite here is length-agnostic.

Notes are generated on demand rather than pinned at stage 2 and carried forward.
Stage 2 is a preview and a validation gate; stages 3 and 6 regenerate from the live
drafts, so if backend master moves mid-release the notes still describe what is
actually tagged.
"""

from __future__ import annotations

import logging
import re

from .gh import GitHub
from .plan import category_titles

logger = logging.getLogger(__name__)

PR_REF = re.compile(r"\(#(\d+)\)")
SECTION = re.compile(r"^##\s+(.*?)\s*$")
COMPARE_URL = re.compile(
    r"(https://github\.com/[^/\s]+/[^/\s]+/compare/)"
    r"[A-Za-z0-9._-]+\.\.\.[A-Za-z0-9._-]+"
)


def link_prs(body: str, repo: str) -> str:
    """Turn `(#1234)` into a cross-repo link, so frontend refs work in backend notes."""
    name = repo.split("/")[-1]
    return PR_REF.sub(
        lambda m: f"([{name}#{m.group(1)}](https://github.com/{repo}/pull/{m.group(1)}))",
        body,
    )


def correct_commit_log_links(body: str, plan: dict) -> str:
    """Set compare URLs in the full commit logs section to the planned release range."""
    release_range = f"{plan['previous_version']}...{plan['version']}"
    in_commit_logs = False
    corrected: list[str] = []

    for line in body.splitlines(keepends=True):
        heading = SECTION.match(line)
        if heading:
            in_commit_logs = heading.group(1).lower().endswith("full commit logs")
        if in_commit_logs:
            line = COMPARE_URL.sub(lambda match: f"{match.group(1)}{release_range}", line)
        corrected.append(line)

    return "".join(corrected)


def split_sections(body: str) -> list[tuple[str | None, list[str]]]:
    """Split a release body into (heading, lines) pairs. Leading text gets heading None."""
    sections: list[tuple[str | None, list[str]]] = []
    heading: str | None = None
    buffer: list[str] = []
    for line in body.splitlines():
        match = SECTION.match(line)
        if match:
            sections.append((heading, buffer))
            heading, buffer = match.group(1), []
        else:
            buffer.append(line)
    sections.append((heading, buffer))
    return [(h, b) for h, b in sections if h is not None or any(x.strip() for x in b)]


def bullets(lines: list[str]) -> list[str]:
    return [line.rstrip() for line in lines if line.strip().startswith(("-", "*"))]


def combine(backend_body: str, frontend_body: str, backend_titles: list[str],
            frontend_titles: list[str], repos: dict, plan: dict) -> str:
    """Merge the two drafts into the backend's combined body.

    The two release-drafter configs do not carry identical category lists -- the
    backend has REST/breaking-API sections the frontend lacks, the frontend has
    package improvements the backend lacks -- so the merge takes the union, backend
    order first, rather than treating a difference as an error.
    """
    frontend_body = link_prs(frontend_body, repos["frontend"])

    backend_sections = {
        h: bullets(b) for h, b in split_sections(backend_body) if h is not None
    }
    frontend_sections = {
        h: bullets(b) for h, b in split_sections(frontend_body) if h is not None
    }

    order = list(backend_titles) + [t for t in frontend_titles if t not in backend_titles]

    out: list[str] = []
    for title in order:
        merged = backend_sections.get(title, []) + frontend_sections.get(title, [])
        if merged:
            out += [f"## {title}", ""] + merged + [""]

    out += tail(backend_body, plan)
    return "\n".join(out).rstrip() + "\n"


def tail(backend_body: str, plan: dict) -> list[str]:
    """Keep the draft's trailing boilerplate with the planned compare range."""

    lines: list[str] = []
    for heading, body in split_sections(correct_commit_log_links(backend_body, plan)):
        if heading and ("commit log" in heading.lower() or "versioning" in heading.lower()):
            lines += [f"## {heading}", ""] + [b.rstrip() for b in body if b.strip()] + [""]
    return "\n".join(lines).splitlines()


def build(gh: GitHub, config: dict, plan: dict) -> dict[str, str]:
    """Return {'frontend': body, 'backend': body} for the two releases."""
    repos = config["repos"]
    branches = config["branches"]

    logger.info("reading both release-drafter drafts")
    frontend_draft = gh.draft_release(repos["frontend"])
    backend_draft = gh.draft_release(repos["backend"])
    if not frontend_draft or not backend_draft:
        raise RuntimeError("a release-drafter draft is missing; run preflight first")

    frontend_body = correct_commit_log_links(frontend_draft.get("body") or "", plan)
    backend_body = backend_draft.get("body") or ""

    backend_titles = category_titles(gh, repos["backend"], branches["backend"])
    frontend_titles = category_titles(gh, repos["frontend"], branches["frontend"])
    logger.info("merging %d categories", len(set(backend_titles) | set(frontend_titles)))

    return {
        "frontend": frontend_body.rstrip() + "\n",
        "backend": combine(
            backend_body, frontend_body, backend_titles, frontend_titles, repos, plan
        ),
    }
