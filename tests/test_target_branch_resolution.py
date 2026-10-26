# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""The Gerrit target branch follows the pull request being processed.

Bulk runs process several pull requests at once, on threads of one
process. ``GITHUB_BASE_REF`` is empty there, and could not describe
every pull request even if it were set, so each step that picks a
branch has to take it from the pull request's own context (#502).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from github2gerrit.core import GerritInfo
from github2gerrit.core import Orchestrator
from github2gerrit.core import PreparedChange
from github2gerrit.core import SubmissionResult
from github2gerrit.gitutils import CommandResult
from github2gerrit.models import GitHubContext


STABLE = "stable/vanadium"
GERRIT = GerritInfo(
    host="gerrit.example.org", port=29418, project="example/project"
)


class _Stop(Exception):
    """End a pipeline step once the behaviour under test has happened."""


def _ctx(base_ref: str = STABLE) -> GitHubContext:
    return GitHubContext(
        event_name="workflow_dispatch",
        event_action="",
        event_path=None,
        repository="example/project",
        repository_owner="example",
        server_url="https://github.com",
        run_id="1",
        sha="0" * 40,
        base_ref=base_ref,
        head_ref="dependabot/github_actions/stable/vanadium/actions-deps",
        pr_number=39,
    )


def _recording_run_cmd(
    calls: list[list[str]], stdout: str = ""
) -> Callable[..., CommandResult]:
    def fake(cmd: list[str], **_kwargs: Any) -> CommandResult:
        calls.append(list(cmd))
        return CommandResult(returncode=0, stdout=stdout, stderr="")

    return fake


@pytest.fixture(autouse=True)
def _dispatched_bulk_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Match a bulk run: dispatched, so no event names a base ref."""
    monkeypatch.delenv("GERRIT_BRANCH", raising=False)
    monkeypatch.delenv("GITHUB_BASE_REF", raising=False)


class TestResolver:
    def test_uses_the_pull_request_base_ref(self, tmp_path: Path) -> None:
        orch = Orchestrator(workspace=tmp_path)
        assert orch._resolve_target_branch(_ctx()) == STABLE

    def test_gerrit_branch_still_overrides_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GERRIT_BRANCH", "release-1.2")
        orch = Orchestrator(workspace=tmp_path)
        assert orch._resolve_target_branch(_ctx()) == "release-1.2"

    def test_falls_back_to_github_base_ref(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_BASE_REF", "feature/x")
        orch = Orchestrator(workspace=tmp_path)
        assert orch._resolve_target_branch(_ctx(base_ref="")) == "feature/x"


class TestCallSites:
    def test_squash_is_based_on_the_pull_request_branch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[list[str]] = []
        monkeypatch.setattr(
            "github2gerrit.core.run_cmd", _recording_run_cmd(calls)
        )
        orch = Orchestrator(workspace=tmp_path)
        with (
            patch.multiple(
                orch,
                _analyze_merge_situation=MagicMock(),
                _checkout_with_unshallow_fallback=MagicMock(side_effect=_Stop),
            ),
            pytest.raises(_Stop),
        ):
            orch._prepare_squashed_commit(MagicMock(), _ctx(), GERRIT)
        assert ["git", "fetch", "origin", STABLE] in calls
        assert ["git", "rev-parse", f"origin/{STABLE}"] in calls

    def test_commit_range_ends_at_the_pull_request_branch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[list[str]] = []
        monkeypatch.setattr(
            "github2gerrit.core.run_cmd", _recording_run_cmd(calls)
        )
        orch = Orchestrator(workspace=tmp_path)
        orch._extract_local_commits_for_reconciliation(MagicMock(), _ctx())
        assert ["git", "rev-list", "--reverse", f"origin/{STABLE}..HEAD"] in (
            calls
        )

    def test_push_and_backref_target_the_pull_request_branch(
        self, tmp_path: Path
    ) -> None:
        orch = Orchestrator(workspace=tmp_path)
        stubs: dict[str, Any] = {
            name: MagicMock()
            for name in (
                "_push_to_gerrit",
                "_verify_and_sync_after_push",
                "_add_backref_comment_in_gerrit",
                "_comment_on_pull_request",
                "_validate_committed_files",
                "_post_push_supersession_sweep",
                "_close_pull_request_if_required",
                "_cleanup_ssh",
                "_resolve_reviewers",
            )
        }
        stubs["_query_gerrit_for_results"] = MagicMock(
            return_value=SubmissionResult(
                change_urls=[], change_numbers=[], commit_shas=[]
            )
        )
        with patch.multiple(orch, **stubs):
            orch._push_and_finalize(
                inputs=MagicMock(),
                gh=_ctx(),
                gerrit=GERRIT,
                repo_names=MagicMock(),
                prep=MagicMock(),
                operation_mode="create",
            )
        push = stubs["_push_to_gerrit"]
        backref = stubs["_add_backref_comment_in_gerrit"]
        assert push.call_args.kwargs["branch"] == STABLE
        assert backref.call_args.kwargs["branch"] == STABLE

    def test_supersession_sweep_stays_on_the_pull_request_branch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "github2gerrit.core.run_cmd",
            _recording_run_cmd([], stdout="Bump foo from 1.0 to 1.1"),
        )
        sweep = MagicMock(return_value=[])
        monkeypatch.setattr(
            "github2gerrit.gerrit_pr_closer."
            "abandon_superseded_dependency_changes",
            sweep,
        )
        orch = Orchestrator(workspace=tmp_path)
        orch._post_push_supersession_sweep(
            MagicMock(dry_run=False),
            GERRIT,
            _ctx(),
            PreparedChange(change_ids=["I" + "a" * 40], commit_shas=[]),
        )
        assert sweep.call_args.kwargs["target_branch"] == STABLE

    @pytest.mark.parametrize(
        ("env", "base_ref", "expected"),
        [
            ({"GERRIT_BRANCH": "release-1.2"}, STABLE, "release-1.2"),
            ({"GITHUB_BASE_REF": "feature/x"}, "", "feature/x"),
        ],
        ids=["override-wins", "no-base-ref"],
    )
    def test_dependency_reuse_searches_the_push_branch(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        env: dict[str, str],
        base_ref: str,
        expected: str,
    ) -> None:
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        query = MagicMock(return_value=[])
        monkeypatch.setattr(
            "github2gerrit.gerrit_query.query_open_changes_by_project", query
        )
        monkeypatch.setattr(
            "github2gerrit.gerrit_rest.build_client_for_host", MagicMock()
        )
        orch = Orchestrator(workspace=tmp_path)
        orch._find_change_by_dependency_package(
            _ctx(base_ref=base_ref), GERRIT, "Bump foo from 1.0 to 1.1"
        )
        assert query.call_args.kwargs["branch"] == expected
