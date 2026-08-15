import pytest

from release import pom

# Trimmed from the real backend pom: a Spring Boot parent whose <version> comes
# first in the file, then the project's own version, then the frontend property.
POM = """<?xml version="1.0" encoding="UTF-8"?>
<project>
  <parent>
    <groupId>org.springframework.boot</groupId>
    <artifactId>spring-boot-starter-parent</artifactId>
    <version>3.5.6</version>
  </parent>
  <groupId>org.cbioportal</groupId>
  <artifactId>cbioportal</artifactId>
  <version>v7.0.6-SNAPSHOT</version>
  <properties>
    <frontend.version>v7.0.5</frontend.version>
  </properties>
</project>
"""


@pytest.fixture
def workdir(tmp_path):
    (tmp_path / "pom.xml").write_text(POM)
    return tmp_path


def test_read_values_skips_the_parent_version(workdir):
    # A naive "first <version>" read would return the Spring Boot parent's 3.5.6.
    assert pom.read_values(workdir) == ("v7.0.6-SNAPSHOT", "v7.0.5")


def test_parent_version_is_read_separately(workdir):
    assert pom.parent_version(workdir) == "3.5.6"


def test_read_values_after_the_frontend_workflow_stripped_the_suffix(tmp_path):
    # This is the state the disabled snapshot-release.yml choked on: the frontend
    # job had already replaced `vX-SNAPSHOT` with a bare tag, so the backend sed
    # matched nothing, produced no diff, and `git commit` exited 1.
    (tmp_path / "pom.xml").write_text(POM.replace("v7.0.6-SNAPSHOT", "v7.0.6"))
    assert pom.read_values(tmp_path) == ("v7.0.6", "v7.0.5")


def test_assert_only_pom_changed_rejects_a_wider_diff(monkeypatch, workdir):
    monkeypatch.setattr(pom, "run", lambda *a, **k: "pom.xml\nsrc/other.java\n")
    with pytest.raises(pom.PomError, match="only pom.xml"):
        pom.assert_only_pom_changed(workdir)


def test_assert_only_pom_changed_rejects_an_empty_diff(monkeypatch, workdir):
    # An empty diff means the edit silently did nothing -- the exact failure mode
    # that made the snapshot workflow look flaky rather than broken.
    monkeypatch.setattr(pom, "run", lambda *a, **k: "")
    with pytest.raises(pom.PomError, match="only pom.xml"):
        pom.assert_only_pom_changed(workdir)


def test_assert_only_pom_changed_accepts_the_expected_diff(monkeypatch, workdir):
    monkeypatch.setattr(pom, "run", lambda *a, **k: "pom.xml\n")
    pom.assert_only_pom_changed(workdir)


def test_snapshot_bump_skips_when_master_is_already_ahead(workdir, config, plan_v706):
    # pom is at v7.0.6-SNAPSHOT and we just released v7.0.5: nothing to do.
    released = {**plan_v706, "version": "v7.0.5"}
    result = pom.bump_snapshot(config, released, workdir, dry_run=False)
    assert result == {"skipped": True}


def test_snapshot_bump_proceeds_when_master_matches_the_release(
    monkeypatch, tmp_path, config, plan_v706
):
    (tmp_path / "pom.xml").write_text(POM.replace("v7.0.6-SNAPSHOT", "v7.0.6"))
    calls = {}

    def fake_set_version(workdir, new_version):
        calls["version"] = new_version
        (workdir / "pom.xml").write_text(
            POM.replace("v7.0.6-SNAPSHOT", new_version)
        )

    monkeypatch.setattr(pom, "set_version", fake_set_version)
    monkeypatch.setattr(pom, "assert_only_pom_changed", lambda *_: None)
    monkeypatch.setattr(pom, "commit_to_base", lambda *a, **k: {"sha": "abc"})

    pom.bump_snapshot(config, plan_v706, tmp_path, dry_run=False)
    assert calls["version"] == "v7.0.7-SNAPSHOT"


