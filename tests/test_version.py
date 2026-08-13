import pytest

from release import version as ver


def test_bump_defaults_to_patch():
    assert ver.bump("v7.0.5") == "v7.0.6"


@pytest.mark.parametrize(
    "component,expected",
    [("patch", "v7.0.6"), ("minor", "v7.1.0"), ("major", "v8.0.0")],
)
def test_bump_components(component, expected):
    assert ver.bump("v7.0.5", component) == expected


def test_minor_and_major_reset_lower_components():
    assert ver.bump("v6.4.5", "minor") == "v6.5.0"
    assert ver.bump("v6.4.5", "major") == "v7.0.0"


def test_latest_ignores_npm_module_tags():
    # cbioportal-frontend tags its lerna packages in the same namespace; those must
    # never be mistaken for a release. This is the same confusion that makes
    # `tags: '*'` fire the Maven publish for `react-mutation-mapper@0.9.9`.
    tags = [
        "v7.0.4",
        "react-mutation-mapper@0.9.9",
        "v7.0.5",
        "oncoprintjs@6.1.5",
        "v6.4.5",
    ]
    assert ver.latest(tags) == "v7.0.5"


def test_latest_compares_numerically_not_lexically():
    assert ver.latest(["v7.0.9", "v7.0.10"]) == "v7.0.10"
    assert ver.latest(["v6.4.5", "v7.0.0"]) == "v7.0.0"


def test_snapshot_is_one_patch_above_the_release():
    assert ver.snapshot("v7.0.5") == "v7.0.6-SNAPSHOT"
    assert ver.snapshot("v7.1.0") == "v7.1.1-SNAPSHOT"


def test_is_release_tag():
    assert ver.is_release_tag("v7.0.5")
    assert not ver.is_release_tag("v7.0.0-rc.9")
    assert not ver.is_release_tag("v7.0.6-SNAPSHOT")
    assert not ver.is_release_tag("oncoprintjs@6.1.5")


def test_parse_rejects_junk():
    with pytest.raises(ValueError):
        ver.parse("not-a-tag")
