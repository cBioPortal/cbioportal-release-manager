"""Stage 9: the hand-off.

Writes the job summary and files one issue per release, assigned to whoever owns the
deploy. On success the issue is the signal to go deploy; on failure it is the record
of what is half-finished. It is the artifact the current process lacks entirely --
today nothing says "v7.0.6 is out and this exact digest is what you deploy".
"""

from __future__ import annotations

import datetime
import logging
import os

from . import publish
from .gh import GitHub

logger = logging.getLogger(__name__)

OK = ("success", "skipped")


def world_state(gh: GitHub, config: dict, plan: dict) -> dict:
    """Ask the world what actually happened, rather than trusting the job results."""
    version = plan["version"]
    repos = config["repos"]
    maven = config["maven"]
    docker = config["docker"]

    frontend_release = gh.release_by_tag(repos["frontend"], version)
    backend_release = gh.release_by_tag(repos["backend"], version)

    pom = gh.file_contents(repos["backend"], "pom.xml", config["branches"]["backend"]) or ""
    from .plan import POM_FRONTEND, POM_VERSION

    snapshots = [v for v in POM_VERSION.findall(pom) if v.endswith("-SNAPSHOT")]
    frontend_pin = POM_FRONTEND.search(pom)

    images = {}
    for suffix in docker["tag_suffixes"]:
        tag = f"{version}{suffix}"
        try:
            result = publish.docker_manifest(docker["image"], tag)
        except Exception as error:  # a registry hiccup must not break the report
            logger.warning("could not read %s:%s from the registry: %s",
                           docker["image"], tag, error)
            images[tag] = {"error": str(error)}
            continue
        images[tag] = (
            {"digest": result[0], "platforms": result[1]} if result else {"missing": True}
        )

    return {
        "frontend_release": frontend_release["html_url"] if frontend_release else None,
        "backend_release": backend_release["html_url"] if backend_release else None,
        "frontend_on_central": publish.on_central(
            config, maven["frontend_artifact"], version
        ),
        "backend_on_central": publish.on_central(config, maven["backend_artifact"], version),
        "pom_snapshot": snapshots[0] if snapshots else None,
        "pom_frontend_version": frontend_pin.group(1) if frontend_pin else None,
        "images": images,
    }


def _stage_table(results: dict[str, str]) -> list[str]:
    icon = {"success": "✅", "skipped": "⏭️", "failure": "❌", "cancelled": "🚫"}
    rows = ["| Stage | Result |", "| --- | --- |"]
    rows += [
        f"| `{name}` | {icon.get(result, '❔')} {result} |" for name, result in results.items()
    ]
    return rows


def _deploy_tag(config: dict, version: str) -> str:
    suffixes = config["docker"]["tag_suffixes"]
    preferred = "-web-shenandoah" if "-web-shenandoah" in suffixes else suffixes[0]
    return f"{config['docker']['image']}:{version}{preferred}"


def success_body(config: dict, plan: dict, state: dict, results: dict, run_url: str) -> str:
    version = plan["version"]
    deploy_tag = _deploy_tag(config, version)
    image = state["images"].get(deploy_tag.split(":", 1)[1], {})

    lines = [
        f"`{version}` is released as a **pre-release** and its image is published.",
        "",
        "| | |",
        "| --- | --- |",
        f"| Frontend release | {state['frontend_release'] or '_missing_'} |",
        f"| Backend release | {state['backend_release'] or '_missing_'} |",
        f"| Maven Central | `io.github.cbioportal:cbioportal:{version}` |",
        f"| Image | `{deploy_tag}` |",
        f"| Digest | `{image.get('digest', 'unknown')}` |",
        f"| Platforms | {', '.join(image.get('platforms', [])) or 'unknown'} |",
        f"| Backend master now | `{state['pom_snapshot']}` |",
        "",
        "## Stages",
        "",
    ]
    lines += _stage_table(results)
    if plan.get("warnings"):
        lines += ["", "## Warnings", ""] + [f"- {w}" for w in plan["warnings"]]
    lines += ["", f"[Workflow run]({run_url})"]
    # Last, so it is what the reader is left on: the issue exists to get this done.
    lines += [
        "",
        "## 🚀 Next: deploy this",
        "",
        ("Deployment is deliberately not automated. Update the relevant deployments in "
         "`knowledgesystems/knowledgesystems-k8s-deployment` to the digest above, then "
         "close this issue."),
    ]
    return "\n".join(lines)


def failure_body(config: dict, plan: dict, state: dict, results: dict, run_url: str,
                 failed: list[str]) -> str:
    version = plan["version"]
    lines = [
        f"The `{version}` release stopped at **{', '.join(f'`{f}`' for f in failed)}**.",
        "",
        f"[Workflow run]({run_url}) — re-running the failed jobs is safe: every stage",
        "checks whether its work is already done and skips if so.",
        "",
        "## What actually exists right now",
        "",
        "| | |",
        "| --- | --- |",
        f"| Frontend release | {state['frontend_release'] or '❌ not created'} |",
        f"| Frontend on Maven Central | {'✅' if state['frontend_on_central'] else '❌'} |",
        (f"| Backend pom frontend pin | `{state['pom_frontend_version']}` "
         f"(target `{version}`) |"),
        f"| Backend release | {state['backend_release'] or '❌ not created'} |",
        f"| Backend on Maven Central | {'✅' if state['backend_on_central'] else '❌'} |",
        f"| Backend master version | `{state['pom_snapshot']}` |",
    ]
    for tag, info in state["images"].items():
        status = (
            f"`{info['digest']}`" if info.get("digest")
            else f"❌ {info.get('error', 'not published')}"
        )
        lines.append(f"| Image `{tag}` | {status} |")
    lines += ["", "## Stages", ""]
    lines += _stage_table(results)
    lines += ["", "**Do not deploy this version until the release completes.**"]
    return "\n".join(lines)


