"""Thin GitHub REST client.

Deliberately not PyGithub: the orchestrator touches ~15 endpoints, and a plain
requests wrapper is smaller, has no hidden lazy-loading, and is trivial to record
fixtures for.
"""

from __future__ import annotations

import logging
import os
import time

import requests

logger = logging.getLogger(__name__)

API = "https://api.github.com"


class GitHubError(RuntimeError):
    pass


class GitHub:
    def __init__(self, token: str | None = None, session: requests.Session | None = None):
        self.token = token or os.environ.get("GITHUB_TOKEN", "")
        self.session = session or requests.Session()

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "cbioportal-release-manager",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def request(self, method: str, path: str, **kwargs):
        url = path if path.startswith("http") else f"{API}{path}"
        for attempt in range(3):
            response = self.session.request(
                method, url, headers=self._headers(), timeout=30, **kwargs
            )
            # 403 with a rate-limit reset is worth waiting out; other 4xx are not.
            if response.status_code == 403 and "rate limit" in response.text.lower():
                reset = int(response.headers.get("X-RateLimit-Reset", 0))
                delay = max(1, min(60, reset - int(time.time())))
                time.sleep(delay)
                continue
            if response.status_code >= 500 and attempt < 2:
                time.sleep(2**attempt)
                continue
            break

        # Never log headers: they carry the installation token.
        logger.debug("%s %s -> %s (%.0f ms)", method, url, response.status_code,
                     response.elapsed.total_seconds() * 1000)

        if response.status_code == 404:
            return None
        if not response.ok:
            raise GitHubError(f"{method} {url} -> {response.status_code}: {response.text[:400]}")
        if response.status_code == 204 or not response.content:
            return {}
        return response.json()

    def get(self, path: str, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path: str, json: dict):
        return self.request("POST", path, json=json)

    def patch(self, path: str, json: dict):
        return self.request("PATCH", path, json=json)

    def put(self, path: str, json: dict):
        return self.request("PUT", path, json=json)

    def graphql(self, query: str, **variables):
        """The one thing REST cannot do: Projects v2 is GraphQL-only."""
        payload = self.post("/graphql", {"query": query, "variables": variables})
        if payload.get("errors"):
            raise GitHubError(f"graphql: {payload['errors']}")
        return payload["data"]

    def paginate(self, path: str, key: str | None = None):
        """Yield items across pages. `key` names the list field for envelope responses."""
        url = f"{path}{'&' if '?' in path else '?'}per_page=100"
        while url:
            response = self.session.request("GET", url if url.startswith("http") else f"{API}{url}",
                                            headers=self._headers(), timeout=30)
            if not response.ok:
                raise GitHubError(f"GET {url} -> {response.status_code}: {response.text[:400]}")
            payload = response.json()
            items = payload[key] if key else payload
            yield from items
            url = response.links.get("next", {}).get("url")

    # ---- endpoints the orchestrator actually uses -------------------------

    def branch_sha(self, repo: str, branch: str) -> str:
        commit = self.get(f"/repos/{repo}/commits/{branch}")
        if not commit:
            raise GitHubError(f"{repo}: branch {branch} not found")
        return commit["sha"]

    def releases(self, repo: str) -> list[dict]:
        return list(self.paginate(f"/repos/{repo}/releases"))

    def published_releases(self, repo: str) -> list[dict]:
        """Releases that actually exist."""
        return [release for release in self.releases(repo) if not release["draft"]]

    def release_by_tag(self, repo: str, tag: str) -> dict | None:
        """A published release with this tag. A draft holding the tag does not count."""
        release = self.get(f"/repos/{repo}/releases/tags/{tag}")
        return None if (release and release.get("draft")) else release

    def draft_release(self, repo: str) -> dict | None:
        """The release-drafter draft, i.e. the newest release with draft=true."""
        for release in self.releases(repo):
            if release["draft"]:
                return release
        return None

    def ref_sha(self, repo: str, ref: str) -> str | None:
        data = self.get(f"/repos/{repo}/git/ref/{ref}")
        return data["object"]["sha"] if data else None

    def create_ref(self, repo: str, ref: str, sha: str) -> dict:
        return self.post(f"/repos/{repo}/git/refs", {"ref": ref, "sha": sha})

    def create_release(self, repo: str, tag: str, name: str, body: str, sha: str,
                       prerelease: bool = True) -> dict:
        return self.post(
            f"/repos/{repo}/releases",
            {
                "tag_name": tag,
                "target_commitish": sha,
                "name": name,
                "body": body,
                "draft": False,
                "prerelease": prerelease,
            },
        )

    def update_release(self, repo: str, release_id: int, **fields) -> dict:
        return self.patch(f"/repos/{repo}/releases/{release_id}", fields)

    def combined_state(self, repo: str, sha: str) -> dict[str, str]:
        """Merge check-runs and commit statuses into one {name: conclusion} map.

        Both matter: GitHub Actions reports check-runs, CircleCI reports statuses.
        Where a name appears more than once, a failure wins -- a flaky job that
        passed on retry should not mask a real failure without someone looking.
        """
        results: dict[str, str] = {}

        def record(name: str, state: str) -> None:
            if name in results and results[name] not in ("success", "neutral", "skipped"):
                return
            results[name] = state

        checks = self.get(f"/repos/{repo}/commits/{sha}/check-runs?per_page=100") or {}
        for run in checks.get("check_runs", []):
            state = run["conclusion"] if run["status"] == "completed" else "pending"
            record(run["name"], state or "pending")

        status = self.get(f"/repos/{repo}/commits/{sha}/status?per_page=100") or {}
        for entry in status.get("statuses", []):
            record(entry["context"], entry["state"])

        return results

    def create_pull(self, repo: str, head: str, base: str, title: str, body: str) -> dict:
        return self.post(
            f"/repos/{repo}/pulls", {"head": head, "base": base, "title": title, "body": body}
        )

    def find_pull(self, repo: str, head_branch: str, base: str) -> dict | None:
        owner = repo.split("/")[0]
        pulls = self.get(f"/repos/{repo}/pulls?head={owner}:{head_branch}&base={base}&state=open")
        return pulls[0] if pulls else None

    def dispatch_workflow(self, repo: str, workflow: str, ref: str, inputs: dict) -> dict:
        return self.post(
            f"/repos/{repo}/actions/workflows/{workflow}/dispatches",
            {"ref": ref, "inputs": inputs},
        )

    def workflow_runs(self, repo: str, workflow: str, **params) -> list[dict]:
        query = "&".join(f"{k}={v}" for k, v in params.items())
        data = self.get(f"/repos/{repo}/actions/workflows/{workflow}/runs?per_page=20&{query}")
        return (data or {}).get("workflow_runs", [])

    def find_issue(self, repo: str, title: str) -> dict | None:
        for issue in self.paginate(f"/repos/{repo}/issues?state=all&sort=created&direction=desc"):
            if issue.get("title") == title and "pull_request" not in issue:
                return issue
        return None

    def create_issue(self, repo: str, title: str, body: str, labels: list[str],
                     assignees: list[str]) -> dict:
        return self.post(
            f"/repos/{repo}/issues",
            {"title": title, "body": body, "labels": labels, "assignees": assignees},
        )

    def update_issue(self, repo: str, number: int, **fields) -> dict:
        return self.patch(f"/repos/{repo}/issues/{number}", fields)

    def comment(self, repo: str, number: int, body: str) -> dict:
        return self.post(f"/repos/{repo}/issues/{number}/comments", {"body": body})

    def file_contents(self, repo: str, path: str, ref: str = "HEAD") -> str | None:
        import base64

        data = self.get(f"/repos/{repo}/contents/{path}?ref={ref}")
        if not data:
            return None
        return base64.b64decode(data["content"]).decode()

    def put_file(self, repo: str, path: str, content: str, message: str, branch: str) -> dict:
        import base64

        existing = self.get(f"/repos/{repo}/contents/{path}?ref={branch}") or {}
        payload = {
            "message": message,
            "content": base64.b64encode(content.encode()).decode(),
            "branch": branch,
        }
        if existing.get("sha"):
            payload["sha"] = existing["sha"]
        return self.put(f"/repos/{repo}/contents/{path}", payload)
