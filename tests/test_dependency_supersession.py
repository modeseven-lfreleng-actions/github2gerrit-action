# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for dependency supersession logic (issue #188).

Covers two mechanisms:

1. **Strategy 5** in ``_find_existing_change_for_pr`` — reuse the
   Change-Id of an existing open Gerrit change that bumps the same
   dependency package (update-in-place).

2. **Post-push abandon sweep**
   (``abandon_superseded_dependency_changes``) — after pushing a new
   change, abandon any remaining open Gerrit changes for the same
   dependency (Option A fallback).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock
from unittest.mock import patch

from github2gerrit.core import Orchestrator
from github2gerrit.gerrit_pr_closer import abandon_superseded_dependency_changes
from github2gerrit.gerrit_query import GerritChange
from github2gerrit.gerrit_query import query_open_changes_by_project
from github2gerrit.gitreview import GerritInfo
from github2gerrit.models import GitHubContext
from github2gerrit.similarity import extract_dependency_package_from_subject


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------


def _gerrit_change(
    *,
    change_id: str = "Iaaaa000000000000000000000000000000000000",
    number: str = "12345",
    subject: str = "chore: bump requests from 2.31.0 to 2.32.0",
    status: str = "NEW",
    files: list[str] | None = None,
    commit_message: str = (
        "chore: bump requests from 2.31.0 to 2.32.0\n\n"
        "GitHub-PR: https://github.com/org/repo/pull/42\n"
        "Signed-off-by: Bot <bot@example.com>"
    ),
    topic: str | None = None,
) -> GerritChange:
    """Factory for ``GerritChange`` test fixtures."""
    return GerritChange(
        change_id=change_id,
        number=number,
        subject=subject,
        status=status,
        current_revision="deadbeef",
        files=files or [],
        commit_message=commit_message,
        topic=topic,
    )


# -------------------------------------------------------------------
# extract_dependency_package_from_subject — baseline
# -------------------------------------------------------------------


class TestPackageExtraction:
    """Verify that the package extractor works for titles we rely on."""

    def test_simple_bump(self) -> None:
        """Standard Dependabot title."""

        pkg = extract_dependency_package_from_subject(
            "Bump requests from 2.31.0 to 2.32.0"
        )
        assert pkg == "requests"

    def test_scoped_package(self) -> None:
        """Org-scoped package name."""

        pkg = extract_dependency_package_from_subject(
            "chore: bump @types/react from 18.2.0 to 18.3.0"
        )
        assert pkg == "@types/react"

    def test_github_action_path(self) -> None:
        """Long GitHub Actions workflow path."""

        pkg = extract_dependency_package_from_subject(
            "chore: bump lfreleng-actions/github2gerrit-action "
            "from 1.0.8 to 1.2.0"
        )
        assert pkg == "lfreleng-actions/github2gerrit-action"

    def test_non_dependency_title(self) -> None:
        """Non-dependency title returns empty string."""

        pkg = extract_dependency_package_from_subject("feat: add login page")
        assert pkg == ""

    def test_build_deps_prefix(self) -> None:
        """Build(deps) prefix should be handled (CC broadening)."""

        pkg = extract_dependency_package_from_subject(
            "Build(deps): Bump lfit/releng-reusable-workflows "
            "from 0.2.28 to 0.2.31"
        )
        assert pkg == "lfit/releng-reusable-workflows"

    def test_fix_deps_prefix(self) -> None:
        """Fix(deps) prefix should be handled."""

        pkg = extract_dependency_package_from_subject(
            "Fix(deps): update lodash from 4.17.20 to 4.17.21"
        )
        assert pkg == "lodash"


# -------------------------------------------------------------------
# query_open_changes_by_project
# -------------------------------------------------------------------


