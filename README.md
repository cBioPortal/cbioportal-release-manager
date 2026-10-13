# cBioPortal Release Manager

Cuts a release across `cbioportal` and `cbioportal-frontend`, up to a published and
verified Docker image, then files an issue saying what to deploy. Deployment itself
is not automated.

## Local development

Requires `uv` and Python 3.11+. Java 21 and Maven are only needed for `bump-pom` and
`bump-snapshot`.

```bash
uv sync
uv run pytest
uv run ruff check .
uv run release --help
```

To run against the real repos you need a token that can read draft releases:

```bash
export GITHUB_TOKEN=$(gh auth token)
uv run release plan --plan /tmp/plan.json     # read-only
uv run release notes --plan /tmp/plan.json    # read-only
```

Every command that writes takes `--dry-run`, which does all the reads and prints the
intended writes instead of making them. `RELEASE_ASSIGNEE` (optional) sets who issues
and bump PRs are assigned to.

Logging: `release -v <command>` adds every HTTP call; `-q` shows warnings and errors
only. Diagnostics go to stderr. When adding log lines, use `%s` placeholders rather
than f-strings, raise instead of logging for anything that should stop the release,
and never log headers or tokens.

## Tests

Tests use recorded fixtures and fakes; nothing hits the network. When behaviour
depends on an external format (the release-drafter template, the pom layout, the
Docker Hub manifest), update the fixture in `tests/` rather than loosening the
assertion.

## Workflows

- `release.yml` — the release, weekly or dispatched by hand. One job per stage; each
  stage checks whether its work is already done, so **Re-run failed jobs** resumes a
  broken release.
- `promote.yml` — scans for pre-releases due for promotion; given a `version`, flips
  it to official and opens the downstream bump PRs.
- `ci.yml` — ruff and pytest.

## Repository setup

A GitHub App installed on this repo and on every repo in `config.toml` `[repos]`,
with repository permissions contents, pull requests, issues and actions **write**,
and the organisation permission projects **write**.

| Kind | Name |
| --- | --- |
| Secrets | `APP_ID`, `APP_PRIVATE_KEY` |
| Variable | `RELEASE_ASSIGNEE` |
| Labels | `release`, `succeeded`, `failed`, `promotion` |

`cbioportal` and `cbioportal-frontend` each need the `[changelog]` catch-all label as a
release-drafter category, and `workflow_dispatch` on the release-drafter workflow:
stage 1 labels unlabelled PRs and re-runs release-drafter itself.

The `cbioportal` `master` ruleset must give the App an **Always allow** bypass:
stages 5 and 8 commit the pom straight to `master` (a PR would land in the release
notes), and App permissions alone do not bypass the ruleset.

## config.toml

Everything environment-specific lives in `config.toml`. Keep it in step with the
repos it describes: `[docker]` must match the tags and platforms
`cbioportal/.github/workflows/dockerimage.yml` publishes, and `[maven]` artifact ids
must match each `pom.xml`. `[required_checks]` is an allowlist of checks that must be
green before a release starts — add a name only if it should genuinely block one.
