# Copyright SUSE LLC
# SPDX-License-Identifier: MIT
"""Tests for the review CLI, with GitHub, the skill checkout and Hermes replaced by fakes."""

import json
import os
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
from pytest_mock import MockerFixture

from review_assistant.cli import EXIT_FAILED, EXIT_OK, EXIT_USAGE, main
from review_assistant.config import ReviewerConfig
from review_assistant.github import PullSummary, ReviewDraft
from review_assistant.review import PullRequest, ReviewError
from review_assistant.state import ReviewRecord, load_state, save_state, state_key

REPO = "os-autoinst/openQA"
EXAMPLE = Path(__file__).parent.parent / "config" / "reviewer.example.yaml"
WRITE_TOKEN = "write-token-placeholder"
PULL_NUMBERS = (1, 2, 3)


def _head(number: int) -> str:
    return str(number) * 40


PULLS = [PullSummary(number=n, head_sha=_head(n), title=f"PR {n}", body="") for n in PULL_NUMBERS]


@pytest.fixture
def fakes(mocker: MockerFixture, tmp_path: Path) -> SimpleNamespace:
    token_file = tmp_path / "read-token"
    token_file.write_text("read-token-placeholder", encoding="utf-8")
    ns = SimpleNamespace(
        token_file=token_file,
        state_file=tmp_path / "state.json",
        skill_cache=tmp_path / "skills",
        review_errors={},
        review_overrides={},
        post_errors={},
    )

    def fetch(_token: str, repo: str, number: int) -> PullRequest:
        return PullRequest(
            repo=repo,
            number=number,
            head_sha=_head(number),
            title=f"PR {number}",
            body="",
            diff="diff --git a/x b/x\n",
            changed_lines=3,
        )

    def draft(_config: ReviewerConfig, pull: PullRequest, _skill_root: Path, scratch_root: Path) -> Path:
        if pull.number in ns.review_errors:
            raise ns.review_errors[pull.number]
        directory = scratch_root / f"pr-{pull.number}"
        directory.mkdir(parents=True, exist_ok=True)
        review = {
            "commit_id": pull.head_sha,
            "event": "COMMENT",
            "body": f"review {pull.number}",
            "comments": [],
            **ns.review_overrides.get(pull.number, {}),
        }
        path = directory / f"review-{pull.number}.json"
        path.write_text(json.dumps(review), encoding="utf-8")
        return path

    def post(_token: str, _repo: str, number: int, review: dict) -> int:
        if number in ns.post_errors:
            raise ns.post_errors[number]
        ReviewDraft.model_validate(review)
        return 5000 + number

    mocker.patch("review_assistant.cli.checkout_skill", return_value=tmp_path / "checkout")
    ns.listing = mocker.patch("review_assistant.cli.list_open_pulls", return_value=PULLS)
    ns.fetch = mocker.patch("review_assistant.cli.fetch_pull_request", side_effect=fetch)
    ns.review = mocker.patch("review_assistant.cli.review_pull_request", side_effect=draft)
    ns.post_review = mocker.patch("review_assistant.cli.post_review", side_effect=post)
    return ns


def _argv(ns: SimpleNamespace, *extra: str) -> list[str]:
    return [
        "run",
        "--config",
        str(EXAMPLE),
        "--read-token-file",
        str(ns.token_file),
        "--state-file",
        str(ns.state_file),
        "--skill-cache",
        str(ns.skill_cache),
        *extra,
    ]


def _with_write_token(mocker: MockerFixture) -> None:
    mocker.patch.dict(os.environ, {"REVIEW_ASSISTANT_GH_TOKEN": WRITE_TOKEN})


def _fetched_numbers(ns: SimpleNamespace) -> list[int]:
    return [call.args[2] for call in ns.fetch.call_args_list]


def test_dry_run_prints_reviews_and_writes_nothing(fakes: SimpleNamespace, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(_argv(fakes)) == EXIT_OK
    out = capsys.readouterr().out
    assert f"# {REPO}#1 at {_head(1)}" in out
    assert '"body": "review 3"' in out
    fakes.post_review.assert_not_called()
    assert not fakes.state_file.exists()


def test_post_records_each_posted_review(fakes: SimpleNamespace, mocker: MockerFixture) -> None:
    _with_write_token(mocker)
    assert main(_argv(fakes, "--post")) == EXIT_OK
    assert fakes.post_review.call_args_list[0].args[:3] == (WRITE_TOKEN, REPO, 1)
    assert load_state(fakes.state_file) == {
        state_key(REPO, n): ReviewRecord(head_sha=_head(n), review_id=5000 + n) for n in PULL_NUMBERS
    }


def test_post_without_write_token_fails_before_listing(fakes: SimpleNamespace, mocker: MockerFixture) -> None:
    mocker.patch.dict(os.environ, {}, clear=True)
    assert main(_argv(fakes, "--post")) == EXIT_USAGE
    fakes.listing.assert_not_called()


def test_dry_run_removes_write_token_so_agent_cannot_inherit_it(fakes: SimpleNamespace, mocker: MockerFixture) -> None:
    _with_write_token(mocker)
    assert main(_argv(fakes)) == EXIT_OK
    assert "REVIEW_ASSISTANT_GH_TOKEN" not in os.environ
    fakes.post_review.assert_not_called()


def test_reviewed_head_is_skipped(fakes: SimpleNamespace) -> None:
    save_state(fakes.state_file, {state_key(REPO, 1): ReviewRecord(head_sha=_head(1), review_id=1)})
    assert main(_argv(fakes)) == EXIT_OK
    assert _fetched_numbers(fakes) == [2, 3]


def test_limit_caps_new_reviews_after_skips(fakes: SimpleNamespace) -> None:
    save_state(fakes.state_file, {state_key(REPO, 1): ReviewRecord(head_sha=_head(1), review_id=1)})
    assert main(_argv(fakes, "--limit", "1")) == EXIT_OK
    assert _fetched_numbers(fakes) == [2]


@pytest.mark.parametrize(
    "configure",
    [
        pytest.param(lambda ns: ns.review_errors.update({1: ReviewError("lint failed")}), id="review-raises"),
        pytest.param(lambda ns: ns.review_overrides.update({1: {"commit_id": "0" * 40}}), id="commit-id-mismatch"),
        pytest.param(lambda ns: ns.review_overrides.update({1: {"event": "APPROVE"}}), id="approve-event"),
        pytest.param(lambda ns: ns.post_errors.update({1: requests.HTTPError("403")}), id="post-http-error"),
    ],
)
def test_failed_pull_is_logged_and_not_recorded_while_run_continues(
    fakes: SimpleNamespace,
    mocker: MockerFixture,
    caplog: pytest.LogCaptureFixture,
    configure: Callable[[SimpleNamespace], None],
) -> None:
    _with_write_token(mocker)
    configure(fakes)
    assert main(_argv(fakes, "--post")) == EXIT_FAILED
    assert set(load_state(fakes.state_file)) == {state_key(REPO, 2), state_key(REPO, 3)}
    assert f"{REPO}#1 not recorded" in caplog.text