def build(gh: GitHub, config: dict, plan: dict, results: dict[str, str],
          run_url: str) -> tuple[str, str, bool]:
    """Return (title, body, succeeded)."""
    state = world_state(gh, config, plan)
    failed = [name for name, result in results.items() if result not in OK]
    title = f"Release {plan['version']}"
    if failed:
        return title, failure_body(config, plan, state, results, run_url, failed), False
    return title, success_body(config, plan, state, results, run_url), True


PROJECT_QUERY = """
query($org: String!, $number: Int!) {
  organization(login: $org) {
    projectV2(number: $number) {
      id
      fields(first: 50) {
        nodes {
          ... on ProjectV2SingleSelectField { id name options { id name } }
          ... on ProjectV2IterationField {
            id name
            configuration { iterations { id title startDate duration } }
          }
        }
      }
    }
  }
}
"""

ADD_ITEM = """
mutation($project: ID!, $content: ID!) {
  addProjectV2ItemById(input: {projectId: $project, contentId: $content}) {
    item { id }
  }
}
"""

SET_FIELD = """
mutation($project: ID!, $item: ID!, $field: ID!, $value: ProjectV2FieldValue!) {
  updateProjectV2ItemFieldValue(
    input: {projectId: $project, itemId: $item, fieldId: $field, value: $value}
  ) { projectV2Item { id } }
}
"""


def _current_iteration(iterations: list[dict]) -> dict | None:
    """The sprint today falls in. `iterations` excludes completed ones already."""
    today = datetime.date.today()
    for iteration in iterations:
        start = datetime.date.fromisoformat(iteration["startDate"])
        if start <= today < start + datetime.timedelta(days=iteration["duration"]):
            return iteration
    # Between sprints, or the board has run out: the next one up is the useful answer.
    return iterations[0] if iterations else None


def add_to_project(gh: GitHub, config: dict, issue: dict) -> None:
    """Put the issue on the team planning board, in Todo and the current sprint.

    Assigning is not enough: the sprint view only lists project items, so an issue
    that is merely assigned is invisible to the person who owns the deploy. Adding
    is idempotent, and re-runs re-assert both fields -- the release issue is meant
    to be freshly actionable after a retry.

    Failures here are logged, not raised. The issue is the deliverable; a board
    that did not get updated is worth a warning, not a red stage 9.
    """
    settings = config["project"]
    try:
        data = gh.graphql(PROJECT_QUERY, org=settings["org"], number=settings["number"])
        project = data["organization"]["projectV2"]
        fields = {node["name"]: node for node in project["fields"]["nodes"] if node}

        item = gh.graphql(
            ADD_ITEM, project=project["id"], content=issue["node_id"]
        )["addProjectV2ItemById"]["item"]["id"]

        status = fields[settings["status_field"]]
        wanted = settings["status_value"].casefold()
        option = next(o for o in status["options"] if o["name"].casefold() == wanted)
        gh.graphql(SET_FIELD, project=project["id"], item=item, field=status["id"],
                   value={"singleSelectOptionId": option["id"]})

        sprint = fields[settings["sprint_field"]]
        iteration = _current_iteration(sprint["configuration"]["iterations"])
        if iteration:
            gh.graphql(SET_FIELD, project=project["id"], item=item, field=sprint["id"],
                       value={"iterationId": iteration["id"]})
            logger.info("added to %s: %s / %s", project["id"], option["name"],
                        iteration["title"])
        else:
            logger.warning("%s has no open iteration; issue added without a sprint",
                           settings["sprint_field"])
    except Exception as error:  # a board hiccup must not fail the hand-off
        logger.warning("could not add the issue to project %s/%s: %s",
                       settings["org"], settings["number"], error)


def file_issue(gh: GitHub, config: dict, title: str, body: str, succeeded: bool,
               dry_run: bool) -> dict:
    """Create the release issue, or update it in place if this run is a retry."""
    repo = config["repos"]["self"]
    assignee = os.environ.get("RELEASE_ASSIGNEE", "").strip()
    assignees = [assignee] if assignee else []
    labels = ["release", "succeeded" if succeeded else "failed"]

    if not assignee:
        logger.warning("RELEASE_ASSIGNEE is unset; the release issue will be unassigned")

    if dry_run:
        logger.info("[dry-run] would file issue in %s: %s %s -> %s",
                    repo, title, labels, assignees)
        return {"dry_run": True}

    existing = gh.find_issue(repo, title)
    if existing:
        gh.update_issue(
            repo, existing["number"], body=body, labels=labels, state="open",
            assignees=assignees or [a["login"] for a in existing.get("assignees", [])],
        )
        gh.comment(
            repo,
            existing["number"],
            f"Updated after a re-run: **{'succeeded' if succeeded else 'failed'}**.",
        )
        logger.info("updated %s", existing["html_url"])
        issue = existing
    else:
        issue = gh.create_issue(repo, title, body, labels, assignees)
        logger.info("filed %s", issue["html_url"])

    add_to_project(gh, config, issue)
    return issue


def write_summary(body: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as handle:
            handle.write(body + "\n")
