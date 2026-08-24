# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Fork pull request hardening.

A fork pull request's tree is authored by someone without write access
to the base repository.  ``.gitreview`` read out of that tree must never
influence where the resulting change is pushed, or a fork could redirect
the push to an arbitrary Gerrit project.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from github2gerrit.cli import _head_repo_for_pr
from github2gerrit.cli import _read_head_repo
from github2gerrit.core import Orchestrator
from github2gerrit.gitreview import GitReviewInfo
from github2gerrit.models import GitHubContext


sys.path.append(str(Path(__file__).parent))
from fixtures.make_repo import init_repo
from fixtures.make_repo import write_gitreview


BASE_REPO = "opendaylight/mdsal"
FORK_REPO = "attacker/mdsal"


def _gh_ctx(
    *,
    repository: str = BASE_REPO,
    head_repo: str = "",
    base_ref: str = "master",
    head_ref: str = "feature/evil",
    pr_number: int | None = 29,
) -> GitHubContext:
    return GitHubContext(
        event_name="pull_request_target",
        event_action="opened",
        event_path=None,
        repository=repository,
        repository_owner=repository.split("/")[0],
        server_url="https://github.com",
        run_id="1",
        sha="deadbeef",
        base_ref=base_ref,
        head_ref=head_ref,
        pr_number=pr_number,
        head_repo=head_repo,
    )


class TestIsForkPr:
    """Provenance detection on GitHubContext."""

    def test_fork_head_detected(self) -> None:
        assert _gh_ctx(head_repo=FORK_REPO).is_fork_pr is True

    def test_same_repo_not_a_fork(self) -> None:
        assert _gh_ctx(head_repo=BASE_REPO).is_fork_pr is False

    def test_comparison_is_case_insensitive(self) -> None:
        assert _gh_ctx(head_repo="OpenDaylight/MdSal").is_fork_pr is False

    def test_unknown_head_repo_is_not_a_fork(self) -> None:
        # An absent signal must not silently change same-repo handling.
        assert _gh_ctx(head_repo="").is_fork_pr is False

    def test_unknown_base_repo_is_not_a_fork(self) -> None:
        ctx = _gh_ctx(repository="", head_repo=FORK_REPO)
        assert ctx.is_fork_pr is False


class TestReadGitreviewForkIsolation:
    """``_read_gitreview`` must not trust a fork-supplied file."""

    def test_fork_gitreview_in_workspace_is_ignored(
        self, tmp_path: Path
    ) -> None:
        """A malicious .gitreview in the PR tree must not be used."""
        repo = init_repo(tmp_path / "forked", default_branch="master")
        write_gitreview(
            repo,
            host="gerrit.opendaylight.org",
            port=29418,
            project="attacker/exfiltration-target",
        )
        orch = Orchestrator(workspace=repo.path)

        trusted = GitReviewInfo(
            host="gerrit.opendaylight.org",
            port=29418,
            project="mdsal",
        )
        with patch(
            "github2gerrit.core.fetch_gitreview", return_value=trusted
        ) as mock_fetch:
            info = orch._read_gitreview(
                repo.path / ".gitreview",
                _gh_ctx(head_repo=FORK_REPO),
            )

        assert info is not None
        assert info.project == "mdsal", (
            "fork-supplied .gitreview must not reach Gerrit resolution"
        )
        assert mock_fetch.call_count == 1
        assert mock_fetch.call_args.kwargs["skip_local"] is True

    def test_fork_resolution_pinned_to_base_ref(self, tmp_path: Path) -> None:
        """Fork branch names must not steer base-repository lookups."""
        repo = init_repo(tmp_path / "forked2", default_branch="master")
        write_gitreview(
            repo,
            host="gerrit.example.org",
            port=29418,
            project="attacker/target",
        )
        orch = Orchestrator(workspace=repo.path)

        with patch(
            "github2gerrit.core.fetch_gitreview", return_value=None
        ) as mock_fetch:
            orch._read_gitreview(
                repo.path / ".gitreview",
                _gh_ctx(head_repo=FORK_REPO, head_ref="evil-branch"),
            )

        kwargs = mock_fetch.call_args.kwargs
        assert kwargs["api_ref"] == "master"
        assert kwargs["branches"] == ["master"]
        assert "evil-branch" not in kwargs["branches"]
        assert kwargs["include_env_refs"] is False
        assert kwargs["repo_full"] == BASE_REPO

    def test_same_repo_pr_still_reads_local_file(self, tmp_path: Path) -> None:
        """Non-fork behaviour is unchanged."""
        repo = init_repo(tmp_path / "trusted", default_branch="master")
        write_gitreview(
            repo,
            host="gerrit.opendaylight.org",
            port=29418,
            project="mdsal",
        )
        orch = Orchestrator(workspace=repo.path)

        with patch("github2gerrit.core.fetch_gitreview") as mock_fetch:
            info = orch._read_gitreview(
                repo.path / ".gitreview",
                _gh_ctx(head_repo=BASE_REPO),
            )

        assert info is not None
        assert info.project == "mdsal"
        mock_fetch.assert_not_called()

    def test_no_context_still_reads_local_file(self, tmp_path: Path) -> None:
        """Direct CLI use without an event context is unaffected."""
        repo = init_repo(tmp_path / "local", default_branch="master")
        write_gitreview(
            repo,
            host="gerrit.example.org",
            port=29418,
            project="some/project",
        )
        orch = Orchestrator(workspace=repo.path)

        info = orch._read_gitreview(repo.path / ".gitreview")

        assert info is not None
        assert info.project == "some/project"

    def test_fork_falls_through_when_base_lookup_fails(
        self, tmp_path: Path
    ) -> None:
        """Returning None lets inputs/derivation take over."""
        repo = init_repo(tmp_path / "forked3", default_branch="master")
        write_gitreview(
            repo,
            host="gerrit.evil.example",
            port=29418,
            project="attacker/target",
        )
        orch = Orchestrator(workspace=repo.path)

        with patch("github2gerrit.core.fetch_gitreview", return_value=None):
            info = orch._read_gitreview(
                repo.path / ".gitreview",
                _gh_ctx(head_repo=FORK_REPO),
            )

        assert info is None


class TestHeadRepoResolution:
    """Provenance plumbing from the event and the API."""

    def test_env_var_preferred(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PR_HEAD_REPO", FORK_REPO)
        payload = {"pull_request": {"head": {"repo": {"full_name": "x/y"}}}}
        assert _read_head_repo(payload) == FORK_REPO

    def test_payload_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PR_HEAD_REPO", raising=False)
        payload = {"pull_request": {"head": {"repo": {"full_name": FORK_REPO}}}}
        assert _read_head_repo(payload) == FORK_REPO

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"pull_request": {}},
            {"pull_request": {"head": {}}},
            # A deleted fork reports a null repo.
            {"pull_request": {"head": {"repo": None}}},
            {"pull_request": {"head": {"repo": {}}}},
        ],
    )
    def test_missing_payload_fields_yield_empty(
        self, payload: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("PR_HEAD_REPO", raising=False)
        assert _read_head_repo(payload) == ""

    def test_head_repo_from_pull_request_object(self) -> None:
        class _Repo:
            full_name = FORK_REPO

        class _Head:
            repo = _Repo()

        class _Pr:
            head = _Head()

        assert _head_repo_for_pr(_Pr()) == FORK_REPO

    def test_head_repo_from_object_missing_repo(self) -> None:
        class _Pr:
            head = None

        assert _head_repo_for_pr(_Pr()) == ""