class TestQueryOpenChangesByProject:
    """Unit tests for the new Gerrit query helper."""

    def test_returns_changes_on_success(self) -> None:
        """Should delegate to pagination helper and return results."""

        fake_client = MagicMock()
        fake_client.get.return_value = [
            {
                "change_id": "I111",
                "_number": 100,
                "subject": "chore: bump pkg from 1 to 2",
                "status": "NEW",
                "current_revision": "abc",
                "revisions": {
                    "abc": {
                        "files": {},
                        "commit": {"message": ""},
                    }
                },
            },
        ]

        result = query_open_changes_by_project(
            fake_client, "org/repo", max_results=50
        )
        assert len(result) == 1
        assert result[0].change_id == "I111"

    def test_returns_empty_on_error(self) -> None:
        """Should return empty list on REST error."""

        fake_client = MagicMock()
        fake_client.get.side_effect = RuntimeError("connection refused")

        result = query_open_changes_by_project(fake_client, "org/repo")
        assert result == []


# -------------------------------------------------------------------
# abandon_superseded_dependency_changes
# -------------------------------------------------------------------


class TestAbandonSupersededDependencyChanges:
    """Tests for the post-push abandon sweep."""

    @patch("github2gerrit.gerrit_rest.build_client_for_host")
    @patch("github2gerrit.gerrit_query.query_open_changes_by_project")
    def test_abandons_matching_changes(
        self,
        mock_query: MagicMock,
        mock_build_client: MagicMock,
    ) -> None:
        """Should abandon open changes bumping the same dependency."""

        old_change = _gerrit_change(
            change_id="Iold0000000000000000000000000000000000000",
            number="100",
            subject="chore: bump requests from 2.31.0 to 2.31.5",
        )
        unrelated_change = _gerrit_change(
            change_id="Iunrelated00000000000000000000000000000",
            number="200",
            subject="feat: add logging",
        )
        mock_query.return_value = [old_change, unrelated_change]

        fake_client = MagicMock()
        mock_build_client.return_value = fake_client

        result = abandon_superseded_dependency_changes(
            gerrit_server="gerrit.example.org",
            gerrit_project="org/repo",
            current_subject="chore: bump requests from 2.31.0 to 2.32.0",
            exclude_change_ids=["Inew0000000000000000000000000000000000000"],
        )

        assert result == ["100"]
        fake_client.post.assert_called_once()
        call_args = fake_client.post.call_args
        assert "/changes/100/abandon" in call_args[0][0]

    @patch("github2gerrit.gerrit_rest.build_client_for_host")
    @patch("github2gerrit.gerrit_query.query_open_changes_by_project")
    def test_skips_excluded_change_ids(
        self,
        mock_query: MagicMock,
        mock_build_client: MagicMock,
    ) -> None:
        """Must not abandon the change we just pushed."""

        our_change = _gerrit_change(
            change_id="Iours000000000000000000000000000000000000",
            number="300",
            subject="chore: bump requests from 2.31.0 to 2.32.0",
        )
        mock_query.return_value = [our_change]

        fake_client = MagicMock()
        mock_build_client.return_value = fake_client

        result = abandon_superseded_dependency_changes(
            gerrit_server="gerrit.example.org",
            gerrit_project="org/repo",
            current_subject="chore: bump requests from 2.31.0 to 2.32.0",
            exclude_change_ids=["Iours000000000000000000000000000000000000"],
        )

        assert result == []
        fake_client.post.assert_not_called()

    @patch("github2gerrit.gerrit_rest.build_client_for_host")
    @patch("github2gerrit.gerrit_query.query_open_changes_by_project")
    def test_no_action_for_different_packages(
        self,
        mock_query: MagicMock,
        mock_build_client: MagicMock,
    ) -> None:
        """Changes for different packages must not be abandoned."""

        other_pkg = _gerrit_change(
            change_id="Iother00000000000000000000000000000000000",
            number="400",
            subject="chore: bump flask from 2.0 to 3.0",
        )
        mock_query.return_value = [other_pkg]

        fake_client = MagicMock()
        mock_build_client.return_value = fake_client

        result = abandon_superseded_dependency_changes(
            gerrit_server="gerrit.example.org",
            gerrit_project="org/repo",
            current_subject="chore: bump requests from 2.31.0 to 2.32.0",
            exclude_change_ids=[],
        )

        assert result == []
        fake_client.post.assert_not_called()

    def test_non_dependency_subject_returns_empty(self) -> None:
        """Non-dependency subjects should short-circuit."""

        result = abandon_superseded_dependency_changes(
            gerrit_server="gerrit.example.org",
            gerrit_project="org/repo",
            current_subject="feat: add login page",
            exclude_change_ids=[],
        )
        assert result == []

    @patch("github2gerrit.gerrit_rest.build_client_for_host")
    @patch("github2gerrit.gerrit_query.query_open_changes_by_project")
    def test_dry_run_does_not_abandon(
        self,
        mock_query: MagicMock,
        mock_build_client: MagicMock,
    ) -> None:
        """Dry-run mode should report but not POST to Gerrit."""

        old_change = _gerrit_change(
            change_id="Iold0000000000000000000000000000000000000",
            number="500",
            subject="chore: bump requests from 2.31.0 to 2.31.5",
        )
        mock_query.return_value = [old_change]

        fake_client = MagicMock()
        mock_build_client.return_value = fake_client

        result = abandon_superseded_dependency_changes(
            gerrit_server="gerrit.example.org",
            gerrit_project="org/repo",
            current_subject="chore: bump requests from 2.31.0 to 2.32.0",
            exclude_change_ids=[],
            dry_run=True,
        )

        assert result == ["500"]
        fake_client.post.assert_not_called()

    @patch("github2gerrit.gerrit_rest.build_client_for_host")
    @patch("github2gerrit.gerrit_query.query_open_changes_by_project")
    def test_abandon_multiple_stale_changes(
        self,
        mock_query: MagicMock,
        mock_build_client: MagicMock,
    ) -> None:
        """Multiple stale changes for the same dep should all be abandoned."""

        stale1 = _gerrit_change(
            change_id="Istale1000000000000000000000000000000000",
            number="601",
            subject="chore: bump requests from 2.31.0 to 2.31.1",
        )
        stale2 = _gerrit_change(
            change_id="Istale2000000000000000000000000000000000",
            number="602",
            subject="chore: bump requests from 2.31.0 to 2.31.5",
        )
        mock_query.return_value = [stale1, stale2]

        fake_client = MagicMock()
        mock_build_client.return_value = fake_client

        result = abandon_superseded_dependency_changes(
            gerrit_server="gerrit.example.org",
            gerrit_project="org/repo",
            current_subject="chore: bump requests from 2.31.0 to 2.32.0",
            exclude_change_ids=[],
        )

        assert sorted(result) == ["601", "602"]
        assert fake_client.post.call_count == 2

    @patch("github2gerrit.gerrit_rest.build_client_for_host")
    @patch("github2gerrit.gerrit_query.query_open_changes_by_project")
    def test_abandon_failure_is_non_fatal(
        self,
        mock_query: MagicMock,
        mock_build_client: MagicMock,
    ) -> None:
        """A failed abandon should not crash the sweep."""

        stale = _gerrit_change(
            change_id="Istale0000000000000000000000000000000000",
            number="700",
            subject="chore: bump requests from 2.31.0 to 2.31.1",
        )
        mock_query.return_value = [stale]

        fake_client = MagicMock()
        fake_client.post.side_effect = RuntimeError("403 forbidden")
        mock_build_client.return_value = fake_client

        # Should not raise
        result = abandon_superseded_dependency_changes(
            gerrit_server="gerrit.example.org",
            gerrit_project="org/repo",
            current_subject="chore: bump requests from 2.31.0 to 2.32.0",
            exclude_change_ids=[],
        )
        assert result == []

    @patch("github2gerrit.gerrit_rest.build_client_for_host")
    @patch("github2gerrit.gerrit_query.query_open_changes_by_project")
    def test_github_actions_workflow_bump(
        self,
        mock_query: MagicMock,
        mock_build_client: MagicMock,
    ) -> None:
        """GitHub Actions workflow bumps should match by action name."""

        old_action = _gerrit_change(
            change_id="Iaction000000000000000000000000000000000",
            number="800",
            subject=(
                "chore: bump lfreleng-actions/github2gerrit-action "
                "from 1.0.8 to 1.0.9"
            ),
        )
        mock_query.return_value = [old_action]

        fake_client = MagicMock()
        mock_build_client.return_value = fake_client

        result = abandon_superseded_dependency_changes(
            gerrit_server="gerrit.example.org",
            gerrit_project="org/repo",
            current_subject=(
                "chore: bump lfreleng-actions/github2gerrit-action "
                "from 1.0.8 to 1.2.0"
            ),
            exclude_change_ids=[],
        )

        assert result == ["800"]
        fake_client.post.assert_called_once()

    @patch("github2gerrit.gerrit_rest.build_client_for_host")
    @patch("github2gerrit.gerrit_query.query_open_changes_by_project")
    def test_rest_client_failure_is_non_fatal(
        self,
        mock_query: MagicMock,
        mock_build_client: MagicMock,
    ) -> None:
        """REST client construction failure should not crash."""

        mock_build_client.side_effect = RuntimeError("cannot connect")

        result = abandon_superseded_dependency_changes(
            gerrit_server="gerrit.example.org",
            gerrit_project="org/repo",
            current_subject="chore: bump requests from 2.31.0 to 2.32.0",
            exclude_change_ids=[],
        )
        assert result == []

    @patch("github2gerrit.gerrit_rest.build_client_for_host")
    @patch("github2gerrit.gerrit_query.query_open_changes_by_project")
    def test_passes_target_branch_to_query(
        self,
        mock_query: MagicMock,
        mock_build_client: MagicMock,
    ) -> None:
        """target_branch should be forwarded to the Gerrit query."""

        mock_query.return_value = []

        fake_client = MagicMock()
        mock_build_client.return_value = fake_client

        abandon_superseded_dependency_changes(
            gerrit_server="gerrit.example.org",
            gerrit_project="org/repo",
            current_subject="chore: bump requests from 2.31.0 to 2.32.0",
            exclude_change_ids=[],
            target_branch="main",
        )

        mock_query.assert_called_once_with(
            fake_client,
            "org/repo",
            branch="main",
            max_results=200,
        )


