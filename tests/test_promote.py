from datetime import UTC, datetime

import pytest

from release import promote

from .conftest import SPRINT, TODO, BoardStub

NOW = datetime(2026, 8, 12, tzinfo=UTC)

# The real state of cBioPortal/cbioportal on 2026-08-12: three pre-releases
# outstanding, the oldest overdue by weeks.
RELEASES = [
    {"tag_name": "v7.0.5", "draft": False, "prerelease": True,
     "published_at": "2026-07-09T12:03:03Z", "html_url": "u/v7.0.5"},
    {"tag_name": "v7.0.4", "draft": False, "prerelease": True,
     "published_at": "2026-07-01T02:38:02Z", "html_url": "u/v7.0.4"},
    {"tag_name": "v7.0.3", "draft": False, "prerelease": True,
     "published_at": "2026-06-18T15:43:02Z", "html_url": "u/v7.0.3"},
    {"tag_name": "v7.0.2", "draft": False, "prerelease": False,
     "published_at": "2026-06-10T15:17:11Z", "html_url": "u/v7.0.2"},
    {"tag_name": "v7.1.0-rc.1", "draft": False, "prerelease": True,
     "published_at": "2026-01-01T00:00:00Z", "html_url": "u/rc"},
    {"tag_name": "v9.9.9", "draft": True, "prerelease": True,
     "published_at": "2026-01-01T00:00:00Z", "html_url": "u/draft"},
]

COMPOSE_ENV = """# Docker images
DOCKER_IMAGE_CBIOPORTAL=cbioportal/cbioportal:7.0.2
DOCKER_IMAGE_SESSION_SERVICE=cbioportal/session-service:0.6.4
"""

CHART_YAML = """apiVersion: v2
name: cbioportal
version: 1.1.0
appVersion: "6.4.1"
"""

VALUES_YAML = """container:
  image: cbioportal/cbioportal:6.4.1-web-shenandoah
  cmd: "java"
"""


def official(tag: str) -> list[dict]:
    """RELEASES with `tag` flipped official -- i.e. the world after a promotion."""
    return [{**r, "prerelease": False} if r["tag_name"] == tag else r for r in RELEASES]


class FakeGitHub(BoardStub):
    def __init__(self, releases=RELEASES):
        super().__init__()
        self._releases = releases
        self.files = {
            ".env": COMPOSE_ENV,
            "charts/cbioportal/Chart.yaml": CHART_YAML,
            "charts/cbioportal/values.yaml": VALUES_YAML,
        }
        self.written = {}
        self.pulls = []
        self.refs = set()
        self.updated_releases = []
        self.issues = []
        self.assigned = []

    def releases(self, repo):
        return self._releases

    def release_by_tag(self, repo, tag):
        found = next((r for r in self._releases if r["tag_name"] == tag), None)
        return {**found, "id": 7} if found else None

    def update_release(self, repo, release_id, **fields):
        self.updated_releases.append((repo, fields))
        return {}

    def file_contents(self, repo, path, ref="HEAD"):
        return self.files.get(path)

    def branch_sha(self, repo, base):
        return "deadbeef"

    def ref_sha(self, repo, ref):
        return "deadbeef" if ref in self.refs else None

    def create_ref(self, repo, ref, sha):
        self.refs.add(ref.replace("refs/", ""))
        return {}

    def put_file(self, repo, path, content, message, branch):
        self.written[(repo, path)] = content
        return {}

    def find_pull(self, repo, branch, base):
        return None

    def create_pull(self, repo, head, base, title, body):
        pull = {
            "repo": repo,
            "title": title,
            "number": 42,
            "node_id": f"PR_{repo}",
            "html_url": f"https://github.com/{repo}/pull/1",
        }
        self.pulls.append(pull)
        return pull

    def find_issue(self, repo, title):
        return next((i for i in self.issues if i["title"] == title), None)

    def create_issue(self, repo, title, body, labels, assignees):
        issue = {
            "number": len(self.issues) + 1,
            "node_id": "I_promotion",
            "title": title,
            "body": body,
            "assignees": [{"login": a} for a in assignees],
            "html_url": f"https://github.com/{repo}/issues/1",
        }
        self.issues.append(issue)
        return issue

    def update_issue(self, repo, number, **fields):
        self.assigned.append((repo, number, fields.get("assignees")))
        return {}


def test_due_finds_overdue_prereleases(config):
    due = promote.due(FakeGitHub(), config, now=NOW)
    tags = [d["tag"] for d in due]
    # All three outstanding pre-releases are past the 30-day window as of NOW.
    assert tags == ["v7.0.3", "v7.0.4", "v7.0.5"]


def test_due_excludes_official_drafts_and_rcs(config):
    tags = [d["tag"] for d in promote.due(FakeGitHub(), config, now=NOW)]
    assert "v7.0.2" not in tags  # already official
    assert "v9.9.9" not in tags  # still a draft
    assert "v7.1.0-rc.1" not in tags  # not a vX.Y.Z release tag


def test_due_excludes_prereleases_inside_the_window(config):
    # v7.0.5 is 34 days old at NOW... check the boundary explicitly instead.
    due = promote.due(FakeGitHub(), config, now=datetime(2026, 7, 20, tzinfo=UTC))
    assert [d["tag"] for d in due] == ["v7.0.3"]


def test_due_ignores_prereleases_behind_the_official_release(config):
    # v7.0.4 official: v7.0.3 is behind it and will never be promoted now.
    tags = [d["tag"] for d in promote.due(FakeGitHub(official("v7.0.4")), config, now=NOW)]
    assert tags == ["v7.0.5"]


