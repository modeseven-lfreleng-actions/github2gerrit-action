# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from github2gerrit.cli import GitHubPRTarget
from github2gerrit.cli import GitHubRepoTarget
from github2gerrit.cli import _parse_github_target
from github2gerrit.core import GerritInfo
from github2gerrit.core import Orchestrator
from github2gerrit.core import RepoNames


def test_ghe_url_parsing_toggle(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Verify that:
      - By default, GHE (non-github.com) URLs are rejected.
      - When ALLOW_GHE_URLS is set to true, non-github.com URLs are accepted.
      - With ALLOW_GHE_URLS=false, github.com URLs are still accepted.
    """
    ghe_url = "https://ghe.example.org/org/repo/pull/123"
    gh_url = "https://github.com/org/repo/pull/456"

    # Default: reject GHE (env unset -> default False)
    monkeypatch.delenv("ALLOW_GHE_URLS", raising=False)
    assert _parse_github_target(ghe_url) == GitHubRepoTarget(
        owner=None, repo=None
    )

    # Enable GHE: accept non-github.com hosts
    monkeypatch.setenv("ALLOW_GHE_URLS", "true")
    assert _parse_github_target(ghe_url) == GitHubPRTarget(
        owner="org", repo="repo", pr_number=123
    )

    # With ALLOW_GHE_URLS=false, standard github.com URL still parses
    monkeypatch.setenv("ALLOW_GHE_URLS", "false")
    assert _parse_github_target(gh_url) == GitHubPRTarget(
        owner="org", repo="repo", pr_number=456
    )


def test_git_review_args_include_branch_before_reviewers_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    _push_to_gerrit should:
      - include branch as a positional argument before the reviewers
      - expand comma-separated reviewers into one '--reviewers' flag
      - include a topic derived from env and repo name (PR_NUMBER and
      G2G_TOPIC_PREFIX)
    """
    orch = Orchestrator(workspace=tmp_path)

    gerrit = GerritInfo(
        host="gerrit.example.org", port=29418, project="platform/infra"
    )
    repo = RepoNames(project_gerrit="platform/infra", project_github="my-repo")
    branch = "main"
    reviewers = "alice@example.org, bob@example.org"

    # Control topic formation via environment
    monkeypatch.setenv("PR_NUMBER", "42")
    monkeypatch.setenv("G2G_TOPIC_PREFIX", "GH")

    recorded_cmds: list[list[str]] = []

    def fake_run_cmd(cmd: list[str], **kwargs: Any) -> None:
        # Record all commands; we care about the 'git review' invocation
        recorded_cmds.append(list(cmd))

    monkeypatch.setattr(
        "github2gerrit.core.run_cmd", fake_run_cmd, raising=True
    )

    # Act (single_commits=False to avoid extra checkout step)
    orch._push_to_gerrit(
        gerrit=gerrit,
        repo=repo,
        branch=branch,
        reviewers=reviewers,
        single_commits=False,
    )

    # Find the git-review invocation
    review_calls = [
        c
        for c in recorded_cmds
        if len(c) >= 2 and c[0] == "git" and c[1] == "review"
    ]
    assert review_calls, (
        f"No git-review invocation captured in: {recorded_cmds!r}"
    )
    args = review_calls[-1]

    # Basic structure
    assert args[0:2] == ["git", "review"]

    # Topic flag present with expected composition
    assert "-t" in args
    t_idx = args.index("-t")
    assert args[t_idx + 1] == "GH-my-repo-42"

    # Branch is passed as a positional argument *before* the reviewers, so
    # git-review's nargs='+' '--reviewers' cannot swallow it.
    assert branch in args
    assert "--reviewers" in args
    assert args.index(branch) < args.index("--reviewers")

    # A single '--reviewers' flag carries every reviewer; repeating the flag
    # would make argparse keep only the last occurrence.
    assert args.count("--reviewers") == 1
    assert "--reviewer" not in args
    rev_idx = args.index("--reviewers")
    assert args[rev_idx + 1 :] == ["alice@example.org", "bob@example.org"]


def _parse_like_git_review(args: list[str]) -> Any:
    """Parse argv with git-review's own grammar (see git_review/cmd.py)."""
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--reviewers", nargs="+", default=[])
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("-v", action="store_true")
    parser.add_argument("-t", "--topic")
    parser.add_argument("branch", nargs="?")
    return parser.parse_args(args[2:])


def test_git_review_args_parse_back_to_the_intended_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The emitted argv must survive git-review's own argparse grammar.

    Regression guard for Gerrit changes raised from stable/* pull requests
    landing on master: git-review declares 'branch' as a trailing positional
    and '--reviewers' with nargs='+', so argument order alone decides which
    branch the change is pushed to.
    """
    orch = Orchestrator(workspace=tmp_path)
    branch = "stable/vanadium"

    monkeypatch.setenv("PR_NUMBER", "39")
    monkeypatch.setenv("G2G_TOPIC_PREFIX", "GH")
    recorded_cmds: list[list[str]] = []
    monkeypatch.setattr(
        "github2gerrit.core.run_cmd",
        lambda cmd, **kwargs: recorded_cmds.append(list(cmd)),
        raising=True,
    )

    orch._push_to_gerrit(
        gerrit=GerritInfo(
            host="git.opendaylight.org", port=29418, project="openflowplugin"
        ),
        repo=RepoNames(
            project_gerrit="openflowplugin", project_github="openflowplugin"
        ),
        branch=branch,
        reviewers="alice@example.org, bob@example.org",
        single_commits=False,
    )

    args = next(c for c in recorded_cmds if c[0:2] == ["git", "review"])
    parsed = _parse_like_git_review(args)

    assert parsed.branch == branch
    assert parsed.topic == "GH-openflowplugin-39"
    assert parsed.reviewers == ["alice@example.org", "bob@example.org"]
    assert branch not in parsed.reviewers


def test_git_review_retry_keeps_the_target_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The normalized-email retry must target the same branch as the push."""
    orch = Orchestrator(workspace=tmp_path)
    branch = "stable/vanadium"

    recorded_cmds: list[list[str]] = []
    monkeypatch.setattr(
        "github2gerrit.core.run_cmd",
        lambda cmd, **kwargs: recorded_cmds.append(list(cmd)),
        raising=True,
    )

    assert orch._retry_push_with_normalized_emails(
        ["git", "review", "--yes", "-v", "-t", "GH-openflowplugin-39"],
        "Alice@Example.ORG, bob@example.org",
        branch,
        {},
        ["Alice@Example.ORG"],
    )

    parsed = _parse_like_git_review(recorded_cmds[-1])
    assert parsed.branch == branch
    assert parsed.topic == "GH-openflowplugin-39"
    assert branch not in parsed.reviewers
