# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Merge and abandon events close only the pull request's own change.

The closers find a pull request through the ``GitHub-PR`` trailer of a
Gerrit change. Cherry-picks copy that trailer, so merging a backport on
a stable branch closed the original pull request, whose own change might
still be open on its branch (#502).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from github2gerrit import cli
from github2gerrit.gerrit_pr_closer import cleanup_abandoned_prs_single
from github2gerrit.models import GitHubContext


PR_URL = "https://github.com/opendaylight/mdsal/pull/31"
CHANGE_URL = "https://gerrit.example.org/c/mdsal/+/126368"
MODULE = "github2gerrit.gerrit_pr_closer"


@pytest.fixture(autouse=True)
def _no_branch_context(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("GERRIT_BRANCH", "GERRIT_CHANGE_URL", "G2G_GERRIT_CHANGE_URL"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def close_pr(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Serve PR #31, open against master; record closures."""
    pr = MagicMock()
    pr.number = 31
    pr.state = "open"
    pr.base.ref = "master"
    monkeypatch.setattr(f"{MODULE}.build_client", MagicMock())
    monkeypatch.setattr(f"{MODULE}.get_pull", lambda _repo, _number: pr)
    monkeypatch.setattr(f"{MODULE}.extract_pr_info_for_display", MagicMock())
    monkeypatch.setattr(f"{MODULE}.display_pr_info", MagicMock())
    monkeypatch.setenv("CLOSE_MERGED_PRS", "true")
    monkeypatch.setenv("G2G_SHOW_PROGRESS", "false")
    closer = MagicMock()
    monkeypatch.setattr(f"{MODULE}.close_pr", closer)
    return closer


def _push_event(tmp_path: Path, branch: str) -> GitHubContext:
    payload = tmp_path / "event.json"
    payload.write_text(
        json.dumps(
            {"ref": f"refs/heads/{branch}", "commits": [{"id": "c0ffee"}]}
        )
    )
    return GitHubContext(
        event_name="push",
        event_action="",
        event_path=payload,
        repository="opendaylight/mdsal",
        repository_owner="opendaylight",
        server_url="https://github.com",
        run_id="1",
        sha="c0ffee",
        base_ref="",
        head_ref="",
        pr_number=None,
    )


class TestPushEvent:
    @pytest.fixture(autouse=True)
    def _trailer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            f"{MODULE}.extract_pr_url_from_commit", lambda _sha: PR_URL
        )

    def test_backport_merged_on_another_branch_leaves_the_pr_open(
        self, tmp_path: Path, close_pr: MagicMock
    ) -> None:
        cli._process_close_merged_prs(
            MagicMock(dry_run=False), _push_event(tmp_path, "14.0.x")
        )
        close_pr.assert_not_called()

    def test_merge_on_the_pr_branch_closes_it(
        self, tmp_path: Path, close_pr: MagicMock
    ) -> None:
        cli._process_close_merged_prs(
            MagicMock(dry_run=False), _push_event(tmp_path, "master")
        )
        close_pr.assert_called_once()

    def test_merge_on_the_override_branch_closes_it(
        self,
        tmp_path: Path,
        close_pr: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """GERRIT_BRANCH sent the PR's change to another branch."""
        monkeypatch.setenv("GERRIT_BRANCH", "release/1.2")
        cli._process_close_merged_prs(
            MagicMock(dry_run=False), _push_event(tmp_path, "release/1.2")
        )
        close_pr.assert_called_once()


class TestGerritDispatch:
    @pytest.fixture(autouse=True)
    def _merged_change(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A dispatch passes the change in GERRIT_CHANGE_URL, which is
        # what tells GERRIT_BRANCH apart from a push override
        monkeypatch.setenv("GERRIT_CHANGE_URL", CHANGE_URL)
        monkeypatch.setattr(
            "github2gerrit.cli.extract_pr_url_from_gerrit_change",
            lambda _url: PR_URL,
        )
        monkeypatch.setattr(
            "github2gerrit.cli.check_gerrit_change_status",
            lambda _url: "MERGED",
        )

    def test_change_on_another_branch_leaves_the_pr_open(
        self,
        tmp_path: Path,
        close_pr: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GERRIT_BRANCH", "14.0.x")
        cli._process_close_gerrit_change(
            MagicMock(dry_run=False, organization="opendaylight"),
            _push_event(tmp_path, "master"),
            CHANGE_URL,
            force=True,
        )
        close_pr.assert_not_called()

    @pytest.mark.parametrize("branch", ["master", ""])
    def test_change_on_the_pr_branch_closes_it(
        self,
        tmp_path: Path,
        close_pr: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
        branch: str,
    ) -> None:
        monkeypatch.setenv("GERRIT_BRANCH", branch)
        cli._process_close_gerrit_change(
            MagicMock(dry_run=False, organization="opendaylight"),
            _push_event(tmp_path, "master"),
            CHANGE_URL,
            force=True,
        )
        close_pr.assert_called_once()


def test_single_abandon_on_another_branch_leaves_the_pr_open(
    close_pr: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    gerrit = MagicMock()
    gerrit.get.return_value = {"status": "ABANDONED", "branch": "14.0.x"}
    monkeypatch.setattr(
        f"{MODULE}.build_client_for_host", lambda _host, **_kwargs: gerrit
    )
    monkeypatch.setattr(
        f"{MODULE}.extract_pr_url_from_gerrit_change", lambda _url: PR_URL
    )
    assert cleanup_abandoned_prs_single(CHANGE_URL) is False
    close_pr.assert_not_called()
