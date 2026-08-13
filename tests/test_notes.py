from release import notes

from .conftest import BACKEND_TITLES, FRONTEND_TITLES

REPOS = {
    "frontend": "cBioPortal/cbioportal-frontend",
    "backend": "cBioPortal/cbioportal",
}


def test_link_prs_handles_five_digit_numbers():
    # The sed in the release note was `s|(#\(....\)|...|`, which matched exactly four
    # digits and silently left five-digit references unlinked.
    body = "- A change @dev (#5617)\n- Another @dev (#12345)\n"
    linked = notes.link_prs(body, REPOS["frontend"])
    assert (
        "([cbioportal-frontend#5617]"
        "(https://github.com/cBioPortal/cbioportal-frontend/pull/5617))" in linked
    )
    assert (
        "([cbioportal-frontend#12345]"
        "(https://github.com/cBioPortal/cbioportal-frontend/pull/12345))" in linked
    )


def test_link_prs_is_idempotent_on_already_linked_text():
    once = notes.link_prs("- A change (#5617)", REPOS["frontend"])
    twice = notes.link_prs(once, REPOS["frontend"])
    assert once == twice


def test_combine_merges_matching_categories(backend_draft, frontend_draft, plan_v706):
    combined = notes.combine(
        backend_draft, frontend_draft, BACKEND_TITLES, FRONTEND_TITLES, REPOS, plan_v706
    )
    features = combined.split("## 🐛 Bug Fixes")[0]
    assert "Add clickhouse importer" in features
    assert "Add mRNA violin plot" in features


def test_combine_keeps_categories_unique_to_one_repo(
    backend_draft, frontend_draft, plan_v706
):
    # The two release-drafter configs are NOT identical: the backend has REST API
    # sections the frontend lacks and vice versa. The merge takes the union.
    combined = notes.combine(
        backend_draft, frontend_draft, BACKEND_TITLES, FRONTEND_TITLES, REPOS, plan_v706
    )
    assert "## ⚙️ REST API Changes" in combined
    assert "## 📦 Package Improvements" in combined


def test_combine_orders_backend_categories_first(
    backend_draft, frontend_draft, plan_v706
):
    combined = notes.combine(
        backend_draft, frontend_draft, BACKEND_TITLES, FRONTEND_TITLES, REPOS, plan_v706
    )
    assert combined.index("REST API Changes") < combined.index("Package Improvements")


def test_combine_omits_empty_categories(backend_draft, frontend_draft, plan_v706):
    combined = notes.combine(
        backend_draft, frontend_draft, BACKEND_TITLES, FRONTEND_TITLES, REPOS, plan_v706
    )
    assert "✨ Enhancements" not in combined


def test_combine_links_only_frontend_prs(backend_draft, frontend_draft, plan_v706):
    combined = notes.combine(
        backend_draft, frontend_draft, BACKEND_TITLES, FRONTEND_TITLES, REPOS, plan_v706
    )
    assert "cbioportal-frontend#5617" in combined
    # Backend PRs stay bare so GitHub resolves them in the backend release.
    assert "- Add clickhouse importer @dev-a (#12001)" in combined


def test_combine_keeps_the_trailing_boilerplate(
    backend_draft, frontend_draft, plan_v706
):
    combined = notes.combine(
        backend_draft, frontend_draft, BACKEND_TITLES, FRONTEND_TITLES, REPOS, plan_v706
    )
    assert "Full commit logs" in combined
    assert "Notes on versioning" in combined


def test_tail_corrects_compare_links_on_a_minor_release(backend_draft, plan_v706):
    # release-drafter always renders $NEXT_PATCH_VERSION, so on a minor bump the
    # compare links would point at a version that will never exist.
    plan_v706 = {**plan_v706, "version": "v7.1.0", "bump": "minor"}
    tail = "\n".join(notes.tail(backend_draft, plan_v706))
    assert "compare/v7.0.5...v7.1.0" in tail
    assert "v7.0.6" not in tail


def test_tail_leaves_patch_releases_alone(backend_draft, plan_v706):
    tail = "\n".join(notes.tail(backend_draft, plan_v706))
    assert "compare/v7.0.5...v7.0.6" in tail
