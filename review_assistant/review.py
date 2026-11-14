# Copyright SUSE LLC
# SPDX-License-Identifier: MIT
"""Checks out a pull request, runs the Hermes review agent over it, and lints the review it drafts."""

import subprocess  # noqa: S404 - git, hermes and python3 are external interfaces, argv only, no shell
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from review_assistant.config import ReviewerConfig
from review_assistant.secret_filter import holds_secret, redact

PLAYBOOK = "openqa-pr-review"
LINT_SCRIPT = Path("scripts") / "review-lint.py"
GIT = ("git", "-c", "core.hooksPath=/dev/null")

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
class Workspace:
    """Where a review runs: the pinned skill, the scratch root, the openQA clone, and the values never to echo."""

    skill_root: Path
    scratch_root: Path
    clone: Path
    secrets: tuple[str, ...] = ()


@dataclass(frozen=True)
class Scratch:
    """Per-PR scratch directory holding the inputs and the drafted review."""

    directory: Path
    worktree: Path
    review: Path


def run_command(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run argv without a shell and return its completed process."""
    return subprocess.run(argv, check=False, capture_output=True, text=True)  # noqa: S603 - argv only, no shell


def _check(result: subprocess.CompletedProcess[str], step: str) -> None:
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        msg = f"{step} exited {result.returncode}: {detail}"
        raise ReviewError(msg)


def ensure_clone(url: str, path: Path, runner: CommandRunner = run_command) -> None:
    """Clone url into path when path is not already a clone, so the PR heads can be fetched there."""
    if (path / ".git").is_dir():
        return
    _check(runner([*GIT, "clone", "--quiet", "--depth", "1", url, str(path)]), "git clone")


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


def checkout_pr(clone: Path, pr: PullRequest, scratch: Scratch, runner: CommandRunner) -> None:
    """Create a detached read-only worktree of the PR head, failing unless it is exactly head_sha."""
    refname = f"refs/review-assistant/pr-{pr.number}"
    _check(
        runner([*GIT, "-C", str(clone), "fetch", "--quiet", "origin", f"+refs/pull/{pr.number}/head:{refname}"]),
        "git fetch",
    )
    _check(
        runner([*GIT, "-C", str(clone), "worktree", "add", "--detach", str(scratch.worktree), refname]), "git worktree"
    )
    head = runner([*GIT, "-C", str(scratch.worktree), "rev-parse", "HEAD"])
    _check(head, "git rev-parse")
    if head.stdout.strip() != pr.head_sha:
        msg = f"PR #{pr.number} moved: checked out {head.stdout.strip()}, expected {pr.head_sha}"
        raise ReviewError(msg)


def _prompt(pr: PullRequest, skill_root: Path, scratch: Scratch) -> str:
    return (
        f"Review {pr.repo} pull request #{pr.number} with the {PLAYBOOK} playbook of the openQA skill "
        f"at {skill_root}. Inputs: diff {scratch.directory / 'diff.patch'}, PR text {scratch.directory / 'pr.md'}, "
        f"read-only PR-head checkout {scratch.worktree}. Write the review JSON to {scratch.review}. "
        "Post nothing: no review, comment, approval or label."
    )


def agent_env(directory: Path) -> list[str]:
    """Return the argv prefix that confines the agent's file writes to directory."""
    return ["env", f"HERMES_WRITE_SAFE_ROOT={directory}"]


def _hermes_argv(config: ReviewerConfig, prompt: str, directory: Path) -> list[str]:
    return [
        *agent_env(directory),
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


def _review(config: ReviewerConfig, pr: PullRequest, workspace: Workspace, runner: CommandRunner) -> Path:
    scratch = _prepare_scratch(workspace.scratch_root, pr)
    checkout_pr(workspace.clone, pr, scratch, runner)
    _check(runner(_hermes_argv(config, _prompt(pr, workspace.skill_root, scratch), scratch.directory)), "hermes")
    if not scratch.review.is_file():
        msg = f"hermes wrote no review at {scratch.review}"
        raise ReviewError(msg)
    if holds_secret(scratch.review.read_text(encoding="utf-8"), workspace.secrets):
        msg = f"review at {scratch.review} contains a credential"
        raise ReviewError(msg)
    lint_argv = [
        "python3",
        str(workspace.skill_root / LINT_SCRIPT),
        str(scratch.review),
        "--lines",
        str(pr.changed_lines),
    ]
    _check(runner(lint_argv), "review-lint")
    return scratch.review


def review_pull_request(
    config: ReviewerConfig, pr: PullRequest, workspace: Workspace, runner: CommandRunner = run_command
) -> Path:
    """Check out pr, have Hermes draft its review, lint it, and return the review JSON if it holds no credential.

    Errors carry agent-authored output (Hermes' reply, review-lint findings), so they are redacted before they reach
    a log.
    """
    try:
        return _review(config, pr, workspace, runner)
    except ReviewError as error:
        raise ReviewError(redact(str(error), workspace.secrets)) from None
