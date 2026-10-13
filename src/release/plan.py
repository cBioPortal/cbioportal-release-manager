"""Stage 1: pin, decide the version, and refuse to start on a bad footing.

Its whole job is to turn "master looks ready" into a concrete, recorded intention --
and to fail before anything irreversible happens. The only writes are labelling
unlabelled PRs with the catch-all label and re-running release-drafter.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import yaml

from . import version as ver
from .gh import GitHub

logger = logging.getLogger(__name__)

PR_BULLET = re.compile(r"^\s*[-*] .*\(#(\d+)\)\s*$")
HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*$")
POM_VERSION = re.compile(r"<version>([^<]+)</version>")
POM_FRONTEND = re.compile(r"<frontend\.version>([^<]+)</frontend\.version>")


class PreflightError(RuntimeError):
    """A precondition failed. The release must not start."""


@dataclass
class Preflight:
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def check(self, ok: bool, message: str) -> None:
        if not ok:
            self.errors.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)


def category_titles(gh: GitHub, repo: str, ref: str) -> list[str]:
    """Category headings release-drafter can emit for this repo, in config order."""
    raw = gh.file_contents(repo, ".github/release-drafter.yml", ref)
    if not raw:
        raise PreflightError(f"{repo}: .github/release-drafter.yml not found at {ref}")
    config = yaml.safe_load(raw)
    return [category["title"] for category in config.get("categories", [])]


def uncategorised_prs(body: str, titles: list[str] | set[str]) -> list[int]:
    """PR numbers rendered outside any known category.

    release-drafter puts label-less PRs at the top of $CHANGES with no category
    heading of their own; the backend template happens to emit a literal
    `## Changes` above that spot. Either way they sit under a heading that is not
    one of the configured category titles.
    """
    numbers: list[int] = []
    current = None
    for line in body.splitlines():
        heading = HEADING.match(line)
        if heading:
            current = heading.group(1)
            continue
        bullet = PR_BULLET.match(line)
        if bullet and current not in titles:
            numbers.append(int(bullet.group(1)))
    return numbers


def count_prs(body: str) -> int:
    return len([line for line in body.splitlines() if PR_BULLET.match(line)])


def file_uncategorised(gh: GitHub, config: dict, repo: str, branch: str,
                       titles: list[str], loose: list[int], dry_run: bool,
                       sleep=time.sleep) -> str | None:
    """Label loose PRs with the catch-all and wait for the draft to re-file them.

    Returns the re-rendered draft body, or None on a dry run. release-drafter is
    dispatched rather than left to the `labeled` event: for PRs from forks that run
    gets a read-only token, cannot see the draft, and fails.
    """
    label = config["changelog"]["catch_all_label"]
    workflow = config["changelog"]["drafter_workflow"]
    listed = ", ".join(f"#{n}" for n in loose)
    if dry_run:
        logger.info("[dry-run] %s: would label %s %s and re-run %s", repo, listed, label,
                    workflow)
        return None

    for number in loose:
        gh.add_labels(repo, number, [label])
    logger.info("%s: labelled %s %s; re-running %s", repo, listed, label, workflow)
    gh.dispatch_workflow(repo, workflow, branch, {})

    attempts = config["timeouts"]["drafter_attempts"]
    interval = config["timeouts"]["drafter_interval_seconds"]
    body = ""
    for attempt in range(1, attempts + 1):
        sleep(interval)
        body = (gh.draft_release(repo) or {}).get("body") or ""
        if not uncategorised_prs(body, titles):
            logger.info("%s: draft re-rendered", repo)
            return body
        logger.info("%s: draft not re-rendered yet (%d/%d)", repo, attempt, attempts)
    return body


def verify_checks(gh: GitHub, repo: str, sha: str, required: list[str],
                  pre: Preflight) -> None:
    if not required:
        logger.warning("%s: no required checks configured; CI is not gating this release",
                       repo)
        return
    logger.info("checking %d required checks on %s", len(required), repo)
    states = gh.combined_state(repo, sha)
    for name in required:
        state = states.get(name)
        if state is None:
            pre.check(False, f"{repo}@{sha[:8]}: required check {name!r} did not report")
        elif state == "pending":
            pre.check(False, f"{repo}@{sha[:8]}: required check {name!r} is still running")
        elif state not in ("success", "neutral", "skipped"):
            pre.check(False, f"{repo}@{sha[:8]}: required check {name!r} is {state}")


def build(gh: GitHub, config: dict, bump: str = "patch", dry_run: bool = False,
          sleep=time.sleep) -> dict:
    repos = config["repos"]
    branches = config["branches"]
    pre = Preflight()

    frontend_sha = gh.branch_sha(repos["frontend"], branches["frontend"])
    logger.info("%s@%s is %s", repos["frontend"], branches["frontend"], frontend_sha[:8])
    backend_sha = gh.branch_sha(repos["backend"], branches["backend"])
    logger.info("%s@%s is %s", repos["backend"], branches["backend"], backend_sha[:8])

    # Version: previous release + 1. Both repos tag in lockstep, so a mismatch
    # means someone released one without the other and the operator must look.
    logger.info("reading published releases")
    frontend_latest = ver.latest(
        [r["tag_name"] for r in gh.published_releases(repos["frontend"])]
    )
    backend_latest = ver.latest(
        [r["tag_name"] for r in gh.published_releases(repos["backend"])]
    )
    if frontend_latest != backend_latest:
        raise PreflightError(
            f"repos are out of lockstep: frontend is at {frontend_latest}, "
            f"backend at {backend_latest}. Reconcile before releasing."
        )
    previous = frontend_latest
    target = ver.bump(previous, bump)
    logger.info("latest release is %s in both repos; target is %s (%s bump)",
                previous, target, bump)

    if gh.release_by_tag(repos["frontend"], target) or gh.release_by_tag(repos["backend"], target):
        raise PreflightError(f"{target} is already released in at least one repo")

    # The backend pom's -SNAPSHOT records what the next version was expected to be.
    # A mismatch is worth surfacing but is not fatal: --bump minor/major legitimately
    # diverges from it.
    logger.info("reading the backend pom")
    pom = gh.file_contents(repos["backend"], "pom.xml", backend_sha) or ""
    snapshots = [v for v in POM_VERSION.findall(pom) if v.endswith("-SNAPSHOT")]
    pom_snapshot = snapshots[0] if snapshots else None
    if pom_snapshot and pom_snapshot != f"{target}-SNAPSHOT":
        pre.warn(
            f"backend pom is at {pom_snapshot}, expected {target}-SNAPSHOT for this release"
        )
    frontend_pinned = POM_FRONTEND.search(pom)

    verify_checks(gh, repos["frontend"], frontend_sha,
                  config["required_checks"]["frontend"], pre)
    verify_checks(gh, repos["backend"], backend_sha,
                  config["required_checks"]["backend"], pre)

    drafts = {}
    for key in ("frontend", "backend"):
        repo = repos[key]
        logger.info("reading the %s draft", repo)
        draft = gh.draft_release(repo)
        if not draft:
            raise PreflightError(f"{repo}: no release-drafter draft found")
        body = draft.get("body") or ""
        titles = category_titles(gh, repo, branches[key])
        loose = uncategorised_prs(body, titles)
        if loose:
            listed = ", ".join(f"#{n}" for n in loose)
            label = config["changelog"]["catch_all_label"]
            refiled = file_uncategorised(gh, config, repo, branches[key], titles, loose,
                                         dry_run, sleep)
            if refiled is None:
                pre.warn(f"{repo}: {len(loose)} unlabelled PR(s) would be filed under "
                         f"{label}: {listed}")
            else:
                body = refiled
                still = uncategorised_prs(body, titles)
                pre.check(
                    not still,
                    f"{repo}: {len(still)} PR(s) are still uncategorised after labelling "
                    f"them {label}: {', '.join(f'#{n}' for n in still)}",
                )
                if not still:
                    pre.warn(f"{repo}: filed {len(loose)} unlabelled PR(s) under {label}: "
                             f"{listed}")
                loose = still
        count = count_prs(body)
        drafts[key] = {"id": draft["id"], "pr_count": count}
        if loose:
            logger.warning("%s: %d of %d PRs have no changelog label", repo, len(loose), count)
        else:
            logger.info("%s: %d PRs, all labelled", repo, count)

    if pre.errors:
        raise PreflightError("preflight failed:\n  - " + "\n  - ".join(pre.errors))

    return {
        "version": target,
        "previous_version": previous,
        "bump": bump,
        "created_at": datetime.now(UTC).isoformat(),
        "frontend": {
            "repo": repos["frontend"],
            "branch": branches["frontend"],
            "sha": frontend_sha,
            "pr_count": drafts["frontend"]["pr_count"],
        },
        "backend": {
            "repo": repos["backend"],
            "branch": branches["backend"],
            "sha": backend_sha,
            "pr_count": drafts["backend"]["pr_count"],
            "pom_snapshot": pom_snapshot,
            "pom_frontend_version": frontend_pinned.group(1) if frontend_pinned else None,
        },
        "warnings": pre.warnings,
    }


def render(plan: dict) -> str:
    front, back = plan["frontend"], plan["backend"]
    lines = [
        f"## Release plan: {plan['version']}",
        "",
        (f"- **Version** `{plan['previous_version']}` -> `{plan['version']}` "
         f"({plan['bump']} bump)"),
        (f"- **Frontend** `{front['sha'][:8]}` on `{front['branch']}`, "
         f"{front['pr_count']} PRs in the draft"),
        (f"- **Backend** `{back['sha'][:8]}` on `{back['branch']}`, "
         f"{back['pr_count']} PRs in the draft"),
        (f"- **Backend pom** version `{back['pom_snapshot']}`, "
         f"frontend pinned at `{back['pom_frontend_version']}`"),
    ]
    if plan["warnings"]:
        lines += ["", "### Warnings", ""]
        lines += [f"- {w}" for w in plan["warnings"]]
    return "\n".join(lines)


def write(plan: dict, path: str | Path) -> None:
    Path(path).write_text(json.dumps(plan, indent=2) + "\n")


def read(path: str | Path) -> dict:
    return json.loads(Path(path).read_text())