def test_promoting_the_newest_clears_the_backlog(config):
    # The wrinkle: promoting v7.0.5 must leave nothing outstanding behind it,
    # rather than re-flagging v7.0.3 and v7.0.4 on the next scan.
    assert promote.due(FakeGitHub(official("v7.0.5")), config, now=NOW) == []


def test_due_reports_age(config):
    oldest = promote.due(FakeGitHub(), config, now=NOW)[0]
    assert oldest["age_days"] == 54


def test_flip_marks_both_repos_official(config):
    gh = FakeGitHub()
    promote.flip(gh, config, "v7.0.3", dry_run=False)
    assert len(gh.updated_releases) == 2
    assert all(fields["prerelease"] is False for _, fields in gh.updated_releases)


def test_flip_skips_an_already_official_release(config):
    gh = FakeGitHub()
    promote.flip(gh, config, "v7.0.2", dry_run=False)
    assert gh.updated_releases == []


def test_flip_refuses_to_promote_behind_the_official_release(config):
    # v7.0.4 official, so promoting v7.0.3 would drag Latest backwards.
    gh = FakeGitHub(official("v7.0.4"))
    with pytest.raises(promote.PromotionError, match="behind the current official"):
        promote.flip(gh, config, "v7.0.3", dry_run=False)
    assert gh.updated_releases == []


def test_flip_refuses_before_a_dry_run_too(config):
    gh = FakeGitHub(official("v7.0.4"))
    with pytest.raises(promote.PromotionError):
        promote.flip(gh, config, "v7.0.3", dry_run=True)


def test_re_promoting_the_current_official_stays_a_no_op(config):
    # The resume path: bump_downstream failed, so the same version is dispatched
    # again. The guard must not turn that into a hard failure.
    gh = FakeGitHub(official("v7.0.5"))
    promote.flip(gh, config, "v7.0.5", dry_run=False)
    assert gh.updated_releases == []


def test_bump_downstream_rewrites_all_three_files(config):
    gh = FakeGitHub()
    promote.bump_downstream(gh, config, "v7.0.6", dry_run=False)
    written = {path: content for (_, path), content in gh.written.items()}
    assert "cbioportal/cbioportal:7.0.6" in written[".env"]
    assert 'appVersion: "7.0.6"' in written["charts/cbioportal/Chart.yaml"]
    assert (
        "image: cbioportal/cbioportal:7.0.6-web-shenandoah"
        in written["charts/cbioportal/values.yaml"]
    )


def test_bump_downstream_strips_the_v_prefix(config):
    gh = FakeGitHub()
    promote.bump_downstream(gh, config, "v7.0.6", dry_run=False)
    assert "cbioportal:v7.0.6" not in gh.written[(config["repos"]["compose"], ".env")]


def test_bump_downstream_opens_one_pr_per_repo(config):
    gh = FakeGitHub()
    promote.bump_downstream(gh, config, "v7.0.6", dry_run=False)
    assert len(gh.pulls) == 2


def test_bump_downstream_is_a_noop_when_already_current(config):
    gh = FakeGitHub()
    promote.bump_downstream(gh, config, "v7.0.2", dry_run=False)
    # .env is already on 7.0.2, so only the helm repo should move.
    assert [p["repo"] for p in gh.pulls] == [config["repos"]["helm"]]


def test_bump_downstream_fails_loudly_if_a_pattern_stops_matching(config):
    gh = FakeGitHub()
    gh.files[".env"] = "# someone restructured this file\n"
    with pytest.raises(promote.PromotionError, match="matched nothing"):
        promote.bump_downstream(gh, config, "v7.0.6", dry_run=False)


def test_bump_downstream_writes_nothing_on_a_dry_run(config):
    gh = FakeGitHub()
    promote.bump_downstream(gh, config, "v7.0.6", dry_run=True)
    assert gh.written == {} and gh.pulls == []
    assert gh.assigned == [] and gh.project_items == []


def test_the_promotion_issue_lands_on_the_board(config, monkeypatch):
    monkeypatch.setenv("RELEASE_ASSIGNEE", "zainasir")
    gh = FakeGitHub()
    promote.scan(gh, config, dry_run=False)
    assert gh.issues[0]["assignees"] == [{"login": "zainasir"}]
    assert gh.project_items == ["I_promotion"]
    assert gh.field_values == [TODO, SPRINT]


def test_bump_prs_are_assigned_and_land_on_the_board(config, monkeypatch):
    monkeypatch.setenv("RELEASE_ASSIGNEE", "zainasir")
    gh = FakeGitHub()
    promote.bump_downstream(gh, config, "v7.0.6", dry_run=False)

    compose, helm = config["repos"]["compose"], config["repos"]["helm"]
    assert gh.assigned == [
        (compose, 42, ["zainasir"]),
        (helm, 42, ["zainasir"]),
    ]
    # These are the PRs that historically rot unmerged, so both must be visible.
    assert gh.project_items == [f"PR_{compose}", f"PR_{helm}"]
    assert gh.field_values == [TODO, SPRINT, TODO, SPRINT]


def test_bump_prs_are_left_unassigned_without_a_configured_assignee(config, monkeypatch):
    monkeypatch.delenv("RELEASE_ASSIGNEE", raising=False)
    gh = FakeGitHub()
    promote.bump_downstream(gh, config, "v7.0.6", dry_run=False)
    assert gh.pulls and gh.assigned == []
