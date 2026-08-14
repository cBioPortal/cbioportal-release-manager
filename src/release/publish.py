"""Stages 3, 4, 6 and 7: cut the releases and wait for the artifacts to really exist.

A stage is complete when Maven Central resolves the artifact or Docker Hub serves the
manifest list -- never when a workflow reports success. That distinction is the whole
point: for v7.0.5 the frontend publish workflow "succeeded" in one run while the run
carrying the backend pom update failed, and nothing noticed.
"""

from __future__ import annotations

import logging
import time

import requests

from .gh import GitHub, GitHubError

logger = logging.getLogger(__name__)

DOCKER_AUTH = "https://auth.docker.io/token"
DOCKER_REGISTRY = "https://registry-1.docker.io/v2"
MANIFEST_TYPES = ",".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)


class PublishError(RuntimeError):
    pass


# ---- Maven Central ---------------------------------------------------------


def maven_url(config: dict, artifact: str, version: str) -> str:
    maven = config["maven"]
    return (
        f"{maven['base']}/{maven['group_path']}/{artifact}/{version}/"
        f"{artifact}-{version}.pom"
    )


def on_central(config: dict, artifact: str, version: str,
               session: requests.Session | None = None) -> bool:
    """Check the .pom rather than the directory: a directory can exist half-uploaded."""
    session = session or requests.Session()
    response = session.head(maven_url(config, artifact, version), timeout=30,
                            allow_redirects=True)
    return response.status_code == 200


def await_central(config: dict, artifact: str, version: str,
                  session: requests.Session | None = None, sleep=time.sleep) -> None:
    attempts = config["timeouts"]["maven_attempts"]
    interval = config["timeouts"]["maven_interval_seconds"]
    for attempt in range(1, attempts + 1):
        if on_central(config, artifact, version, session):
            logger.info("%s %s is on Maven Central", artifact, version)
            return
        logger.info("not yet on Central (%d/%d); waiting %ds", attempt, attempts, interval)
        sleep(interval)
    raise PublishError(
        f"{artifact} {version} did not appear on Maven Central after "
        f"{attempts * interval // 60} minutes: {maven_url(config, artifact, version)}"
    )


# ---- Docker Hub ------------------------------------------------------------


def docker_manifest(image: str, tag: str,
                    session: requests.Session | None = None) -> tuple[str, list[str]] | None:
    """Return (digest, platforms) for a tag, or None if it is not published yet."""
    session = session or requests.Session()
    token = session.get(
        DOCKER_AUTH,
        params={"service": "registry.docker.io", "scope": f"repository:{image}:pull"},
        timeout=30,
    )
    token.raise_for_status()
    headers = {
        "Authorization": f"Bearer {token.json()['token']}",
        "Accept": MANIFEST_TYPES,
    }
    response = session.get(f"{DOCKER_REGISTRY}/{image}/manifests/{tag}", headers=headers,
                           timeout=30)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    digest = response.headers.get("Docker-Content-Digest", "")
    body = response.json()
    platforms = [
        f"{m['platform']['os']}/{m['platform']['architecture']}"
        for m in body.get("manifests", [])
        if m.get("platform", {}).get("architecture") != "unknown"
    ]
    return digest, platforms


def await_image(config: dict, version: str,
                session: requests.Session | None = None, sleep=time.sleep) -> dict[str, dict]:
    """Wait for every configured tag of the release image, and verify it is multi-arch.

    dockerimage.yml builds each architecture on its own runner and assembles the
    manifest list in a separate job, so a tag can exist while being single-arch.
    """
    docker = config["docker"]
    attempts = config["timeouts"]["docker_attempts"]
    interval = config["timeouts"]["docker_interval_seconds"]
    wanted = [f"{version}{suffix}" for suffix in docker["tag_suffixes"]]
    found: dict[str, dict] = {}

    for attempt in range(1, attempts + 1):
        for tag in wanted:
            if tag in found:
                continue
            result = docker_manifest(docker["image"], tag, session)
            if result:
                digest, platforms = result
                found[tag] = {"digest": digest, "platforms": platforms}
                logger.info("%s:%s -> %s %s", docker["image"], tag, digest, platforms)
        if len(found) == len(wanted):
            break
        logger.info("waiting for %s (%d/%d)", sorted(set(wanted) - set(found)),
                    attempt, attempts)
        sleep(interval)

    missing = sorted(set(wanted) - set(found))
    if missing:
        raise PublishError(
            f"image tags never appeared on Docker Hub: {', '.join(missing)}. "
            f"Found so far: {found or 'nothing'}"
        )

    for tag, info in found.items():
        absent = [p for p in docker["platforms"] if p not in info["platforms"]]
        if absent:
            raise PublishError(
                f"{docker['image']}:{tag} is missing {absent} -- the multi-arch merge "
                f"job in dockerimage.yml probably failed. Published: {info['platforms']}"
            )
    return found


