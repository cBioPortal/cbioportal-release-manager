"""Logging setup. Configured once by the CLI; every other module just calls
`logging.getLogger(__name__)`.

Diagnostics go to stderr so stdout stays the deliverable -- `release notes > notes.md`
should give you the notes, not the notes plus a log.

Under GitHub Actions the formatter emits workflow commands, so warnings and errors
are annotated on the run summary page and each stage collapses into a group. Locally
the same records render as plain timestamped lines.
"""

from __future__ import annotations

import logging
import os
import sys
from contextlib import contextmanager

ROOT = "release"


def in_actions() -> bool:
    return os.environ.get("GITHUB_ACTIONS") == "true"


class ActionsFormatter(logging.Formatter):
    """Prefix records with the workflow command matching their level.

    Workflow commands are single-line, so embedded newlines have to be escaped or
    everything after the first one silently loses its annotation.
    """

    PREFIX = {
        logging.CRITICAL: "::error::",
        logging.ERROR: "::error::",
        logging.WARNING: "::warning::",
        logging.DEBUG: "::debug::",
    }

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        prefix = self.PREFIX.get(record.levelno)
        if not prefix:
            return message
        return prefix + message.replace("\n", "%0A")


def setup(verbose: bool = False, quiet: bool = False) -> None:
    # Actions sets RUNNER_DEBUG when someone enables step debug logging on a re-run,
    # which is exactly the moment they want the HTTP trace.
    if os.environ.get("RUNNER_DEBUG") == "1":
        verbose = True

    if verbose:
        level = logging.DEBUG
    elif quiet:
        level = logging.WARNING
    else:
        level = logging.INFO

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        ActionsFormatter("%(message)s")
        if in_actions()
        else logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    )

    logger = logging.getLogger(ROOT)
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False

    # urllib3 logs every connection at DEBUG; only worth it when tracing.
    logging.getLogger("urllib3").setLevel(logging.DEBUG if verbose else logging.WARNING)


@contextmanager
def group(title: str):
    """A collapsible section in the Actions log; a plain heading everywhere else."""
    logger = logging.getLogger(ROOT)
    if in_actions():
        print(f"::group::{title}", file=sys.stderr, flush=True)
        try:
            yield
        finally:
            print("::endgroup::", file=sys.stderr, flush=True)
    else:
        logger.info("%s", title)
        yield
