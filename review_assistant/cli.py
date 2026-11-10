# Copyright SUSE LLC
# SPDX-License-Identifier: MIT
"""Command line entry point: review open pull requests with Hermes, post only with --post."""

import argparse
import json
import logging
import os
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pydantic
import requests

from review_assistant.config import ConfigError, ReviewerConfig, load_config
from review_assistant.github import (
    GitHubError,
    PullSummary,
    ReviewDraft,
    fetch_pull_request,
    list_open_pulls,
    post_review,
    read_token_file,
    take_write_token,
)
from review_assistant.hardening import HardeningError, make_non_dumpable
from review_assistant.review import ReviewError, Workspace, review_pull_request
from review_assistant.skill import SkillCheckoutError, checkout_skill
from review_assistant.state import ReviewRecord, State, is_reviewed, load_state, save_state, state_key

REPO = "os-autoinst/openQA"
SKILL_SUBDIR = Path("skills") / "openqa"
DEFAULT_CONFIG = Path("config/reviewer.yaml")
DEFAULT_STATE = Path.home() / ".local/state/review-assistant/state.json"
DEFAULT_SKILL_CACHE = Path.home() / ".cache/review-assistant/skills"
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
PER_PULL_ERRORS = (ReviewError, requests.RequestException, json.JSONDecodeError, pydantic.ValidationError)

log = logging.getLogger(__name__)


@dataclass
class _Session:
    config: ReviewerConfig
    read_token: str
    post_token: str | None
    skill_root: Path
    clone: Path
    scratch_root: Path
    state_file: Path
    state: State


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="review-assistant")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="review open pull requests of os-autoinst/openQA")
    run.add_argument("--post", action="store_true", help="post the reviews; dry-run prints them instead")
    run.add_argument("--limit", type=int, help="new reviews per run (default: pilot_cap of the config)")
    run.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="reviewer configuration file")
    run.add_argument("--read-token-file", type=Path, required=True, help="file holding the read-only GitHub token")
    run.add_argument("--state-file", type=Path, default=DEFAULT_STATE, help="local record of posted reviews")
    run.add_argument("--skill-cache", type=Path, default=DEFAULT_SKILL_CACHE, help="directory for the pinned skill")
    run.add_argument("--clone", type=Path, required=True, help="openQA clone whose fetches give the PR heads")
    return parser


def _review_one(session: _Session, summary: PullSummary) -> None:
    pull = fetch_pull_request(session.read_token, REPO, summary.number)
    workspace = Workspace(session.skill_root, session.scratch_root, session.clone)
    review_path = review_pull_request(session.config, pull, workspace)
    review = json.loads(review_path.read_text(encoding="utf-8"))
    ReviewDraft.model_validate(review)
    if review.get("commit_id") != pull.head_sha:
        msg = f"review commit_id {review.get('commit_id')} is not the checked head {pull.head_sha}"
        raise ReviewError(msg)
    if session.post_token is None:
        sys.stdout.write(f"# {REPO}#{pull.number} at {pull.head_sha}\n{review_path.read_text(encoding='utf-8')}\n")
        return
    review_id = post_review(session.post_token, REPO, pull.number, review)
    session.state[state_key(REPO, pull.number)] = ReviewRecord(head_sha=pull.head_sha, review_id=review_id)
    save_state(session.state_file, session.state)
    log.info("posted review %s on %s#%s at %s", review_id, REPO, pull.number, pull.head_sha)


def _review_pulls(session: _Session, pulls: Sequence[PullSummary], limit: int) -> int:
    todo = [pull for pull in pulls if not is_reviewed(session.state, REPO, pull.number, pull.head_sha)][:limit]
    failures = 0
    for summary in todo:
        try:
            _review_one(session, summary)
        except PER_PULL_ERRORS as error:
            log.warning("%s#%s not recorded: %s", REPO, summary.number, error)
            failures += 1
    return EXIT_FAILED if failures else EXIT_OK


def _post_token(*, post: bool) -> str | None:
    token = take_write_token(os.environ)
    if post and not token:
        msg = "REVIEW_ASSISTANT_GH_TOKEN is not set; --post needs it"
        raise GitHubError(msg)
    return token if post else None


def _run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    post_token = _post_token(post=args.post)
    limit = args.limit if args.limit is not None else config.pilot_cap
    skill_root = checkout_skill(config.skill, args.skill_cache) / SKILL_SUBDIR
    read_token = read_token_file(args.read_token_file)
    pulls = list_open_pulls(read_token, REPO)
    with tempfile.TemporaryDirectory(prefix="review-assistant-") as scratch_root:
        session = _Session(
            config=config,
            read_token=read_token,
            post_token=post_token,
            skill_root=skill_root,
            clone=args.clone,
            scratch_root=Path(scratch_root),
            state_file=args.state_file,
            state=load_state(args.state_file),
        )
        return _review_pulls(session, pulls, limit)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the review CLI and return its exit code."""
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        make_non_dumpable()
        return _run(args)
    except (GitHubError, HardeningError, ConfigError, SkillCheckoutError, ReviewError) as error:
        sys.stderr.write(f"error: {error}\n")
        return EXIT_USAGE