# ---- GitHub releases -------------------------------------------------------


def cut_release(gh: GitHub, repo: str, tag: str, sha: str, body: str,
                dry_run: bool) -> dict:
    """Create the pre-release, or no-op if it already exists at the same commit."""
    existing = gh.release_by_tag(repo, tag)
    if existing:
        tagged = gh.ref_sha(repo, f"tags/{tag}")
        if tagged and sha and tagged != sha:
            raise PublishError(
                f"{repo}: tag {tag} already exists at {tagged[:8]}, expected {sha[:8]}. "
                f"Someone released this version already, or the pin is stale."
            )
        logger.info("%s: %s already released (%s) -- skipping", repo, tag,
                    existing["html_url"])
        return existing

    if dry_run:
        logger.info("[dry-run] %s: would create pre-release %s at %s (%d lines of notes)",
                    repo, tag, sha[:8], len(body.splitlines()))
        return {"html_url": f"https://github.com/{repo}/releases/tag/{tag}", "dry_run": True}

    release = gh.create_release(repo, tag, tag, body, sha, prerelease=True)
    logger.info("%s: created pre-release %s -> %s", repo, tag, release["html_url"])
    return release


# A run that has not finished yet, across the statuses GitHub reports.
ACTIVE = ("queued", "in_progress", "requested", "waiting", "pending")


def publish_runs(gh: GitHub, repo: str, workflow: str, tag: str) -> list[dict]:
    """Runs of the publish workflow for this tag, whatever event started them.

    Tag-triggered runs carry the tag in `head_branch`, so the branch filter finds
    both the `release: published` run and any manual dispatch.
    """
    return gh.workflow_runs(repo, workflow, branch=tag)


def ensure_publishing(gh: GitHub, repo: str, workflow: str, tag: str, dry_run: bool,
                      grace_seconds: int = 60, sleep=time.sleep) -> None:
    """Make sure exactly one publish is under way for this tag.

    Publishing the release already starts this workflow through `release: published`,
    so dispatching unconditionally would run a second concurrent `mvn deploy` of the
    same GAV -- the self-race that broke v7.0.5, recreated from this side. Dispatch
    only when nothing is running and nothing has already succeeded.

    The grace period covers the gap between creating the release and GitHub
    registering the run it triggered: this job starts within seconds of stage 3, and
    treating "no run yet" as "no run coming" would cause the very duplicate it is
    meant to prevent.
    """
    # Nothing will ever appear on a dry run, so do not sit out the grace period twice.
    deadline = 1 if dry_run else max(1, grace_seconds // 10)
    for attempt in range(1, deadline + 1):
        runs = publish_runs(gh, repo, workflow, tag)
        active = [r for r in runs if r["status"] in ACTIVE]
        if active:
            logger.info("%s: %s is already running for %s (%s)",
                        repo, workflow, tag, active[0]["html_url"])
            return
        if any(r["conclusion"] == "success" for r in runs):
            logger.info("%s: %s already succeeded for %s; waiting for Central to catch up",
                        repo, workflow, tag)
            return
        if runs:
            break  # runs exist and all of them failed -- retry below
        if attempt < deadline:
            logger.info("no %s run for %s yet; waiting for the release trigger (%d/%d)",
                        workflow, tag, attempt, deadline)
            sleep(10)

    if dry_run:
        logger.info("[dry-run] would dispatch %s in %s for %s", workflow, repo, tag)
        return

    if runs:
        logger.warning("%s: every %s run for %s failed; dispatching a retry",
                       repo, workflow, tag)
    else:
        logger.warning("%s: the release trigger never started %s for %s; dispatching",
                       repo, workflow, tag)
    try:
        gh.dispatch_workflow(repo, workflow, tag, {"source_ref": tag})
        logger.info("%s: dispatched %s for %s", repo, workflow, tag)
    except GitHubError as error:
        # Not fatal on its own: the poll below is what decides whether this worked.
        logger.warning("%s: could not dispatch %s (%s)", repo, workflow, error)
