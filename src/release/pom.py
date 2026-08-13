"""Stages 5 and 8: the two backend pom edits, each as a reviewed PR.

Both use `mvn versions:set` rather than sed. The disabled `snapshot-release.yml`
is a standing demonstration of why: its sed looked for `<version>...-SNAPSHOT</version>`
in a pom whose suffix the frontend workflow had already stripped, matched nothing,
produced no diff, and died on `git commit` with "nothing to commit". A sed that
matches nothing looks identical to a sed that had nothing to do.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from pathlib import Path

from . import version as ver
from .gh import GitHub

logger = logging.getLogger(__name__)

PROJECT_VERSION = re.compile(r"<version>([^<]+)</version>")
FRONTEND_VERSION = re.compile(r"<frontend\.version>([^<]+)</frontend\.version>")


class PomError(RuntimeError):
    pass


def run(command: list[str], cwd: Path) -> str:
    logger.debug("$ %s", " ".join(command))
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise PomError(
            f"{' '.join(command)} failed ({result.returncode}):\n"
            f"{result.stdout[-2000:]}\n{result.stderr[-2000:]}"
        )
    return result.stdout


def read_values(workdir: Path) -> tuple[str, str | None]:
    """(project version, frontend.version) as the pom currently stands."""
    text = (workdir / "pom.xml").read_text()
    # The first <version> after <parent> belongs to the parent; the project's own
    # version is the first one that is not inside the parent block.
    without_parent = re.sub(r"<parent>.*?</parent>", "", text, flags=re.S)
    project = PROJECT_VERSION.search(without_parent)
    frontend = FRONTEND_VERSION.search(text)
    if not project:
        raise PomError("could not find the project <version> in pom.xml")
    return project.group(1), (frontend.group(1) if frontend else None)


def parent_version(workdir: Path) -> str | None:
    text = (workdir / "pom.xml").read_text()
    parent = re.search(r"<parent>.*?</parent>", text, flags=re.S)
    if not parent:
        return None
    match = PROJECT_VERSION.search(parent.group(0))
    return match.group(1) if match else None


def set_version(workdir: Path, new_version: str) -> None:
    logger.info("setting the project version to %s", new_version)
    run(
        ["mvn", "--batch-mode", "-q", "versions:set",
         f"-DnewVersion={new_version}", "-DgenerateBackupPoms=false"],
        workdir,
    )


def set_property(workdir: Path, prop: str, value: str) -> None:
    logger.info("setting %s to %s", prop, value)
    run(
        ["mvn", "--batch-mode", "-q", "versions:set-property",
         f"-Dproperty={prop}", f"-DnewVersion={value}", "-DgenerateBackupPoms=false"],
        workdir,
    )


def assert_only_pom_changed(workdir: Path) -> None:
    changed = run(["git", "diff", "--name-only"], workdir).split()
    if changed != ["pom.xml"]:
        raise PomError(f"expected only pom.xml to change, got: {changed or 'nothing'}")


# ---- the shared branch/PR/merge path ---------------------------------------


def open_and_merge(gh: GitHub, config: dict, workdir: Path, repo: str, base: str,
                   branch: str, message: str, body: str, dry_run: bool) -> dict:
    if dry_run:
        logger.info("[dry-run] would commit %r on %s and merge into %s", message, branch, base)
        logger.info("%s", run(["git", "diff"], workdir))
        return {"dry_run": True}

    run(["git", "config", "user.name", "cbioportal-release-manager"], workdir)
    run(["git", "config", "user.email",
         "cbioportal-release-manager@users.noreply.github.com"], workdir)
    run(["git", "checkout", "-B", branch], workdir)
    run(["git", "add", "pom.xml"], workdir)
    run(["git", "commit", "-m", message], workdir)
    run(["git", "push", "--force-with-lease", "origin", branch], workdir)

    pull = gh.find_pull(repo, branch, base) or gh.create_pull(
        repo, branch, base, message, body
    )
    logger.info("%s: PR #%s %s", repo, pull["number"], pull["html_url"])

    await_pr_checks(gh, config, repo, pull["number"])
    gh.merge_pull(repo, pull["number"], method="squash")
    logger.info("%s: merged #%s", repo, pull["number"])
    return pull


def await_pr_checks(gh: GitHub, config: dict, repo: str, number: int,
                    sleep=time.sleep) -> None:
    key = "backend" if repo == config["repos"]["backend"] else "frontend"
    required = config["required_checks"][key]
    attempts = config["timeouts"]["checks_attempts"]
    interval = config["timeouts"]["checks_interval_seconds"]

    for attempt in range(1, attempts + 1):
        pull = gh.get(f"/repos/{repo}/pulls/{number}")
        states = gh.combined_state(repo, pull["head"]["sha"])
        pending = [n for n in required if states.get(n) in (None, "pending")]
        failed = [n for n in required
                  if states.get(n) not in (None, "pending", "success", "neutral", "skipped")]
        if failed:
            raise PomError(f"{repo} PR #{number}: required checks failed: {failed}")
        if not pending:
            logger.info("%s PR #%s: required checks green", repo, number)
            return
        logger.info("waiting on %s (%d/%d)", pending, attempt, attempts)
        sleep(interval)
    raise PomError(f"{repo} PR #{number}: checks did not finish in time")


# ---- stage 5 ---------------------------------------------------------------


def bump_frontend(gh: GitHub, config: dict, plan: dict, workdir: Path,
                  dry_run: bool) -> dict:
    target = plan["version"]
    repo = config["repos"]["backend"]
    base = config["branches"]["backend"]
    prop = config["pom"]["frontend_version_property"]

    current_version, current_frontend = read_values(workdir)
    if current_version == target and current_frontend == target:
        logger.info("pom already at %s with %s=%s -- skipping", target, prop, target)
        return {"skipped": True}

    set_version(workdir, target)
    set_property(workdir, prop, target)
    assert_only_pom_changed(workdir)

    after_version, after_frontend = read_values(workdir)
    if (after_version, after_frontend) != (target, target):
        raise PomError(
            f"pom edit did not take: version={after_version}, {prop}={after_frontend}"
        )

    drift = drift_note(gh, repo, base, plan["backend"]["sha"])
    return open_and_merge(
        gh, config, workdir, repo, base,
        branch=f"release/{target}-frontend",
        message=f"Frontend {target}",
        body=(
            f"Points the backend at frontend `{target}`.\n\n"
            f"- `<version>`: `{current_version}` -> `{target}`\n"
            f"- `<{prop}>`: `{current_frontend}` -> `{target}`\n\n"
            f"Opened by cbioportal-release-manager.{drift}"
        ),
        dry_run=dry_run,
    )


def drift_note(gh: GitHub, repo: str, base: str, pinned: str) -> str:
    """Report, but do not block on, commits merged since stage 1 pinned the SHA.

    Aborting here would strand an already-published frontend release, so drift is
    surfaced rather than treated as fatal; stage 6 regenerates the notes so they
    still describe whatever ends up tagged.
    """
    head = gh.branch_sha(repo, base)
    if head == pinned:
        return ""
    logger.warning("%s moved past the pinned %s since this release was planned",
                   base, pinned[:8])
    compare = gh.get(f"/repos/{repo}/compare/{pinned}...{head}") or {}
    count = compare.get("ahead_by", "some")
    return (
        f"\n\n> **Note:** `{base}` moved {count} commit(s) since this release was "
        f"planned at `{pinned[:8]}`. Those commits are included in this release; the "
        f"notes are regenerated to match."
    )


# ---- stage 8 ---------------------------------------------------------------


def bump_snapshot(gh: GitHub, config: dict, plan: dict, workdir: Path,
                  dry_run: bool) -> dict:
    released = plan["version"]
    target = ver.snapshot(released)
    repo = config["repos"]["backend"]
    base = config["branches"]["backend"]

    current_version, current_frontend = read_values(workdir)
    before_parent = parent_version(workdir)

    if current_version.endswith("-SNAPSHOT"):
        current_release = current_version[: -len("-SNAPSHOT")]
        if ver.is_release_tag(current_release) and ver.parse(current_release) > ver.parse(
            released
        ):
            logger.info("pom already at %s, ahead of %s -- skipping",
                        current_version, released)
            return {"skipped": True}

    set_version(workdir, target)
    assert_only_pom_changed(workdir)

    after_version, after_frontend = read_values(workdir)
    if after_version != target:
        raise PomError(f"snapshot bump did not take: version={after_version}")
    if after_frontend != current_frontend:
        raise PomError(
            f"snapshot bump changed the frontend pin: "
            f"{current_frontend} -> {after_frontend}"
        )
    if parent_version(workdir) != before_parent:
        raise PomError("snapshot bump changed the parent version")

    return open_and_merge(
        gh, config, workdir, repo, base,
        branch=f"release/{target}",
        message=f"Prepare for {target}",
        body=(
            f"Reopens `{base}` for development after `{released}`.\n\n"
            f"- `<version>`: `{current_version}` -> `{target}`\n"
            f"- frontend pin left at `{current_frontend}`\n\n"
            f"Opened by cbioportal-release-manager."
        ),
        dry_run=dry_run,
    )
