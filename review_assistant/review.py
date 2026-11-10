# Copyright SUSE LLC
# SPDX-License-Identifier: MIT
"""Runs the Hermes review agent over one pull request and lints the review it drafts."""

import subprocess  # noqa: S404 - hermes and python3 are external interfaces, argv only, no shell
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from review_assistant.config import ReviewerConfig

PLAYBOOK = "openqa-pr-review"
LINT_SCRIPT = Path("scripts") / "review-lint.py"

type CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


class ReviewError(RuntimeError):
    """The review agent failed, or its review did not pass review-lint."""


@dataclass(frozen=True)
class PullRequest:
    """Pull request data the review agent works from."""

    repo: str
    number: int
    head_sha: str
    title: str
    body: str
    diff: str
    changed_lines: int


@dataclass(frozen=True)
class Scratch:
    """Per-PR scratch directory holding the inputs and the drafted review."""

    directory: Path
    worktree: Path
    review: Path


def run_command(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run argv without a shell and return its completed process."""
    return subprocess.run(argv, check=False, capture_output=True, text=True)  # noqa: S603 - argv only, no shell


def _prepare_scratch(root: Path, pr: PullRequest) -> Scratch:
    directory = root / f"pr-{pr.number}"
    directory.mkdir(parents=True, exist_ok=True)
    scratch = Scratch(
        directory=directory,
        worktree=directory / "worktree",
        review=directory / f"review-{pr.number}.json",
    )
    scratch.review.unlink(missing_ok=True)
    (directory / "diff.patch").write_text(pr.diff, encoding="utf-8")
    (directory / "pr.md").write_text(f"# {pr.title}\n\n{pr.body}\n", encoding="utf-8")
    return scratch


def _prompt(pr: PullRequest, skill_root: Path, scratch: Scratch) -> str:
    return (
        f"Review {pr.repo} pull request #{pr.number} with the {PLAYBOOK} playbook of the openQA skill "
        f"at {skill_root}. Inputs: diff {scratch.directory / 'diff.patch'}, PR text {scratch.directory / 'pr.md'}, "
        f"read-only PR-head worktree path {scratch.worktree}. Write the review JSON to {scratch.review} "
        f"and lint it with python3 {skill_root / LINT_SCRIPT} {scratch.review} --lines {pr.changed_lines} "
        "until it exits 0. Post nothing: no review, comment, approval or label."
    )


def _hermes_argv(config: ReviewerConfig, prompt: str, directory: Path) -> list[str]:
    return [
        "hermes",
        "-z",
        prompt,
        "--provider",
        config.provider,
        "-m",
        config.model,
        "--in",
        str(directory),
        "-t",
        config.toolsets,
    ]


def _check(result: subprocess.CompletedProcess[str], step: str) -> None:
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        msg = f"{step} exited {result.returncode}: {detail}"
        raise ReviewError(msg)


def review_pull_request(
    config: ReviewerConfig,
    pr: PullRequest,
    skill_root: Path,
    scratch_root: Path,
    runner: CommandRunner = run_command,
) -> Path:
    """Draft the review of pr with Hermes, lint it, and return the path of the passing review JSON."""
    scratch = _prepare_scratch(scratch_root, pr)
    _check(runner(_hermes_argv(config, _prompt(pr, skill_root, scratch), scratch.directory)), "hermes")
    if not scratch.review.is_file():
        msg = f"hermes wrote no review at {scratch.review}"
        raise ReviewError(msg)
    lint_argv = ["python3", str(skill_root / LINT_SCRIPT), str(scratch.review), "--lines", str(pr.changed_lines)]
    _check(runner(lint_argv), "review-lint")
    return scratch.review
