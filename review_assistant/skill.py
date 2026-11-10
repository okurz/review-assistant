# Copyright SUSE LLC
# SPDX-License-Identifier: MIT
"""Pinned openQA-skill checkout used as the review instructions."""

import subprocess  # noqa: S404 - git is an external interface, argv only, no shell
from pathlib import Path

from review_assistant.config import SkillConfig


class SkillCheckoutError(RuntimeError):
    """The checked-out skill does not match the pinned commit."""


def _git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)  # noqa: S603, S607 - fixed git argv, resolved from PATH
    return result.stdout.strip()


def checkout_skill(skill: SkillConfig, cache_root: Path) -> Path:
    """Clone or refresh the skill under cache_root at the pinned commit and return its path."""
    target = cache_root / skill.sha
    if (target / ".git").is_dir():
        _git("fetch", "--quiet", "origin", cwd=target)
    else:
        _git("clone", "--quiet", skill.repo, str(target))
    _git("checkout", "--quiet", "--detach", skill.sha, cwd=target)
    head = _git("rev-parse", "HEAD", cwd=target)
    if not head.startswith(skill.sha):
        msg = f"openQA-skill HEAD {head} does not match pinned {skill.sha}"
        raise SkillCheckoutError(msg)
    return target
