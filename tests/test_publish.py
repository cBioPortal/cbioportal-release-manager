import pytest

from release import publish

REPO = "cBioPortal/cbioportal-frontend"
WORKFLOW = "maven-package-build.yml"
TAG = "v7.0.6"


def run(status="completed", conclusion="success", event="release"):
    return {
        "status": status,
        "conclusion": conclusion,
        "event": event,
        "html_url": "https://github.com/run/1",
    }


class FakeGitHub:
    def __init__(self, runs=None, appear_after=0):
        self._runs = runs or []
        self.appear_after = appear_after
        self.polls = 0
        self.dispatched = []

    def workflow_runs(self, repo, workflow, **params):
        self.polls += 1
        # Model GitHub taking a moment to register the release-triggered run.
        return self._runs if self.polls > self.appear_after else []

    def dispatch_workflow(self, repo, workflow, ref, inputs):
        self.dispatched.append((repo, workflow, ref, inputs))
        return {}


def ensure(gh, dry_run=False, grace=60):
    publish.ensure_publishing(gh, REPO, WORKFLOW, TAG, dry_run,
                              grace_seconds=grace, sleep=lambda _: None)


def test_does_not_dispatch_while_a_run_is_in_flight():
    # Publishing the release already started the workflow. Dispatching here would
    # run a second concurrent `mvn deploy` of the same GAV -- the race that broke
    # v7.0.5.
    gh = FakeGitHub([run(status="in_progress", conclusion=None)])
    ensure(gh)
    assert gh.dispatched == []


@pytest.mark.parametrize("status", publish.ACTIVE)
def test_treats_every_unfinished_status_as_in_flight(status):
    gh = FakeGitHub([run(status=status, conclusion=None)])
    ensure(gh)
    assert gh.dispatched == []


def test_does_not_dispatch_when_a_run_already_succeeded():
    # The jar can be published while Maven Central has not propagated it yet.
    gh = FakeGitHub([run()])
    ensure(gh)
    assert gh.dispatched == []


def test_waits_for_the_release_trigger_before_dispatching():
    # The job starts seconds after the release is created, so "no run yet" must not
    # be read as "no run coming".
    gh = FakeGitHub([run(status="queued", conclusion=None)], appear_after=2)
    ensure(gh)
    assert gh.dispatched == []
    assert gh.polls == 3


def test_dispatches_when_the_trigger_never_fired():
    gh = FakeGitHub([])
    ensure(gh, grace=30)
    assert len(gh.dispatched) == 1
    assert gh.dispatched[0] == (REPO, WORKFLOW, TAG, {"source_ref": TAG})


def test_dispatches_a_retry_when_every_run_failed():
    gh = FakeGitHub([run(conclusion="failure"), run(conclusion="cancelled")])
    ensure(gh)
    assert len(gh.dispatched) == 1


def test_a_single_success_among_failures_still_blocks_dispatch():
    gh = FakeGitHub([run(conclusion="failure"), run(conclusion="success")])
    ensure(gh)
    assert gh.dispatched == []


def test_retry_does_not_wait_out_the_grace_period():
    # Failed runs exist, so there is nothing to wait for.
    gh = FakeGitHub([run(conclusion="failure")])
    ensure(gh)
    assert gh.polls == 1


def test_dry_run_dispatches_nothing():
    gh = FakeGitHub([])
    ensure(gh, dry_run=True, grace=30)
    assert gh.dispatched == []


def test_dispatch_failure_is_not_fatal():
    # The Central poll is what decides success; a refused dispatch only gets logged.
    class Refusing(FakeGitHub):
        def dispatch_workflow(self, *a, **k):
            from release.gh import GitHubError

            raise GitHubError("403 workflow_dispatch not permitted")

    ensure(Refusing([]), grace=30)
