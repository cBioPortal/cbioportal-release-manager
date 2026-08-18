"""Promotion: pre-release -> official, and the community artifacts that follow.

Two separate things on purpose. `scan` runs nightly and only ever opens an issue --
"we tested it in production for a month and saw no major issues" is a judgement call,
not a cron job. `promote` is dispatched by hand once that call is made.

The artifacts bumped here are what other institutions install from, not anything
running at MSK. They drift precisely because the step is manual and easy to skip:
as of 2026-08-12 the helm chart is on 6.4.1 against an official release of 7.0.2.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import UTC, datetime, timedelta

from . import board
from . import version as ver
from .gh import GitHub

logger = logging.getLogger(__name__)


class PromotionError(RuntimeError):
    pass


def _assignee() -> str:
    return os.environ.get("RELEASE_ASSIGNEE", "").strip()


def due(gh: GitHub, config: dict, now: datetime | None = None) -> list[dict]:
    """Pre-releases old enough to be considered, and still ahead of the official one."""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=config["promotion"]["age_days"])
    releases = gh.releases(config["repos"]["backend"])

    # Promotion only ever moves forward. Once a version is official, every
    # pre-release behind it is dead -- nobody will go back and promote v7.0.3
    # after v7.0.5 shipped -- so those must not keep showing up as outstanding.
    official = [
        ver.parse(release["tag_name"]) for release in releases
        if not release["draft"] and not release["prerelease"]
        and ver.is_release_tag(release["tag_name"])
    ]
    floor = max(official) if official else None

    out = []
    for release in releases:
        if release["draft"] or not release["prerelease"]:
            continue
        if not ver.is_release_tag(release["tag_name"]):
            continue
        if floor is not None and ver.parse(release["tag_name"]) <= floor:
            continue
        published = datetime.fromisoformat(release["published_at"].replace("Z", "+00:00"))
        if published <= cutoff:
            out.append(
                {
                    "tag": release["tag_name"],
                    "published_at": release["published_at"],
                    "age_days": (now - published).days,
                    "url": release["html_url"],
                }
            )
    return sorted(out, key=lambda r: ver.parse(r["tag"]))


def scan(gh: GitHub, config: dict, dry_run: bool) -> dict | None:
    candidates = due(gh, config)
    if not candidates:
        logger.info("no pre-releases are due for promotion")
        return None

    newest = candidates[-1]
    title = f"Promote {newest['tag']} to official?"
    repo = config["repos"]["self"]
    assignee = _assignee()

    body = "\n".join(
        [
            (f"`{newest['tag']}` has been a pre-release for {newest['age_days']} days "
             f"({newest['url']})."),
            "",
            "If it has run in production without major issues, promote it:",
            "",
            f"1. Run the **promote** workflow with `version = {newest['tag']}`.",
            "2. It flips both repos to official and opens the helm and",
            "   docker-compose bumps for review.",
            "",
            "Promoting it retires the pre-releases it skips past:",
            "",
        ]
        + ([f"- `{c['tag']}` ({c['age_days']}d)" for c in candidates[:-1]] or ["- none"])
    )

    if dry_run:
        logger.info("[dry-run] would open: %s", title)
        return None

    if gh.find_issue(repo, title):
        logger.info("issue already open for %s", newest["tag"])
        return None
    issue = gh.create_issue(repo, title, body, ["promotion"], [assignee] if assignee else [])
    logger.info("filed %s", issue["html_url"])
    board.add(gh, config, issue)
    return issue


# ---- the promotion itself --------------------------------------------------


def flip(gh: GitHub, config: dict, tag: str, dry_run: bool) -> None:
    for key in ("frontend", "backend"):
        repo = config["repos"][key]
        release = gh.release_by_tag(repo, tag)
        if not release:
            raise PromotionError(f"{repo}: no release {tag}")
        if not release["prerelease"]:
            logger.info("%s: %s is already official -- skipping", repo, tag)
            continue
        if dry_run:
            logger.info("[dry-run] %s: would mark %s official", repo, tag)
            continue
        gh.update_release(repo, release["id"], prerelease=False, make_latest="true")
        logger.info("%s: %s is now the official release", repo, tag)


def bump_downstream(gh: GitHub, config: dict, tag: str, dry_run: bool) -> list[dict]:
    """Open PRs bumping the chart and the compose file to the new official version.

    They land assigned and on the board: these are the PRs that historically get
    forgotten, which is why the chart drifts several minor versions behind.
    """
    plain = tag.lstrip("v")
    branch = f"release/{tag}"
    edits = [
        (
            config["repos"]["compose"],
            "master",
            [(".env", re.compile(r"(DOCKER_IMAGE_CBIOPORTAL=cbioportal/cbioportal:)\S+"),
              rf"\g<1>{plain}")],
        ),
        (
            config["repos"]["helm"],
            "main",
            [
                ("charts/cbioportal/Chart.yaml",
                 re.compile(r'(appVersion:\s*")[^"]+(")'), rf"\g<1>{plain}\g<2>"),
                ("charts/cbioportal/values.yaml",
                 re.compile(r"(image:\s*cbioportal/cbioportal:)\S+"),
                 rf"\g<1>{plain}-web-shenandoah"),
            ],
        ),
    ]

    assignee = _assignee()
    if not assignee:
        logger.warning("RELEASE_ASSIGNEE is unset; the bump PRs will be unassigned")

    pulls = []
    for repo, base, files in edits:
        changes = []
        for path, pattern, replacement in files:
            current = gh.file_contents(repo, path, base)
            if current is None:
                raise PromotionError(f"{repo}: {path} not found on {base}")
            updated, count = pattern.subn(replacement, current)
            if count == 0:
                raise PromotionError(
                    f"{repo}: pattern {pattern.pattern!r} matched nothing in {path}"
                )
            if updated != current:
                changes.append((path, updated))

        if not changes:
            logger.info("%s: already on %s -- skipping", repo, plain)
            continue
        if dry_run:
            logger.info("[dry-run] %s: would bump %s to %s",
                        repo, [p for p, _ in changes], plain)
            continue

        head = gh.branch_sha(repo, base)
        if not gh.ref_sha(repo, f"heads/{branch}"):
            gh.create_ref(repo, f"refs/heads/{branch}", head)
        for path, content in changes:
            gh.put_file(repo, path, content, f"Update cbioportal to {plain}", branch)

        pull = gh.find_pull(repo, branch, base) or gh.create_pull(
            repo, branch, base, f"Update cbioportal to {plain}",
            f"`{tag}` is now the official cBioPortal release.\n\n"
            f"Opened by cbioportal-release-manager.",
        )
        # A PR is an issue as far as the assignees endpoint is concerned.
        if assignee:
            gh.update_issue(repo, pull["number"], assignees=[assignee])
        board.add(gh, config, pull)
        logger.info("%s: %s", repo, pull["html_url"])
        pulls.append(pull)
    return pulls
