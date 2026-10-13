from release import plan

from .conftest import BACKEND_TITLES, FRONTEND_TITLES


def test_uncategorised_prs_finds_the_changes_bucket(backend_draft):
    # Release-Procedure.md tells you to eyeball the `## Changes` section for PRs
    # that were never labelled. This is that check.
    assert plan.uncategorised_prs(backend_draft, BACKEND_TITLES) == [12345]


def test_uncategorised_prs_ignores_labelled_prs(frontend_draft):
    assert plan.uncategorised_prs(frontend_draft, FRONTEND_TITLES) == []


def test_uncategorised_prs_catches_bullets_before_any_heading():
    body = "- Straight into the void @dev (#999)\n\n## 🧬 Features\n- Fine @dev (#1)\n"
    assert plan.uncategorised_prs(body, BACKEND_TITLES) == [999]


def test_uncategorised_prs_ignores_link_bullets_in_the_boilerplate(backend_draft):
    # The `Full commit logs` section is bullets too, but carries no (#NNN).
    loose = plan.uncategorised_prs(backend_draft, BACKEND_TITLES)
    assert 12287 not in loose


def test_count_prs(backend_draft, frontend_draft):
    assert plan.count_prs(backend_draft) == 4
    assert plan.count_prs(frontend_draft) == 3


def test_preflight_collects_errors_without_raising():
    pre = plan.Preflight()
    pre.check(True, "fine")
    pre.check(False, "broken")
    pre.warn("odd")
    assert pre.errors == ["broken"]
    assert pre.warnings == ["odd"]


def test_render_includes_warnings(plan_v706):
    plan_v706["warnings"] = ["backend pom is at v7.0.9-SNAPSHOT"]
    rendered = plan.render(plan_v706)
    assert "v7.0.5` -> `v7.0.6" in rendered
    assert "backend pom is at v7.0.9-SNAPSHOT" in rendered


def test_render_omits_the_warning_section_when_clean(plan_v706):
    assert "Warnings" not in plan.render(plan_v706)


def test_published_releases_excludes_drafts():
    # release-drafter names its draft with `tag-template: v$NEXT_PATCH_VERSION`, so
    # the backend's open draft is literally tagged v7.0.6 while v7.0.5 is the newest
    # release that exists. Counting the draft makes the repos look out of lockstep
    # and the next version look already taken.
    from release import version as ver
    from release.gh import GitHub

    gh = GitHub(token="x")
    gh.releases = lambda repo: [
        {"tag_name": "v7.0.6", "draft": True},
        {"tag_name": "v7.0.5", "draft": False},
        {"tag_name": "v7.0.4", "draft": False},
    ]
    tags = [r["tag_name"] for r in gh.published_releases("any/repo")]
    assert tags == ["v7.0.5", "v7.0.4"]
    assert ver.latest(tags) == "v7.0.5"


def test_release_by_tag_ignores_a_draft_holding_the_tag():
    from release.gh import GitHub

    gh = GitHub(token="x")
    gh.get = lambda path, **kw: {"tag_name": "v7.0.6", "draft": True}
    assert gh.release_by_tag("any/repo", "v7.0.6") is None

    gh.get = lambda path, **kw: {"tag_name": "v7.0.5", "draft": False}
    assert gh.release_by_tag("any/repo", "v7.0.5")["tag_name"] == "v7.0.5"


def test_roundtrip(tmp_path, plan_v706):
    path = tmp_path / "plan.json"
    plan.write(plan_v706, path)
    assert plan.read(path) == plan_v706


LOOSE = "## Changes\n- Unlabelled @dev (#7)\n- Also unlabelled @dev (#8)\n"
REFILED = "## 🗂 Miscellaneous\n- Unlabelled @dev (#7)\n- Also unlabelled @dev (#8)\n"
TITLES = ["🗂 Miscellaneous"]


class DrafterStub:
    """Serves one draft body per poll and records the labels and dispatches."""

    def __init__(self, *bodies):
        self.bodies = list(bodies)
        self.labels = []
        self.dispatches = []

    def add_labels(self, repo, number, labels):
        self.labels.append((repo, number, labels))

    def dispatch_workflow(self, repo, workflow, ref, inputs):
        self.dispatches.append((repo, workflow, ref))

    def draft_release(self, repo):
        return {"body": self.bodies.pop(0) if len(self.bodies) > 1 else self.bodies[0]}


def file_uncategorised(gh, config, dry_run=False):
    return plan.file_uncategorised(gh, config, "o/backend", "master", TITLES, [7, 8],
                                   dry_run, sleep=lambda _: None)


def test_file_uncategorised_labels_dispatches_and_returns_the_refiled_draft(config):
    gh = DrafterStub(REFILED)
    assert file_uncategorised(gh, config) == REFILED
    assert gh.labels == [("o/backend", 7, ["cl-misc"]), ("o/backend", 8, ["cl-misc"])]
    assert gh.dispatches == [("o/backend", "release-management.yml", "master")]


def test_file_uncategorised_polls_until_the_draft_re_renders(config):
    gh = DrafterStub(LOOSE, LOOSE, REFILED)
    assert file_uncategorised(gh, config) == REFILED
    assert gh.bodies == [REFILED]
    assert len(gh.dispatches) == 1


def test_file_uncategorised_gives_up_with_the_still_loose_draft(config):
    gh = DrafterStub(LOOSE)
    polls = []
    result = plan.file_uncategorised(gh, config, "o/backend", "master", TITLES, [7, 8],
                                     False, sleep=polls.append)
    assert plan.uncategorised_prs(result, TITLES) == [7, 8]
    assert len(polls) == config["timeouts"]["drafter_attempts"]


def test_file_uncategorised_writes_nothing_on_a_dry_run(config):
    gh = DrafterStub(LOOSE)
    assert file_uncategorised(gh, config, dry_run=True) is None
    assert gh.labels == []
    assert gh.dispatches == []