# -------------------------------------------------------------------
# Strategy 5 integration in _find_existing_change_for_pr
# -------------------------------------------------------------------


@dataclass
class _FakeGitHubUser:
    """Minimal mock of a GitHub user object."""

    login: str = "dependabot[bot]"


@dataclass
class _FakeGitHubIssue:
    """Minimal mock of a GitHub issue (for comments)."""

    def get_comments(self) -> list[Any]:
        """Return empty comment list."""
        return []


@dataclass
class _FakePullRequest:
    """Minimal mock of a GitHub PR object."""

    number: int = 42
    title: str = "Bump requests from 2.31.0 to 2.32.0"
    state: str = "open"
    user: _FakeGitHubUser | None = None

    def as_issue(self) -> _FakeGitHubIssue:
        """Return fake issue for comment iteration."""
        return _FakeGitHubIssue()


class TestStrategy5Integration:
    """Test that Strategy 5 is reached and works in the discovery cascade."""

    @patch("github2gerrit.core.build_client")
    @patch("github2gerrit.core.get_repo_from_env")
    @patch("github2gerrit.core.get_pull")
    @patch("github2gerrit.gerrit_rest.build_client_for_host")
    @patch("github2gerrit.gerrit_query.query_open_changes_by_project")
    def test_strategy5_finds_matching_change(
        self,
        mock_query_open: MagicMock,
        mock_gerrit_client: MagicMock,
        mock_get_pull: MagicMock,
        mock_get_repo: MagicMock,
        mock_build_client: MagicMock,
        tmp_path: Any,
    ) -> None:
        """Strategy 5 should return Change-Id when package matches."""

        # Set up GitHub mocks — strategies 1-4 should all fail
        mock_get_pull.return_value = _FakePullRequest(
            number=42,
            title="Bump requests from 2.31.0 to 2.32.0",
            user=_FakeGitHubUser(),
        )
        mock_get_repo.return_value = MagicMock()

        # Strategy 1 (topic) will fail — no changes for this topic
        # Strategy 2/3 (hash/trailer) will fail — no matching hash
        # Strategy 4 (mapping comments) will fail — no comments
        # Mock the Gerrit REST client to return empty for topic query
        fake_gerrit = MagicMock()
        fake_gerrit.get.return_value = []
        mock_gerrit_client.return_value = fake_gerrit

        # Strategy 5 — return an open change bumping the same package
        existing_change = _gerrit_change(
            change_id="Iexist00000000000000000000000000000000000",
            number="999",
            subject="chore: bump requests from 2.31.0 to 2.31.5",
        )
        mock_query_open.return_value = [existing_change]

        # Build orchestrator and context
        orch = Orchestrator(workspace=tmp_path)
        gh = GitHubContext(
            event_name="pull_request",
            event_action="opened",
            event_path=None,
            repository="owner/repo",
            repository_owner="owner",
            server_url="https://github.com",
            run_id="1",
            sha="abc123",
            base_ref="main",
            head_ref="dependabot/pip/requests-2.32.0",
            pr_number=42,
        )
        gerrit = GerritInfo(
            host="gerrit.example.org",
            port=29418,
            project="owner/repo",
        )

        result = orch._find_existing_change_for_pr(gh, gerrit)
        assert result == ["Iexist00000000000000000000000000000000000"]

    @patch("github2gerrit.core.build_client")
    @patch("github2gerrit.core.get_repo_from_env")
    @patch("github2gerrit.core.get_pull")
    @patch("github2gerrit.gerrit_rest.build_client_for_host")
    @patch("github2gerrit.gerrit_query.query_open_changes_by_project")
    def test_strategy5_skips_non_dependency_pr(
        self,
        mock_query_open: MagicMock,
        mock_gerrit_client: MagicMock,
        mock_get_pull: MagicMock,
        mock_get_repo: MagicMock,
        mock_build_client: MagicMock,
        tmp_path: Any,
    ) -> None:
        """Strategy 5 should be skipped for non-dependency PRs."""

        mock_get_pull.return_value = _FakePullRequest(
            number=42,
            title="feat: add user dashboard",
            user=_FakeGitHubUser(login="human-dev"),
        )
        mock_get_repo.return_value = MagicMock()

        fake_gerrit = MagicMock()
        fake_gerrit.get.return_value = []
        mock_gerrit_client.return_value = fake_gerrit

        # Should never be called since package extraction fails
        mock_query_open.return_value = []

        orch = Orchestrator(workspace=tmp_path)
        gh = GitHubContext(
            event_name="pull_request",
            event_action="opened",
            event_path=None,
            repository="owner/repo",
            repository_owner="owner",
            server_url="https://github.com",
            run_id="1",
            sha="abc123",
            base_ref="main",
            head_ref="feature/dashboard",
            pr_number=42,
        )
        gerrit = GerritInfo(
            host="gerrit.example.org",
            port=29418,
            project="owner/repo",
        )

        result = orch._find_existing_change_for_pr(gh, gerrit)
        assert result == []

    @patch("github2gerrit.core.build_client")
    @patch("github2gerrit.core.get_repo_from_env")
    @patch("github2gerrit.core.get_pull")
    @patch("github2gerrit.gerrit_rest.build_client_for_host")
    @patch("github2gerrit.gerrit_query.query_open_changes_by_project")
    def test_strategy5_no_match_for_different_package(
        self,
        mock_query_open: MagicMock,
        mock_gerrit_client: MagicMock,
        mock_get_pull: MagicMock,
        mock_get_repo: MagicMock,
        mock_build_client: MagicMock,
        tmp_path: Any,
    ) -> None:
        """Strategy 5 should not match changes for different packages."""

        mock_get_pull.return_value = _FakePullRequest(
            number=42,
            title="Bump requests from 2.31.0 to 2.32.0",
            user=_FakeGitHubUser(),
        )
        mock_get_repo.return_value = MagicMock()

        fake_gerrit = MagicMock()
        fake_gerrit.get.return_value = []
        mock_gerrit_client.return_value = fake_gerrit

        # Open change for a DIFFERENT package
        different_pkg = _gerrit_change(
            change_id="Idiff000000000000000000000000000000000000",
            number="888",
            subject="chore: bump flask from 2.0 to 3.0",
        )
        mock_query_open.return_value = [different_pkg]

        orch = Orchestrator(workspace=tmp_path)
        gh = GitHubContext(
            event_name="pull_request",
            event_action="opened",
            event_path=None,
            repository="owner/repo",
            repository_owner="owner",
            server_url="https://github.com",
            run_id="1",
            sha="abc123",
            base_ref="main",
            head_ref="dependabot/pip/requests-2.32.0",
            pr_number=42,
        )
        gerrit = GerritInfo(
            host="gerrit.example.org",
            port=29418,
            project="owner/repo",
        )

        result = orch._find_existing_change_for_pr(gh, gerrit)
        assert result == []


