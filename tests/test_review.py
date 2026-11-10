# Copyright SUSE LLC
# SPDX-License-Identifier: MIT
"""Tests for the Hermes review runner, with a fake runner in place of Hermes and review-lint."""

import subprocess  # noqa: S404 - builds CompletedProcess fixtures only
from collections.abc import Sequence
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from review_assistant.config import load_config
from review_assistant.review import PullRequest, ReviewError, review_pull_request, run_command

EXAMPLE = Path(__file__).parent.parent / "config" / "reviewer.example.yaml"
CONFIG = load_config(EXAMPLE)
SKILL_ROOT = Path("/cache/skill/skills/openqa")
NUMBER = 8079
PR = PullRequest(
    repo="os-autoinst/openQA",
    number=NUMBER,
    title="Fix retry handling",
    body="Retries were unbounded.",
    diff="diff --git a/lib/x.pm b/lib/x.pm\n",
    changed_lines=42,
)


class _FakeRunner:
    def __init__(self, *, hermes_rc: int = 0, lint_rc: int = 0, lint_out: str = "", write_review: bool = True) -> None:
        self.hermes_rc = hermes_rc
        self.lint_rc = lint_rc
        self.lint_out = lint_out
        self.write_review = write_review
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        if argv[0] == "hermes":
            if self.write_review and self.hermes_rc == 0:
                (Path(argv[argv.index("--in") + 1]) / f"review-{NUMBER}.json").write_text("{}", encoding="utf-8")
            return subprocess.CompletedProcess(argv, self.hermes_rc, stdout="", stderr="hermes failed")
        return subprocess.CompletedProcess(argv, self.lint_rc, stdout=self.lint_out, stderr="")

    def hermes_argv(self) -> list[str]:
        return next(call for call in self.calls if call[0] == "hermes")


def test_hermes_is_invoked_with_provider_model_scratch_and_toolsets(tmp_path: Path) -> None:
    runner = _FakeRunner()
    review_pull_request(CONFIG, PR, SKILL_ROOT, tmp_path, runner)
    argv = runner.hermes_argv()
    assert argv[0:2] == ["hermes", "-z"]
    assert argv[3:] == [
        "--provider",
        "google-vertex",
        "-m",
        "gemini-3.8-flash",
        "--in",
        str(tmp_path / f"pr-{NUMBER}"),
        "-t",
        "file,terminal",
    ]


@pytest.mark.parametrize(
    "expected",
    [
        pytest.param("openqa-pr-review", id="playbook-named"),
        pytest.param(str(SKILL_ROOT), id="skill-root-given"),
        pytest.param(f"review-{NUMBER}.json", id="review-path-given"),
        pytest.param("--lines 42", id="changed-lines-given"),
        pytest.param("Post nothing", id="no-posting-instruction"),
    ],
)
def test_prompt_names_playbook_and_paths(tmp_path: Path, expected: str) -> None:
    runner = _FakeRunner()
    review_pull_request(CONFIG, PR, SKILL_ROOT, tmp_path, runner)
    assert expected in runner.hermes_argv()[2]


def test_scratch_holds_diff_and_pr_text(tmp_path: Path) -> None:
    review_pull_request(CONFIG, PR, SKILL_ROOT, tmp_path, _FakeRunner())
    scratch = tmp_path / f"pr-{NUMBER}"
    assert (scratch / "diff.patch").read_text(encoding="utf-8") == PR.diff
    assert (scratch / "pr.md").read_text(encoding="utf-8") == "# Fix retry handling\n\nRetries were unbounded.\n"


def test_passing_review_is_linted_with_changed_lines_and_returned(tmp_path: Path) -> None:
    runner = _FakeRunner()
    review = review_pull_request(CONFIG, PR, SKILL_ROOT, tmp_path, runner)
    assert review == tmp_path / f"pr-{NUMBER}" / f"review-{NUMBER}.json"
    assert runner.calls[-1] == [
        "python3",
        str(SKILL_ROOT / "scripts" / "review-lint.py"),
        str(review),
        "--lines",
        "42",
    ]


@pytest.mark.parametrize(
    ("runner", "message"),
    [
        pytest.param(_FakeRunner(hermes_rc=3), "hermes exited 3: hermes failed", id="hermes-fails"),
        pytest.param(_FakeRunner(write_review=False), "hermes wrote no review", id="no-review-written"),
        pytest.param(
            _FakeRunner(lint_rc=1, lint_out="too many nits"), "review-lint exited 1: too many nits", id="lint-findings"
        ),
        pytest.param(
            _FakeRunner(lint_rc=2, lint_out="not a review"), "review-lint exited 2: not a review", id="lint-usage-error"
        ),
    ],
)
def test_failed_review_is_rejected(tmp_path: Path, runner: _FakeRunner, message: str) -> None:
    with pytest.raises(ReviewError, match=message):
        review_pull_request(CONFIG, PR, SKILL_ROOT, tmp_path, runner)


def test_stale_review_from_earlier_run_is_not_accepted(tmp_path: Path) -> None:
    scratch = tmp_path / f"pr-{NUMBER}"
    scratch.mkdir()
    (scratch / f"review-{NUMBER}.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ReviewError, match="hermes wrote no review"):
        review_pull_request(CONFIG, PR, SKILL_ROOT, tmp_path, _FakeRunner(write_review=False))


def test_run_command_executes_argv_without_shell(mocker: MockerFixture) -> None:
    completed = subprocess.CompletedProcess(["hermes", "-z"], 0, stdout="OK", stderr="")
    run = mocker.patch("review_assistant.review.subprocess.run", return_value=completed)
    assert run_command(["hermes", "-z"]) is completed
    run.assert_called_once_with(["hermes", "-z"], check=False, capture_output=True, text=True)
