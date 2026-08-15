"""Command line entry point. One subcommand per pipeline stage.

The workflow is deliberately thin glue over these: anything with logic in it should
be runnable on a laptop with --dry-run, not only by cutting a real release.
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from . import config as config_module
from . import logs, notes, plan, promote, publish, report
from .gh import GitHub, GitHubError
from .pom import PomError, bump_frontend, bump_snapshot, read_values

PUBLISH_WORKFLOW = "maven-package-build.yml"


def _gh() -> GitHub:
    return GitHub()


def cmd_plan(args, config) -> int:
    with logs.group("preflight"):
        built = plan.build(_gh(), config, bump=args.bump)
    plan.write(built, args.plan)
    summary = plan.render(built)
    print(summary)
    report.write_summary(summary)
    return 0


def cmd_notes(args, config) -> int:
    current = plan.read(args.plan)
    with logs.group("release notes"):
        bodies = notes.build(_gh(), config, current)
    Path(args.out).mkdir(parents=True, exist_ok=True)
    for key, body in bodies.items():
        (Path(args.out) / f"{key}.md").write_text(body)
    print(f"--- backend notes ({current['version']}) ---\n")
    print(bodies["backend"])
    report.write_summary(
        f"## Release notes preview\n\n<details><summary>{current['version']}"
        f"</summary>\n\n{bodies['backend']}\n\n</details>"
    )
    return 0


def cmd_release_frontend(args, config) -> int:
    gh = _gh()
    current = plan.read(args.plan)
    with logs.group(f"cut frontend {current['version']}"):
        bodies = notes.build(gh, config, current)
        publish.cut_release(
            gh, config["repos"]["frontend"], current["version"],
            current["frontend"]["sha"], bodies["frontend"], args.dry_run,
        )
    return 0


def cmd_await_frontend(args, config) -> int:
    gh = _gh()
    current = plan.read(args.plan)
    version = current["version"]
    artifact = config["maven"]["frontend_artifact"]
    with logs.group(f"wait for {artifact} {version} on Maven Central"):
        if publish.on_central(config, artifact, version):
            logging.getLogger(__name__).info(
                "%s %s is already on Maven Central -- skipping", artifact, version
            )
            return 0
        publish.ensure_publishing(
            gh, config["repos"]["frontend"], PUBLISH_WORKFLOW, version, args.dry_run
        )
        if args.dry_run:
            return 0
        publish.await_central(config, artifact, version)
    return 0


def cmd_bump_pom(args, config) -> int:
    current = plan.read(args.plan)
    with logs.group(f"point the backend pom at frontend {current['version']}"):
        bump_frontend(_gh(), config, current, Path(args.workdir), args.dry_run)
    return 0


def cmd_release_backend(args, config) -> int:
    gh = _gh()
    current = plan.read(args.plan)
    version = current["version"]
    repo = config["repos"]["backend"]
    branch = config["branches"]["backend"]

    with logs.group(f"cut backend {version}"):
        head = gh.branch_sha(repo, branch)
        pom_version, frontend_pin = read_values(Path(args.workdir))
        if (pom_version, frontend_pin) != (version, version):
            message = (
                f"pom at {head[:8]} says version={pom_version}, frontend={frontend_pin}, "
                f"expected {version} for both"
            )
            # On a dry run the previous stage deliberately did not merge, so this
            # mismatch is expected and must not fail the rehearsal.
            if not args.dry_run:
                raise SystemExit(f"refusing to tag {version}: {message}. Stage 5 did not land.")
            logging.getLogger(__name__).warning("[dry-run] %s (stage 5 did not merge)", message)

        bodies = notes.build(gh, config, current)
        publish.cut_release(gh, repo, version, head, bodies["backend"], args.dry_run)
    return 0


def cmd_await_backend(args, config) -> int:
    gh = _gh()
    current = plan.read(args.plan)
    version = current["version"]
    artifact = config["maven"]["backend_artifact"]
    logger = logging.getLogger(__name__)
    with logs.group(f"wait for {artifact} {version} on Maven Central"):
        if not publish.on_central(config, artifact, version):
            publish.ensure_publishing(
                gh, config["repos"]["backend"], PUBLISH_WORKFLOW, version, args.dry_run
            )
            if not args.dry_run:
                publish.await_central(config, artifact, version)
        else:
            logger.info("%s %s is already on Maven Central -- skipping", artifact, version)
    if args.dry_run:
        logger.info("[dry-run] skipping the Docker Hub wait")
        return 0
    with logs.group(f"wait for the {version} image on Docker Hub"):
        publish.await_image(config, version)
    return 0


def cmd_bump_snapshot(args, config) -> int:
    with logs.group("reopen master for development"):
        bump_snapshot(config, plan.read(args.plan), Path(args.workdir), args.dry_run)
    return 0


def cmd_report(args, config) -> int:
    gh = _gh()
    current = plan.read(args.plan)
    results = dict(item.split("=", 1) for item in args.result)
    run_url = args.run_url or os.environ.get("RUN_URL", "")
    with logs.group("hand off"):
        title, body, succeeded = report.build(gh, config, current, results, run_url)
        report.file_issue(gh, config, title, body, succeeded, args.dry_run)
    print(body)
    report.write_summary(f"# {title}\n\n{body}")
    if not succeeded:
        logging.getLogger(__name__).error("%s did not complete; see %s", title, run_url)
    return 0 if succeeded else 1


def cmd_promote_scan(args, config) -> int:
    with logs.group("scan for overdue pre-releases"):
        promote.scan(_gh(), config, args.dry_run)
    return 0


def cmd_promote(args, config) -> int:
    gh = _gh()
    with logs.group(f"promote {args.version} to official"):
        promote.flip(gh, config, args.version, args.dry_run)
        promote.bump_downstream(gh, config, args.version, args.dry_run)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="release", description=__doc__)
    parser.add_argument("--config", default=None, help="path to config.toml")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="debug logging, including every HTTP call")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="warnings and errors only")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add(name, handler, help_text, *, needs_plan=True, needs_workdir=False):
        sub = subparsers.add_parser(name, help=help_text)
        sub.set_defaults(handler=handler)
        sub.add_argument("--dry-run", action="store_true",
                         help="read everything, write nothing; print intended changes")
        if needs_plan:
            sub.add_argument("--plan", default="plan.json")
        if needs_workdir:
            sub.add_argument("--workdir", required=True,
                             help="path to a checkout of the backend repo")
        return sub

    add("plan", cmd_plan, "stage 1: pin, pick the version, verify preconditions") \
        .add_argument("--bump", choices=["patch", "minor", "major"], default="patch")
    add("notes", cmd_notes, "stage 2: preview and validate the combined release notes") \
        .add_argument("--out", default="notes")
    add("release-frontend", cmd_release_frontend, "stage 3: cut the frontend pre-release")
    add("await-frontend", cmd_await_frontend, "stage 4: wait for the frontend on Central")
    add("bump-pom", cmd_bump_pom, "stage 5: point the backend pom at the new frontend",
        needs_workdir=True)
    add("release-backend", cmd_release_backend, "stage 6: cut the backend pre-release",
        needs_workdir=True)
    add("await-backend", cmd_await_backend, "stage 7: wait for Central and the image")
    add("bump-snapshot", cmd_bump_snapshot, "stage 8: reopen master for development",
        needs_workdir=True)
    report_parser = add("report", cmd_report, "stage 9: summarise and file the issue")
    report_parser.add_argument("--result", action="append", default=[],
                               metavar="STAGE=RESULT")
    report_parser.add_argument("--run-url", default="")

    add("promote-scan", cmd_promote_scan, "nightly: flag pre-releases due for promotion",
        needs_plan=False)
    add("promote", cmd_promote, "flip a pre-release to official and bump downstream",
        needs_plan=False).add_argument("--version", required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logs.setup(verbose=args.verbose, quiet=args.quiet)
    config = config_module.load(args.config)
    try:
        return args.handler(args, config)
    except (
        plan.PreflightError,
        publish.PublishError,
        PomError,
        promote.PromotionError,
        GitHubError,
    ) as error:
        # These all mean "the release cannot continue", and the message is written
        # for the person reading the job log. A traceback would only bury it, and
        # logger.error() gets it annotated on the Actions run summary.
        logging.getLogger(__name__).error("%s", error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
