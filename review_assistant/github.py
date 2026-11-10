# Copyright SUSE LLC
# SPDX-License-Identifier: MIT
"""GitHub access with two credentials: a read-only token for listing and diffs, the write token for COMMENT reviews."""

from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, Literal

import requests
from pydantic import BaseModel, ConfigDict, Field

from review_assistant.review import PullRequest

API = "https://api.github.com"
JSON_ACCEPT = "application/vnd.github+json"
DIFF_ACCEPT = "application/vnd.github.diff"
TIMEOUT_SECONDS = 60


class GitHubError(RuntimeError):
    """A credential is missing or empty."""


class PullSummary(BaseModel):
    """Open pull request as listed; enough to decide whether it needs a review."""

    model_config = ConfigDict(frozen=True)

    number: int
    head_sha: str
    title: str
    body: str


class InlineComment(BaseModel):
    """Comment anchored to one line of the diff."""

    model_config = ConfigDict(frozen=True)

    path: str
    line: int
    body: str


class ReviewDraft(BaseModel):
    """Review JSON for POST /pulls/N/reviews; only COMMENT is accepted."""

    model_config = ConfigDict(frozen=True)

    commit_id: str
    event: Literal["COMMENT"]
    body: str = ""
    comments: list[InlineComment] = Field(default_factory=list)


def read_token_file(path: Path) -> str:
    """Return the read-only token stored in path."""
    return _non_empty(path.read_text(encoding="utf-8"), str(path))


def write_token_from_env(environ: Mapping[str, str]) -> str:
    """Return the write token from REVIEW_ASSISTANT_GH_TOKEN, the only source for it."""
    name = "REVIEW_ASSISTANT_GH_TOKEN"
    return _non_empty(environ.get(name, ""), name)


def _non_empty(raw: str, source: str) -> str:
    token = raw.strip()
    if not token:
        msg = f"no token in {source}"
        raise GitHubError(msg)
    return token


def _session(token: str) -> requests.Session:
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {token}", "X-GitHub-Api-Version": "2022-11-28"})
    return session


def _get(
    session: requests.Session, url: str, accept: str, params: Mapping[str, str] | None = None
) -> requests.Response:
    response = session.get(url, headers={"Accept": accept}, params=params, timeout=TIMEOUT_SECONDS)
    response.raise_for_status()
    return response


def _paginate(session: requests.Session, url: str, params: Mapping[str, str]) -> Iterator[dict[str, Any]]:
    next_url: str | None = url
    next_params: Mapping[str, str] | None = params
    while next_url:
        response = _get(session, next_url, JSON_ACCEPT, next_params)
        yield from response.json()
        next_url = response.links.get("next", {}).get("url")
        next_params = None


def list_open_pulls(token: str, repo: str) -> list[PullSummary]:
    """List open pull requests of repo, following pagination."""
    session = _session(token)
    url = f"{API}/repos/{repo}/pulls"
    return [
        PullSummary(
            number=item["number"],
            head_sha=item["head"]["sha"],
            title=item["title"],
            body=item["body"] or "",
        )
        for item in _paginate(session, url, {"state": "open", "per_page": "100"})
    ]


def fetch_pull_request(token: str, repo: str, number: int) -> PullRequest:
    """Fetch the metadata and diff of one pull request."""
    session = _session(token)
    url = f"{API}/repos/{repo}/pulls/{number}"
    meta = _get(session, url, JSON_ACCEPT).json()
    diff = _get(session, url, DIFF_ACCEPT).text
    return PullRequest(
        repo=repo,
        number=number,
        head_sha=meta["head"]["sha"],
        title=meta["title"],
        body=meta["body"] or "",
        diff=diff,
        changed_lines=meta["additions"] + meta["deletions"],
    )


def post_review(token: str, repo: str, number: int, review: Mapping[str, Any]) -> int:
    """Post a COMMENT review and return its id; any other event fails validation before a request."""
    draft = ReviewDraft.model_validate(review)
    response = _session(token).post(
        f"{API}/repos/{repo}/pulls/{number}/reviews",
        json=draft.model_dump(),
        headers={"Accept": JSON_ACCEPT},
        timeout=TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return int(response.json()["id"])
