"""The team planning board.

Assigning someone is not enough to make work visible: the sprint view only lists
issues and PRs that are items on the project, with a status and an iteration set.
Anything the orchestrator files for a human therefore has to be added here too.

Projects v2 has no REST API, so this is the one part of the orchestrator that
speaks GraphQL.
"""

from __future__ import annotations

import datetime
import logging

from .gh import GitHub

logger = logging.getLogger(__name__)

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


def add(gh: GitHub, config: dict, item: dict) -> None:
    """Put an issue or PR on the board, in the configured status and this sprint.

    Adding is idempotent, and re-runs re-assert both fields -- what the orchestrator
    files is meant to be freshly actionable after a retry.

    Failures here are logged, not raised. The issue or PR is the deliverable; a
    board that did not get updated is worth a warning, not a failed release.
    """
    settings = config["project"]
    try:
        data = gh.graphql(PROJECT_QUERY, org=settings["org"], number=settings["number"])
        project = data["organization"]["projectV2"]
        fields = {node["name"]: node for node in project["fields"]["nodes"] if node}

        node = gh.graphql(
            ADD_ITEM, project=project["id"], content=item["node_id"]
        )["addProjectV2ItemById"]["item"]["id"]

        status = fields[settings["status_field"]]
        wanted = settings["status_value"].casefold()
        option = next(o for o in status["options"] if o["name"].casefold() == wanted)
        gh.graphql(SET_FIELD, project=project["id"], item=node, field=status["id"],
                   value={"singleSelectOptionId": option["id"]})

        sprint = fields[settings["sprint_field"]]
        iteration = _current_iteration(sprint["configuration"]["iterations"])
        if iteration:
            gh.graphql(SET_FIELD, project=project["id"], item=node, field=sprint["id"],
                       value={"iterationId": iteration["id"]})
            logger.info("board: %s -> %s / %s", item["html_url"], option["name"],
                        iteration["title"])
        else:
            logger.warning("%s has no open iteration; %s added without a sprint",
                           settings["sprint_field"], item["html_url"])
    except Exception as error:  # a board hiccup must not fail the release
        logger.warning("could not add %s to project %s/%s: %s",
                       item.get("html_url"), settings["org"], settings["number"], error)
