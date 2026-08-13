import pytest

# Shapes copied from the real release-drafter output in both repos: the backend
# template emits a literal `## Changes` above $CHANGES, the frontend template does
# not, and change-template renders `- $TITLE @$AUTHOR (#$NUMBER)`.

BACKEND_DRAFT = """## Changes
- Unlabelled backend change @someone (#12345)

## 🧬 Features
- Add clickhouse importer @dev-a (#12001)

## 🐛 Bug Fixes
- Cap clinical-events pageSize @dev-b (#12287)

## ⚙️ REST API Changes
- Deprecate legacy endpoint @dev-c (#12100)

## 🕵️‍♀️ Full commit logs

- Backend: https://github.com/cBioPortal/cbioportal/compare/v7.0.5...v7.0.6
- Frontend: https://github.com/cBioPortal/cbioportal-frontend/compare/v7.0.5...v7.0.6

## 🏷Notes on versioning and release procedure
https://docs.cbioportal.org/development/release-procedure/#a-note-on-versioning
"""

FRONTEND_DRAFT = """## 🧬 Features
- Add mRNA violin plot to study view @dev-d (#5617)

## 🐛 Bug Fixes
- Fix oncoprint sort @dev-e (#5620)

## 📦 Package Improvements
- Bump react-mutation-mapper @dev-f (#5599)
"""

BACKEND_TITLES = [
    "🧬 Features",
    "✨ Enhancements",
    "🐛 Bug Fixes",
    "⚙️ REST API Changes",
]

FRONTEND_TITLES = [
    "🧬 Features",
    "✨ Enhancements",
    "🐛 Bug Fixes",
    "📦 Package Improvements",
]


@pytest.fixture
def backend_draft():
    return BACKEND_DRAFT


@pytest.fixture
def frontend_draft():
    return FRONTEND_DRAFT


@pytest.fixture
def plan_v706():
    return {
        "version": "v7.0.6",
        "previous_version": "v7.0.5",
        "bump": "patch",
        "warnings": [],
        "frontend": {
            "repo": "cBioPortal/cbioportal-frontend",
            "branch": "master",
            "sha": "1b6d62691a590eafca3db3dda879c6c0ea627c02",
            "pr_count": 3,
        },
        "backend": {
            "repo": "cBioPortal/cbioportal",
            "branch": "master",
            "sha": "b9836664e02e2c429da7e305f592df30d29e4642",
            "pr_count": 4,
            "pom_snapshot": "v7.0.6-SNAPSHOT",
            "pom_frontend_version": "v7.0.5",
        },
    }


@pytest.fixture
def config():
    from release import config as config_module

    return config_module.load()