def test_snapshot_bump_refuses_to_touch_the_frontend_pin(
    monkeypatch, tmp_path, config, plan_v706
):
    (tmp_path / "pom.xml").write_text(POM.replace("v7.0.6-SNAPSHOT", "v7.0.6"))

    def clobbering_set_version(workdir, new_version):
        (workdir / "pom.xml").write_text(
            POM.replace("v7.0.6-SNAPSHOT", new_version).replace(
                "<frontend.version>v7.0.5", "<frontend.version>v7.0.7"
            )
        )

    monkeypatch.setattr(pom, "set_version", clobbering_set_version)
    monkeypatch.setattr(pom, "assert_only_pom_changed", lambda *_: None)
    with pytest.raises(pom.PomError, match="frontend pin"):
        pom.bump_snapshot(config, plan_v706, tmp_path, dry_run=False)


# ---- the commit path -------------------------------------------------------


def fake_git(recorded, fail_pushes=0):
    """A `pom.run` stand-in that records commands and fails the first N pushes."""
    pushes = {"seen": 0}

    def run(command, cwd):
        recorded.append(" ".join(command))
        if command[:2] == ["git", "push"]:
            pushes["seen"] += 1
            if pushes["seen"] <= fail_pushes:
                raise pom.PomError("! [rejected] master -> master (fetch first)")
        if command[:2] == ["git", "rev-parse"]:
            return "deadbeefcafe\n"
        return ""

    return run


def test_commit_goes_straight_to_the_base_branch(monkeypatch, workdir):
    recorded = []
    monkeypatch.setattr(pom, "run", fake_git(recorded))

    result = pom.commit_to_base(
        workdir, "cBioPortal/cbioportal", "master", "Frontend v7.0.6", "body",
        expect=("v7.0.6", "v7.0.6"), dry_run=False,
    )

    assert result == {"sha": "deadbeefcafe"}
    assert "git push origin HEAD:master" in recorded
    # No branch, and nothing that could discard what someone else merged.
    assert not any("checkout" in c for c in recorded)
    assert not any("force" in c for c in recorded)


def test_commit_rebases_when_the_base_branch_moved(monkeypatch, tmp_path):
    # The rebase lands the same values the edit intended, so the retry proceeds.
    (tmp_path / "pom.xml").write_text(POM.replace("v7.0.6-SNAPSHOT", "v7.0.6")
                                         .replace("v7.0.5</frontend", "v7.0.6</frontend"))
    recorded = []
    monkeypatch.setattr(pom, "run", fake_git(recorded, fail_pushes=1))

    result = pom.commit_to_base(
        tmp_path, "cBioPortal/cbioportal", "master", "Frontend v7.0.6", "body",
        expect=("v7.0.6", "v7.0.6"), dry_run=False,
    )

    assert result == {"sha": "deadbeefcafe"}
    pushes = [i for i, c in enumerate(recorded) if c.startswith("git push")]
    assert len(pushes) == 2
    between = recorded[pushes[0] + 1:pushes[1]]
    assert between == ["git fetch origin master", "git rebase FETCH_HEAD"]


def test_commit_gives_up_when_the_rebase_lands_a_different_pom(monkeypatch, workdir):
    # workdir's pom is still v7.0.6-SNAPSHOT/v7.0.5, not what this edit intended:
    # something else is editing the pom at the same time.
    monkeypatch.setattr(pom, "run", fake_git([], fail_pushes=1))
    with pytest.raises(pom.PomError, match="no longer matches"):
        pom.commit_to_base(
            workdir, "cBioPortal/cbioportal", "master", "Frontend v7.0.6", "body",
            expect=("v7.0.6", "v7.0.6"), dry_run=False,
        )


def test_dry_run_writes_nothing(monkeypatch, workdir):
    recorded = []
    monkeypatch.setattr(pom, "run", fake_git(recorded))

    result = pom.commit_to_base(
        workdir, "cBioPortal/cbioportal", "master", "Frontend v7.0.6", "body",
        expect=("v7.0.6", "v7.0.6"), dry_run=True,
    )

    assert result == {"dry_run": True}
    assert recorded == ["git diff"]