# -------------------------------------------------------------------
# Real-world scenarios from issue #188
# -------------------------------------------------------------------


class TestRealWorldScenarios:
    """Scenarios taken directly from the issue report."""

    @patch("github2gerrit.gerrit_rest.build_client_for_host")
    @patch("github2gerrit.gerrit_query.query_open_changes_by_project")
    def test_ovsdb_three_stale_changes(
        self,
        mock_query: MagicMock,
        mock_build_client: MagicMock,
    ) -> None:
        """ovsdb scenario: three stale g2g-action bumps, newest wins."""

        stale_109 = _gerrit_change(
            change_id="I109000000000000000000000000000000000000",
            number="121752",
            subject=(
                "chore: bump lfreleng-actions/github2gerrit-action "
                "from 1.0.8 to 1.0.9"
            ),
        )
        stale_110 = _gerrit_change(
            change_id="I110000000000000000000000000000000000000",
            number="121797",
            subject=(
                "chore: bump lfreleng-actions/github2gerrit-action "
                "from 1.0.8 to 1.1.0"
            ),
        )
        current_120 = _gerrit_change(
            change_id="I120000000000000000000000000000000000000",
            number="122030",
            subject=(
                "chore: bump lfreleng-actions/github2gerrit-action "
                "from 1.0.8 to 1.2.0"
            ),
        )
        mock_query.return_value = [stale_109, stale_110, current_120]

        fake_client = MagicMock()
        mock_build_client.return_value = fake_client

        result = abandon_superseded_dependency_changes(
            gerrit_server="git.opendaylight.org",
            gerrit_project="ovsdb",
            current_subject=(
                "chore: bump lfreleng-actions/github2gerrit-action "
                "from 1.0.8 to 1.2.0"
            ),
            exclude_change_ids=["I120000000000000000000000000000000000000"],
        )

        assert sorted(result) == ["121752", "121797"]
        assert fake_client.post.call_count == 2

    @patch("github2gerrit.gerrit_rest.build_client_for_host")
    @patch("github2gerrit.gerrit_query.query_open_changes_by_project")
    def test_lispflowmapping_mixed_packages(
        self,
        mock_query: MagicMock,
        mock_build_client: MagicMock,
    ) -> None:
        """lispflowmapping: two different packages, only same-pkg abandoned."""

        stale_info_yaml = _gerrit_change(
            change_id="Iyaml000000000000000000000000000000000000",
            number="121811",
            subject=("chore: bump info-yaml-verify from 0.2.28 to 0.2.29"),
        )
        stale_g2g = _gerrit_change(
            change_id="Ig2g0000000000000000000000000000000000000",
            number="121813",
            subject=("chore: bump github2gerrit-action from 1.0.9 to 1.1.0"),
        )
        mock_query.return_value = [stale_info_yaml, stale_g2g]

        fake_client = MagicMock()
        mock_build_client.return_value = fake_client

        # Pushing new info-yaml-verify bump should only abandon the
        # old info-yaml-verify change, NOT the g2g-action change.
        result = abandon_superseded_dependency_changes(
            gerrit_server="git.opendaylight.org",
            gerrit_project="lispflowmapping",
            current_subject=(
                "chore: bump info-yaml-verify from 0.2.28 to 0.2.31"
            ),
            exclude_change_ids=[],
        )

        assert result == ["121811"]
        assert fake_client.post.call_count == 1
        call_path = fake_client.post.call_args[0][0]
        assert "121811" in call_path
