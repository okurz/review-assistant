# Copyright SUSE LLC
# SPDX-License-Identifier: MIT
"""Tests for the review runner, with a fake runner in place of git, Hermes and review-lint."""

import subprocess  # noqa: S404 - builds CompletedProcess fixtures only
from collections.abc import Sequence
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from review_assistant.config import load_config
from review_assistant.review import PullRequest, ReviewError, Workspace, review_pull_request, run_command

EXAMPLE = Path(__file__).parent.parent / "config" / "reviewer.example.yaml"
CONFIG = load_config(EXAMPLE)
SKILL_ROOT = Path("/cache/skill/skills/openqa")
CLONE = Path("/cache/openQA")
NUMBER = 8079
HEAD = "a6d255abcbce67a380f6716c94e861869ed64882"
HOOKLESS_GIT = ["git", "-c", "core.hooksPath=/dev/null"]
PR = PullRequest(
    repo="os-autoinst/openQA",
    number=NUMBER,
    head_sha=HEAD,
    title="Fix retry handling",
    body="Retries were unbounded.",
    diff="diff --git a/lib/x.pm b/lib/x.pm\n",
    changed_lines=42,
)


class _FakeRunner:
    def __init__(
        self,
        *,
        hermes_rc: int = 0,
        lint_rc: int = 0,
        lint_out: str = "",
        write_review: bool = True,
        head: str = HEAD,
        git_rc: int = 0,
    ) -> None:
        self.hermes_rc = hermes_rc
        self.lint_rc = lint_rc
        self.lint_out = lint_out
        self.write_review = write_review
        self.head = head
        self.git_rc = git_rc
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        if argv[0] == "env":
            if self.write_review and self.hermes_rc == 0:
                (Path(argv[argv.index("--in") + 1]) / f"review-{NUMBER}.json").write_text("{}", encoding="utf-8")
            return subprocess.CompletedProcess(argv, self.hermes_rc, stdout="", stderr="hermes failed")
        if argv[0] == "python3":
            return subprocess.CompletedProcess(argv, self.lint_rc, stdout=self.lint_out, stderr="")
        if "rev-parse" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=f"{self.head}\n", stderr="")
        return subprocess.CompletedProcess(argv, self.git_rc, stdout="", stderr="git failed")

    def hermes_argv(self) -> list[str]:
        return next(call for call in self.calls if call[0] == "env")


def _review(tmp_path: Path, runner: _FakeRunner) -> Path:
    return review_pull_request(CONFIG, PR, Workspace(SKILL_ROOT, tmp_path, CLONE), runner)


def test_pr_head_is_fetched_by_pull_ref_and_checked_out_detached(tmp_path: Path) -> None:
    runner = _FakeRunner()
    _review(tmp_path, runner)
    worktree = tmp_path / f"pr-{NUMBER}" / "worktree"
    assert runner.calls[0] == [
        *HOOKLESS_GIT,
        "-C",
        str(CLONE),
        "fetch",
        "--quiet",
        "origin",
        f"+refs/pull/{NUMBER}/head:refs/review-assistant/pr-{NUMBER}",
    ]
    assert runner.calls[1] == [
        *HOOKLESS_GIT,
        "-C",
        str(CLONE),
        "worktree",
        "add",
        "--detach",
        str(worktree),
        f"refs/review-assistant/pr-{NUMBER}",
    ]
    assert runner.calls[2] == [*HOOKLESS_GIT, "-C", str(worktree), "rev-parse", "HEAD"]


def test_moved_pr_head_is_rejected_before_review(tmp_path: Path) -> None:
    with pytest.raises(ReviewError, match="moved: checked out"):
        _review(tmp_path, _FakeRunner(head="b" * 40))


def test_git_failure_is_rejected_before_review(tmp_path: Path) -> None:
    runner = _FakeRunner(git_rc=128)
    with pytest.raises(ReviewError, match="git fetch exited 128"):
        _review(tmp_path, runner)
    assert all("hermes" not in call for call in runner.calls)


def test_hermes_gets_only_provider_model_scratch_and_toolsets(tmp_path: Path) -> None:
    runner = _FakeRunner()
    _review(tmp_path, runner)
    argv = runner.hermes_argv()
    assert argv[0:4] == ["env", f"HERMES_WRITE_SAFE_ROOT={tmp_path / f'pr-{NUMBER}'}", "hermes", "-z"]
    assert argv[5:] == [
        "--provider",
        "google-vertex",
        "-m",
        "google/gemini-3.8-flash",
        "--in",
        str(tmp_path / f"pr-{NUMBER}"),
        "-t",
        "file",
    ]


@pytest.mark.parametrize(
    "expected",
    [
        pytest.param("openqa-pr-review", id="playbook-named"),
        pytest.param(str(SKILL_ROOT), id="skill-root-given"),
        pytest.param(f"review-{NUMBER}.json", id="review-path-given"),
        pytest.param("Post nothing", id="no-posting-instruction"),
    ],
)
def test_prompt_names_playbook_and_paths(tmp_path: Path, expected: str) -> None:
    runner = _FakeRunner()
    _review(tmp_path, runner)
    assert expected in runner.hermes_argv()[4]


def test_prompt_does_not_ask_the_agent_to_run_commands(tmp_path: Path) -> None:
    runner = _FakeRunner()
    _review(tmp_path, runner)
    prompt = runner.hermes_argv()[4]
    assert "review-lint" not in prompt
    assert "run " not in prompt


def test_scratch_holds_diff_and_pr_text(tmp_path: Path) -> None:
    _review(tmp_path, _FakeRunner())
    scratch = tmp_path / f"pr-{NUMBER}"
    assert (scratch / "diff.patch").read_text(encoding="utf-8") == PR.diff
    assert (scratch / "pr.md").read_text(encoding="utf-8") == "# Fix retry handling\n\nRetries were unbounded.\n"


def test_passing_review_is_linted_in_python_with_changed_lines(tmp_path: Path) -> None:
    runner = _FakeRunner()
    review = _review(tmp_path, runner)
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
        _review(tmp_path, runner)


def test_stale_review_from_earlier_run_is_not_accepted(tmp_path: Path) -> None:
    scratch = tmp_path / f"pr-{NUMBER}"
    scratch.mkdir()
    (scratch / f"review-{NUMBER}.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ReviewError, match="hermes wrote no review"):
        _review(tmp_path, _FakeRunner(write_review=False))


def test_run_command_executes_argv_without_shell(mocker: MockerFixture) -> None:
    completed = subprocess.CompletedProcess(["hermes", "-z"], 0, stdout="OK", stderr="")
    run = mocker.patch("review_assistant.review.subprocess.run", return_value=completed)
    assert run_command(["hermes", "-z"]) is completed
    run.assert_called_once_with(["hermes", "-z"], check=False, capture_output=True, text=True)
