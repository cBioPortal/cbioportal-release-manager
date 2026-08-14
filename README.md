# cBioPortal Release Manager

Cuts the weekly release across `cbioportal` and `cbioportal-frontend`, up to a
published and verified Docker image, then files an issue saying what to deploy.
Deployment itself is not automated.

## Local development

```bash
uv sync
uv run pytest
uv run ruff check .
uv run release --help
```

Requires `uv` and Python 3.11+. Java 21 and Maven are only needed for the
`bump-pom` and `bump-snapshot` commands.

To run anything against the real repos you need a token that can see draft
releases:

```bash
export GITHUB_TOKEN=$(gh auth token)
uv run release plan --plan /tmp/plan.json     # read-only
uv run release notes --plan /tmp/plan.json    # read-only
```

Every command that writes takes `--dry-run`, which performs all the reads and
prints the intended writes instead of making them.

## Environment variables

| Variable | Used by | Purpose |
| --- | --- | --- |
| `GITHUB_TOKEN` | all | API auth. Must be able to read draft releases. |
| `RELEASE_ASSIGNEE` | `report`, `promote-scan` | GitHub handle to assign issues to. Unset means unassigned. |
| `GITHUB_STEP_SUMMARY` | `plan`, `notes`, `report` | Set by Actions. Appended to when present. |
| `GITHUB_ACTIONS` | logging | Set by Actions. Switches the log formatter to workflow commands. |
| `RUNNER_DEBUG` | logging | Set by Actions when a run is re-run with debug logging. Forces `-v`. |

## Logging

Stdlib `logging`, configured once in `cli.py`. Modules just call
`logging.getLogger(__name__)` — nothing takes a logger or a callback as an argument.

**Streams are split by purpose.** Diagnostics go to stderr, the deliverable goes to
stdout, so `release notes > notes.md` gives you the notes and nothing else.

| Flag | Level | Shows |
| --- | --- | --- |
| `-q` | WARNING | Only things that need attention |
| *(default)* | INFO | One line per step |
| `-v` | DEBUG | Adds every HTTP call with status and timing |

Both flags go before the subcommand: `release -v plan`.

Under Actions the formatter emits workflow commands instead of timestamps:
`WARNING` and `ERROR` become `::warning::`/`::error::`, which annotates them on the
run summary page, and each stage is wrapped in a collapsible `::group::`. Multi-line
records are escaped to `%0A`, since a workflow command that contains a raw newline
loses everything after it.

When adding log lines:

- Use `%s` placeholders (`logger.info("target is %s", target)`), not f-strings — the
  formatting is skipped entirely when the level is disabled.
- `logger.warning` for anything a human should notice but that does not stop the
  release; raise for anything that does. Errors are logged once, at the top level in
  `main()`, so exceptions should carry the message rather than log it themselves.
- Never log headers or tokens. `gh.py` traces method, URL, status and duration only.

## Workflows

| Workflow | Trigger | Does |
| --- | --- | --- |
| `release.yml` | manual | The release. Inputs: `bump`, `dry_run`. One job per stage. |
| `promote.yml` | nightly 13:17 UTC, manual | Scans for overdue pre-releases. With a `version` input, performs the promotion. |
| `rehearse.yml` | Mondays 12:23 UTC, manual | Read-only `plan` + `notes`, to catch drift before release day. |
| `ci.yml` | PR, push to `main` | ruff and pytest. |

`release.yml` uses `concurrency: release`, so two releases cannot overlap. Stage 3
sits behind the `release-approval` environment.

### Running a release

Dispatch **Release**. Leave `bump` on `patch` unless the release needs a database
migration or breaks something. When it finishes, `Release vX.Y.Z` is filed in this
repo with the image tag and `sha256` digest; close it once deployed.

## Repository setup

A GitHub App installed on `cbioportal`, `cbioportal-frontend`, `cbioportal-helm`,
`cbioportal-docker-compose` and this repo, with permissions: contents **write**,
pull requests **write**, issues **write**, actions **write**, metadata **read**.

| Kind | Name | Value |
| --- | --- | --- |
| Secret | `APP_ID` | the App's ID |
| Secret | `APP_PRIVATE_KEY` | the App's private key |
| Variable | `RELEASE_ASSIGNEE` | GitHub handle for release issues |
| Environment | `release-approval` | with required reviewers |
| Labels | `release`, `succeeded`, `failed`, `promotion` | in this repo |

## Maintaining config.toml

Everything environment-specific lives there. The parts most likely to need
updating:

**`[required_checks]`** — an allowlist of checks that must be green before a
release starts (stage 1, on the pinned SHA) and before a pom PR is merged
(stages 5 and 8), covering both GitHub check-runs and CircleCI commit statuses.

Both lists are currently **empty, so CI does not gate a release at all**; stages 1,
5 and 8 each log a warning saying so. That is deliberate — the combined status of
both masters is normally `failure` (sonarcloud, GitBook, Dependabot,
`e2e_localdb`), so there was no dependable gate to enforce. It is an allowlist,
never "everything green": add a name only if it should genuinely stop a release.
To see the current names on a commit:

```bash
gh api repos/cBioPortal/cbioportal/commits/master/check-runs --jq '.check_runs[].name'
gh api repos/cBioPortal/cbioportal/commits/master/status     --jq '.statuses[].context'
```

**`[docker] tag_suffixes` and `platforms`** — must match what
`cbioportal/.github/workflows/dockerimage.yml` publishes. If a new image variant
or architecture is added there, add it here or the release will not wait for it.

**`[maven]` artifact ids** — must match the `artifactId` in each repo's `pom.xml`.

**`[timeouts]`** — `maven_attempts` × `maven_interval_seconds` is the publish
wait. Maven Central routinely takes 20 minutes.

**`[promotion] age_days`** — how long a pre-release must run before `promote-scan`
flags it.

## Testing

Tests use recorded fixtures and hand-written fakes; nothing hits the network.
`tests/conftest.py` holds draft bodies shaped like the real release-drafter output.

When changing behaviour that depends on an external format — the drafter template,
the pom layout, the Docker Hub manifest shape — update the corresponding fixture
in `tests/` rather than loosening the assertion.

## Known constraints

- `bump-pom` and `bump-snapshot` shell out to `mvn versions:set`, so their jobs
  need `setup-java`. Never edit the pom with `sed`: a pattern that stops matching
  is indistinguishable from a pattern with nothing to do.
- `cbioportal-frontend`'s `maven-package-build.yml` currently triggers on both
  `release: published` and `push: tags: '*'`, so it runs twice per release and the
  two runs race on Maven Central. Its `update-backend` job also pushes directly to
  backend `master`, which conflicts with `bump-pom`. Both need fixing in that repo
  before a real release is run from here.
