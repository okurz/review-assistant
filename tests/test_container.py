# Copyright SUSE LLC
# SPDX-License-Identifier: MIT
"""Tests for the podman argv of one review pass: hardening, mount modes and absence of credential values."""

import subprocess  # noqa: S404 - builds CompletedProcess fixtures only
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from review_assistant.container import IMAGE, HostPaths, build_run_argv, default_host_paths, main

REPO = Path(__file__).resolve().parents[1]


def _paths(tmp_path: Path) -> HostPaths:
    return HostPaths(
        adc_file=tmp_path / "adc.json",
        read_token_file=tmp_path / "read-token",
        env_file=tmp_path / "write.env",
        config_file=tmp_path / "reviewer.yaml",
        hermes_config_file=tmp_path / "config.yaml",
        state_file=tmp_path / "state.json",
        skill_cache=tmp_path / "skills",
    )


def _mounts(argv: list[str]) -> dict[str, str]:
    values = [argv[index + 1] for index, arg in enumerate(argv) if arg == "-v"]
    return {value.split(":")[1]: value.split(":")[2] for value in values}


@pytest.mark.parametrize(
    "flag",
    [
        pytest.param("--rm", id="removed-after-pass"),
        pytest.param("--userns=keep-id", id="host-user-owns-mounts"),
        pytest.param("--read-only", id="read-only-root"),
        pytest.param("--tmpfs=/tmp", id="writable-tmp"),
        pytest.param("--tmpfs=/hermes:mode=1777", id="writable-hermes-home"),
        pytest.param("--cap-drop=ALL", id="no-capabilities"),
        pytest.param("--security-opt=no-new-privileges", id="no-privilege-escalation"),
    ],
)
def test_run_is_hardened(tmp_path: Path, flag: str) -> None:
    assert flag in build_run_argv(_paths(tmp_path), [])


@pytest.mark.parametrize(
    ("container_path", "mode"),
    [
        pytest.param("/run/secrets/gcloud/application_default_credentials.json", "ro", id="adc-read-only"),
        pytest.param("/run/secrets/gh-read-token", "ro", id="read-token-read-only"),
        pytest.param("/config/reviewer.yaml", "ro", id="config-read-only"),
        pytest.param("/hermes/config.yaml", "ro", id="hermes-config-read-only"),
        pytest.param("/state/state.json", "rw", id="state-writable"),
        pytest.param("/cache/skills", "rw", id="skill-cache-writable"),
    ],
)
def test_mount_modes(tmp_path: Path, container_path: str, mode: str) -> None:
    assert _mounts(build_run_argv(_paths(tmp_path), []))[container_path] == mode


def test_adc_is_found_through_gcloud_config_not_as_service_account_key(tmp_path: Path) -> None:
    argv = build_run_argv(_paths(tmp_path), [])
    envs = [argv[index + 1] for index, arg in enumerate(argv) if arg == "-e"]
    assert "CLOUDSDK_CONFIG=/run/secrets/gcloud" in envs
    assert not any(env.startswith("GOOGLE_APPLICATION_CREDENTIALS=") for env in envs)


def test_write_token_is_passed_only_as_env_file_path(tmp_path: Path) -> None:
    argv = build_run_argv(_paths(tmp_path), ["--post"])
    assert argv[argv.index("--env-file") + 1] == str(tmp_path / "write.env")
    assert not any("REVIEW_ASSISTANT_GH_TOKEN" in arg for arg in argv)
    assert not any("github_pat_" in arg or "ghp_" in arg for arg in argv)


def test_review_arguments_follow_image(tmp_path: Path) -> None:
    argv = build_run_argv(_paths(tmp_path), ["--post", "--limit", "3"])
    assert argv[argv.index(IMAGE) :] == [
        IMAGE,
        "run",
        "--config",
        "/config/reviewer.yaml",
        "--read-token-file",
        "/run/secrets/gh-read-token",
        "--state-file",
        "/state/state.json",
        "--skill-cache",
        "/cache/skills",
        "--post",
        "--limit",
        "3",
    ]


@pytest.mark.parametrize(
    "field",
    [
        pytest.param("adc_file", id="adc"),
        pytest.param("read_token_file", id="read-token"),
        pytest.param("env_file", id="write-env"),
    ],
)
def test_secrets_are_outside_the_repository(field: str) -> None:
    assert not getattr(default_host_paths(REPO), field).is_relative_to(REPO)


def test_main_returns_podman_exit_code_and_passes_review_flags(mocker: MockerFixture) -> None:
    run = mocker.patch("review_assistant.container.subprocess.run", return_value=subprocess.CompletedProcess([], 4))
    assert main(["--post", "--limit", "2"]) == 4
    argv = run.call_args.args[0]
    assert argv[0:2] == ["podman", "run"]
    assert argv[-3:] == ["--post", "--limit", "2"]


def test_main_without_post_runs_a_dry_run(mocker: MockerFixture) -> None:
    run = mocker.patch("review_assistant.container.subprocess.run", return_value=subprocess.CompletedProcess([], 0))
    assert main([]) == 0
    assert "--post" not in run.call_args.args[0]
