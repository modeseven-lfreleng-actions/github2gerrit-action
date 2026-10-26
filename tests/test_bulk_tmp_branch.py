# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Each orchestrator keeps its own temporary branch.

Bulk mode runs one orchestrator per pull request on threads of a single
process. The temporary branch name travelled through ``G2G_TMP_BRANCH``,
one process-wide variable, so a pull request's push and cleanup could
act on the branch another pull request had named since (#502).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from github2gerrit.core import GerritInfo
from github2gerrit.core import Orchestrator
from github2gerrit.core import RepoNames
from github2gerrit.gitutils import CommandResult
from github2gerrit.models import GitHubContext


GERRIT = GerritInfo(
    host="gerrit.example.org", port=29418, project="example/project"
)


class _Stop(Exception):
    """End a pipeline step once the behaviour under test has happened."""


def _ctx(pr_number: int) -> GitHubContext:
    return GitHubContext(
        event_name="workflow_dispatch",
        event_action="",
        event_path=None,
        repository="example/project",
        repository_owner="example",
        server_url="https://github.com",
        run_id="1",
        sha="0" * 40,
        base_ref="master",
        head_ref=f"feature/{pr_number}",
        pr_number=pr_number,
    )


def _tmp_branch(pr_number: int) -> str:
    return f"g2g_tmp_{pr_number}_{os.getpid()}"


@pytest.fixture(autouse=True)
def _environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CI_TESTING", raising=False)
    monkeypatch.delenv("GERRIT_BRANCH", raising=False)
    # A stale value must not reach the code under test.
    monkeypatch.delenv("G2G_TMP_BRANCH", raising=False)


@pytest.fixture
def git_calls(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Record git commands; stop a push at git-review."""
    calls: list[list[str]] = []

    def fake(cmd: list[str], **_kwargs: Any) -> CommandResult:
        calls.append(list(cmd))
        if cmd[:2] == ["git", "review"]:
            raise _Stop
        stdout = "c0ffee\n" if cmd[:2] == ["git", "rev-list"] else ""
        return CommandResult(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr("github2gerrit.core.run_cmd", fake)
    return calls


def _prepared(tmp_path: Path, pr_number: int, mode: str) -> Orchestrator:
    """Run commit preparation until it has named its temporary branch."""
    orch = Orchestrator(workspace=tmp_path / str(pr_number))
    prepare = (
        orch._prepare_single_commits
        if mode == "single"
        else orch._prepare_squashed_commit
    )
    with (
        patch.multiple(
            orch,
            _analyze_merge_situation=MagicMock(),
            _checkout_with_unshallow_fallback=MagicMock(side_effect=_Stop),
        ),
        pytest.raises(_Stop),
    ):
        prepare(MagicMock(), _ctx(pr_number), GERRIT)
    return orch


@pytest.mark.parametrize("mode", ["single", "squash"])
class TestTemporaryBranchIsolation:
    def test_cleanup_deletes_its_own_branch(
        self, tmp_path: Path, git_calls: list[list[str]], mode: str
    ) -> None:
        first = _prepared(tmp_path, 39, mode)
        _prepared(tmp_path, 40, mode)
        git_calls.clear()
        first._cleanup_tmp_branch("master", {})
        assert ["git", "branch", "-D", _tmp_branch(39)] in git_calls
        assert ["git", "branch", "-D", _tmp_branch(40)] not in git_calls

    def test_single_commit_push_checks_out_its_own_branch(
        self, tmp_path: Path, git_calls: list[list[str]], mode: str
    ) -> None:
        first = _prepared(tmp_path, 39, mode)
        _prepared(tmp_path, 40, mode)
        git_calls.clear()
        with pytest.raises(_Stop):
            first._push_to_gerrit(
                gerrit=GERRIT,
                repo=RepoNames(
                    project_gerrit="example/project",
                    project_github="project",
                ),
                branch="master",
                reviewers="",
                single_commits=True,
                gh=_ctx(39),
            )
        assert git_calls[0] == ["git", "checkout", _tmp_branch(39)]

    def test_preparation_leaves_the_environment_alone(
        self, tmp_path: Path, git_calls: list[list[str]], mode: str
    ) -> None:
        _prepared(tmp_path, 39, mode)
        assert "G2G_TMP_BRANCH" not in os.environ
