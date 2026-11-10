# Copyright SUSE LLC
# SPDX-License-Identifier: MIT
"""Tests for the GitHub client, against recorded responses; no network and no real token."""

import json
from pathlib import Path

import pydantic
import pytest
import requests
import responses
from responses import matchers

from review_assistant.github import (
    API,
    DIFF_ACCEPT,
    GitHubError,
    fetch_pull_request,
    list_open_pulls,
    post_review,
    read_token_file,
    write_token_from_env,
)

REPO = "os-autoinst/openQA"
PULLS = f"{API}/repos/{REPO}/pulls"
READ_TOKEN = "read-token-placeholder"
WRITE_TOKEN = "write-token-placeholder"
HEAD = "a6d255abcbce67a380f6716c94e861869ed64882"
PULL_META = {
    "title": "Fix retry",
    "body": "Retries were unbounded.",
    "head": {"sha": HEAD},
    "additions": 30,
    "deletions": 12,
}
REVIEW = {
    "commit_id": HEAD,
    "event": "COMMENT",
    "body": "One point.",
    "comments": [{"path": "lib/OpenQA/Test.pm", "line": 7, "body": "nit: spacing"}],
}


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("  read-token-placeholder \n", id="padded"),
        pytest.param("read-token-placeholder", id="bare"),
    ],
)
def test_read_token_file_is_stripped(tmp_path: Path, raw: str) -> None:
    token_file = tmp_path / "token"
    token_file.write_text(raw, encoding="utf-8")
    assert read_token_file(token_file) == READ_TOKEN


@pytest.mark.parametrize("raw", [pytest.param("", id="empty"), pytest.param(" \n", id="blank")])
def test_empty_token_file_is_rejected(tmp_path: Path, raw: str) -> None:
    token_file = tmp_path / "token"
    token_file.write_text(raw, encoding="utf-8")
    with pytest.raises(GitHubError, match="no token in"):
        read_token_file(token_file)


def test_write_token_is_read_from_environment() -> None:
    assert write_token_from_env({"REVIEW_ASSISTANT_GH_TOKEN": WRITE_TOKEN}) == WRITE_TOKEN


@pytest.mark.parametrize(
    "environ",
    [
        pytest.param({}, id="unset"),
        pytest.param({"REVIEW_ASSISTANT_GH_TOKEN": "  "}, id="blank"),
    ],
)
def test_missing_write_token_is_rejected(environ: dict[str, str]) -> None:
    with pytest.raises(GitHubError, match="REVIEW_ASSISTANT_GH_TOKEN"):
        write_token_from_env(environ)


def test_open_pulls_are_listed_across_pages_with_read_token() -> None:
    first = {"Link": f'<{PULLS}?page=2>; rel="next"'}
    with responses.RequestsMock() as rsps:
        rsps.get(
            PULLS,
            json=[{"number": 1, "head": {"sha": HEAD}, "title": "One", "body": None}],
            headers=first,
            match=[matchers.query_param_matcher({"state": "open", "per_page": "100"})],
        )
        rsps.get(
            PULLS,
            json=[{"number": 2, "head": {"sha": "b" * 40}, "title": "Two", "body": "text"}],
            match=[matchers.query_param_matcher({"page": "2"})],
        )
        pulls = list_open_pulls(READ_TOKEN, REPO)
        auth = rsps.calls[0].request.headers["Authorization"]
    assert [(pull.number, pull.title, pull.body) for pull in pulls] == [(1, "One", ""), (2, "Two", "text")]
    assert pulls[1].head_sha == "b" * 40
    assert auth == f"Bearer {READ_TOKEN}"


def test_pull_request_is_fetched_with_metadata_and_diff() -> None:
    url = f"{PULLS}/8079"
    with responses.RequestsMock() as rsps:
        rsps.get(url, json=PULL_META, match=[matchers.header_matcher({"Accept": "application/vnd.github+json"})])
        rsps.get(url, body="diff --git a/x b/x\n", match=[matchers.header_matcher({"Accept": DIFF_ACCEPT})])
        pull = fetch_pull_request(READ_TOKEN, REPO, 8079)
    assert (pull.repo, pull.number, pull.head_sha, pull.title, pull.body) == (
        REPO,
        8079,
        HEAD,
        "Fix retry",
        "Retries were unbounded.",
    )
    assert (pull.changed_lines, pull.diff) == (42, "diff --git a/x b/x\n")


def test_listing_http_error_propagates() -> None:
    with responses.RequestsMock() as rsps:
        rsps.get(PULLS, status=403, json={"message": "Forbidden"})
        with pytest.raises(requests.HTTPError):
            list_open_pulls(READ_TOKEN, REPO)


def test_comment_review_is_posted_with_write_token() -> None:
    url = f"{PULLS}/8079/reviews"
    with responses.RequestsMock() as rsps:
        rsps.post(url, json={"id": 5841967131})
        review_id = post_review(WRITE_TOKEN, REPO, 8079, REVIEW)
        request = rsps.calls[0].request
    assert review_id == 5841967131
    assert request.headers["Authorization"] == f"Bearer {WRITE_TOKEN}"
    assert isinstance(request.body, bytes)
    assert json.loads(request.body) == REVIEW


@pytest.mark.parametrize(
    "event",
    [
        pytest.param("APPROVE", id="approve"),
        pytest.param("REQUEST_CHANGES", id="request-changes"),
        pytest.param("DISMISS", id="dismiss"),
        pytest.param(None, id="missing"),
    ],
)
def test_non_comment_review_is_rejected_before_any_request(event: str | None) -> None:
    review = {**REVIEW, "event": event} if event else {k: v for k, v in REVIEW.items() if k != "event"}
    with responses.RequestsMock() as rsps:
        with pytest.raises(pydantic.ValidationError):
            post_review(WRITE_TOKEN, REPO, 8079, review)
        assert len(rsps.calls) == 0
