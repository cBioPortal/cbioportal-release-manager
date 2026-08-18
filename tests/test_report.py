import pytest

from release import publish, report

from .conftest import SPRINT, TODO, BoardStub

DIGEST = "sha256:c0d11a53c9831cfc919509e2ceb571412fabd093aa378daaeb8ae995f022d185"


class FakeGitHub(BoardStub):
    def __init__(self, released=True, pom_version="v7.0.7-SNAPSHOT", frontend_pin="v7.0.6"):
        super().__init__()
        self.released = released
        self.pom_version = pom_version
        self.frontend_pin = frontend_pin
        self.issues = []
        self.updates = []
        self.comments = []

    def release_by_tag(self, repo, tag):
        if not self.released:
            return None
        return {"html_url": f"https://github.com/{repo}/releases/tag/{tag}", "id": 1}

    def file_contents(self, repo, path, ref="HEAD"):
        return (
            "<project><parent><version>3.5.6</version></parent>"
            f"<version>{self.pom_version}</version>"
            f"<frontend.version>{self.frontend_pin}</frontend.version></project>"
        )

    def find_issue(self, repo, title):
        return next((i for i in self.issues if i["title"] == title), None)

    def create_issue(self, repo, title, body, labels, assignees):
        issue = {
            "number": len(self.issues) + 1,
            "node_id": "I_issue",
            "title": title,
            "body": body,
            "labels": labels,
            "assignees": [{"login": a} for a in assignees],
            "html_url": f"https://github.com/{repo}/issues/1",
        }
        self.issues.append(issue)
        return issue

    def update_issue(self, repo, number, **fields):
        self.updates.append((number, fields))
        return {}

    def comment(self, repo, number, body):
        self.comments.append((number, body))
        return {}


@pytest.fixture
def happy(monkeypatch):
    monkeypatch.setattr(publish, "on_central", lambda *a, **k: True)
    monkeypatch.setattr(
        report.publish, "docker_manifest",
        lambda image, tag, session=None: (DIGEST, ["linux/amd64", "linux/arm64"]),
    )


ALL_GREEN = {
    "plan": "success",
    "notes": "success",
    "frontend-release": "success",
    "frontend-central": "success",
    "backend-pom": "success",
    "backend-release": "success",
    "backend-artifacts": "success",
    "snapshot-bump": "success",
}


def test_success_report_carries_the_deploy_digest(happy, config, plan_v706):
    title, body, ok = report.build(
        FakeGitHub(), config, plan_v706, ALL_GREEN, "https://run"
    )
    assert ok
    assert title == "Release v7.0.6"
    assert DIGEST in body
    assert "cbioportal/cbioportal:v7.0.6-web-shenandoah" in body
    assert "linux/amd64, linux/arm64" in body
    assert "Next: deploy this" in body


def test_skipped_stages_still_count_as_success(happy, config, plan_v706):
    results = {**ALL_GREEN, "snapshot-bump": "skipped"}
    _, _, ok = report.build(FakeGitHub(), config, plan_v706, results, "https://run")
    assert ok


def test_failure_report_names_the_stage_and_the_state(monkeypatch, config, plan_v706):
    monkeypatch.setattr(publish, "on_central", lambda *a, **k: False)
    monkeypatch.setattr(
        report.publish, "docker_manifest", lambda image, tag, session=None: None
    )
    results = {**ALL_GREEN, "backend-pom": "failure", "backend-release": "skipped"}
    title, body, ok = report.build(
        FakeGitHub(released=False, pom_version="v7.0.6-SNAPSHOT", frontend_pin="v7.0.5"),
        config, plan_v706, results, "https://run",
    )
    assert not ok
    assert title == "Release v7.0.6"
    assert "`backend-pom`" in body
    assert "not created" in body
    assert "Do not deploy" in body


def test_failure_report_survives_a_registry_error(monkeypatch, config, plan_v706):
    monkeypatch.setattr(publish, "on_central", lambda *a, **k: True)

    def boom(image, tag, session=None):
        raise RuntimeError("registry unreachable")

    monkeypatch.setattr(report.publish, "docker_manifest", boom)
    _, body, _ = report.build(
        FakeGitHub(), config, plan_v706, {**ALL_GREEN, "backend-artifacts": "failure"},
        "https://run",
    )
    assert "registry unreachable" in body


def test_issue_is_created_once_and_updated_on_rerun(happy, config, monkeypatch):
    monkeypatch.setenv("RELEASE_ASSIGNEE", "zainasir")
    gh = FakeGitHub()
    report.file_issue(gh, config, "Release v7.0.6", "body", True, dry_run=False)
    assert len(gh.issues) == 1
    assert gh.issues[0]["assignees"] == [{"login": "zainasir"}]

    report.file_issue(gh, config, "Release v7.0.6", "new body", True, dry_run=False)
    assert len(gh.issues) == 1
    assert gh.updates and gh.updates[0][1]["body"] == "new body"
    assert gh.comments


def test_dry_run_files_nothing(happy, config):
    gh = FakeGitHub()
    report.file_issue(gh, config, "Release v7.0.6", "body", True, dry_run=True)
    assert gh.issues == []
    assert gh.project_items == []


def test_issue_lands_on_the_board_in_todo_and_the_current_sprint(happy, config, monkeypatch):
    monkeypatch.setenv("RELEASE_ASSIGNEE", "zainasir")
    gh = FakeGitHub()
    report.file_issue(gh, config, "Release v7.0.6", "body", True, dry_run=False)
    assert gh.project_items == ["I_issue"]
    assert gh.field_values == [TODO, SPRINT]

    # A re-run must re-assert both, not just leave the item where it was.
    report.file_issue(gh, config, "Release v7.0.6", "body", True, dry_run=False)
    assert gh.project_items == ["I_issue", "I_issue"]
    assert len(gh.field_values) == 4


def test_a_broken_board_does_not_fail_the_hand_off(happy, config, caplog):
    class NoProject(FakeGitHub):
        def graphql(self, query, **variables):
            raise RuntimeError("Resource not accessible by integration")

    gh = NoProject()
    report.file_issue(gh, config, "Release v7.0.6", "body", True, dry_run=False)
    assert len(gh.issues) == 1
    assert "could not add" in caplog.text and "to project cBioPortal/19" in caplog.text
