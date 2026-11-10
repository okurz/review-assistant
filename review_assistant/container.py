# Copyright SUSE LLC
# SPDX-License-Identifier: MIT
"""Host-side builder of the rootless podman run for one review pass; it holds paths, never credential values."""

import argparse
import subprocess  # noqa: S404 - podman is an external interface, argv only, no shell
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

IMAGE = "review-assistant:dev"
CONTAINER_CONFIG = "/config/reviewer.yaml"
CONTAINER_STATE = "/state/state.json"
CONTAINER_SKILL_CACHE = "/cache/skills"
CONTAINER_GH_READ_PATH = "/run/secrets/gh-read-token"
CONTAINER_GCLOUD_DIR = "/run/secrets/gcloud"
CONTAINER_ADC = f"{CONTAINER_GCLOUD_DIR}/application_default_credentials.json"
CONTAINER_HERMES_HOME = "/hermes"
CONTAINER_HERMES_CONFIG = f"{CONTAINER_HERMES_HOME}/config.yaml"


@dataclass(frozen=True)
class HostPaths:
    """Host paths mounted into the container; the secrets among them stay outside the repository."""

    adc_file: Path
    read_token_file: Path
    env_file: Path
    config_file: Path
    hermes_config_file: Path
    state_file: Path
    skill_cache: Path
    image: str = IMAGE


def build_run_argv(paths: HostPaths, review_args: Sequence[str]) -> list[str]:
    """Return the podman argv that runs one review pass in a fresh container."""
    return [
        "podman",
        "run",
        "--rm",
        "--userns=keep-id",
        "--read-only",
        "--tmpfs=/tmp",
        f"--tmpfs={CONTAINER_HERMES_HOME}:mode=1777",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--env-file",
        str(paths.env_file),
        "-e",
        "HOME=/tmp",
        "-e",
        f"HERMES_HOME={CONTAINER_HERMES_HOME}",
        "-e",
        f"CLOUDSDK_CONFIG={CONTAINER_GCLOUD_DIR}",
        "-v",
        f"{paths.adc_file}:{CONTAINER_ADC}:ro",
        "-v",
        f"{paths.read_token_file}:{CONTAINER_GH_READ_PATH}:ro",
        "-v",
        f"{paths.config_file}:{CONTAINER_CONFIG}:ro",
        "-v",
        f"{paths.hermes_config_file}:{CONTAINER_HERMES_CONFIG}:ro",
        "-v",
        f"{paths.state_file}:{CONTAINER_STATE}:rw",
        "-v",
        f"{paths.skill_cache}:{CONTAINER_SKILL_CACHE}:rw",
        paths.image,
        "run",
        "--config",
        CONTAINER_CONFIG,
        "--read-token-file",
        CONTAINER_GH_READ_PATH,
        "--state-file",
        CONTAINER_STATE,
        "--skill-cache",
        CONTAINER_SKILL_CACHE,
        *review_args,
    ]


def default_host_paths(repo: Path) -> HostPaths:
    """Return the host paths of this installation: secrets under ~/.config, state under ~/.local/state."""
    home = Path.home()
    config_home = home / ".config" / "review-assistant"
    return HostPaths(
        adc_file=home / ".config" / "gcloud" / "application_default_credentials.json",
        read_token_file=config_home / "read-token",
        env_file=config_home / "write.env",
        config_file=repo / "config" / "reviewer.yaml",
        hermes_config_file=repo / "deploy" / "hermes" / "config.yaml",
        state_file=home / ".local" / "state" / "review-assistant" / "state.json",
        skill_cache=home / ".cache" / "review-assistant" / "skills",
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run one review pass in a container and return podman's exit code."""
    parser = argparse.ArgumentParser(prog="review-assistant-container")
    parser.add_argument("--post", action="store_true", help="post the reviews instead of a dry run")
    parser.add_argument("--limit", type=int, help="new reviews per run")
    args = parser.parse_args(argv)
    review_args = ["--post"] if args.post else []
    if args.limit is not None:
        review_args += ["--limit", str(args.limit)]
    repo = Path(__file__).resolve().parents[1]
    argv_podman = build_run_argv(default_host_paths(repo), review_args)
    return subprocess.run(argv_podman, check=False).returncode  # noqa: S603 - argv built here, no shell
