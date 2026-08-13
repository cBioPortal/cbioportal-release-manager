"""Version arithmetic over cBioPortal's `vMAJOR.MINOR.PATCH` tags.

The next version is always the previous release + 1 on the requested component,
defaulting to patch. Nothing is inferred from PR labels: a minor or major release
is a judgement call the operator makes by passing `--bump`.
"""

from __future__ import annotations

import re

TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


def parse(tag: str) -> tuple[int, int, int]:
    match = TAG.match(tag)
    if not match:
        raise ValueError(f"not a release tag: {tag!r}")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def format(parts: tuple[int, int, int]) -> str:
    return "v{}.{}.{}".format(*parts)


def is_release_tag(tag: str) -> bool:
    return bool(TAG.match(tag))


def bump(tag: str, component: str = "patch") -> str:
    major, minor, patch = parse(tag)
    if component == "patch":
        return format((major, minor, patch + 1))
    if component == "minor":
        return format((major, minor + 1, 0))
    if component == "major":
        return format((major + 1, 0, 0))
    raise ValueError(f"unknown bump component: {component!r}")


def latest(tags: list[str]) -> str:
    """Highest release tag, ignoring anything that is not vX.Y.Z (rc's, module tags)."""
    releases = [tag for tag in tags if is_release_tag(tag)]
    if not releases:
        raise ValueError("no release tags found")
    return max(releases, key=parse)


def snapshot(tag: str) -> str:
    """The -SNAPSHOT version master should carry after `tag` is released."""
    return f"{bump(tag, 'patch')}-SNAPSHOT"
