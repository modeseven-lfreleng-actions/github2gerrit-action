# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from github2gerrit.cli import _check_automation_only
from github2gerrit.models import GitHubContext


class TestAutomationOnly:
    """Test suite for automation_only feature."""

    def test_automation_only_disabled_allows_all_prs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When automation_only is disabled, all PRs should be allowed."""
        monkeypatch.setenv("AUTOMATION_ONLY", "false")

        mock_pr = MagicMock()
        mock_pr.user.login = "regular-user"

        gh = GitHubContext(
            event_name="pull_request",
            event_action="opened",
            event_path=None,
            repository="owner/repo",
            repository_owner="owner",
            server_url="https://github.com",
            run_id="123",
            sha="abc123",
            base_ref="main",
            head_ref="feature",
            pr_number=1,
        )

        # Should not raise any exception
        _check_automation_only(mock_pr, gh)

    def test_dependabot_pr_allowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Dependabot PRs should be allowed when automation_only is enabled."""
        monkeypatch.setenv("AUTOMATION_ONLY", "true")

        mock_pr = MagicMock()
        mock_pr.user.login = "dependabot[bot]"

        gh = GitHubContext(
            event_name="pull_request",
            event_action="opened",
            event_path=None,
            repository="owner/repo",
            repository_owner="owner",
            server_url="https://github.com",
            run_id="123",
            sha="abc123",
            base_ref="main",
            head_ref="feature",
            pr_number=1,
        )

        # Should not raise any exception
        _check_automation_only(mock_pr, gh)

    def test_precommit_ci_pr_allowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pre-commit.ci PRs should be allowed when automation_only is enabled."""
        monkeypatch.setenv("AUTOMATION_ONLY", "true")

        mock_pr = MagicMock()
        mock_pr.user.login = "pre-commit-ci[bot]"

        gh = GitHubContext(
            event_name="pull_request",
            event_action="opened",
            event_path=None,
            repository="owner/repo",
            repository_owner="owner",
            server_url="https://github.com",
            run_id="123",
            sha="abc123",
            base_ref="main",
            head_ref="feature",
            pr_number=1,
        )

        # Should not raise any exception
        _check_automation_only(mock_pr, gh)

    @patch("github2gerrit.github_api.close_pr")
    def test_regular_user_pr_rejected(
        self, mock_close_pr: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regular user PRs should be rejected when automation_only is enabled."""
        monkeypatch.setenv("AUTOMATION_ONLY", "true")

        mock_pr = MagicMock()
        mock_pr.user.login = "regular-user"

        gh = GitHubContext(
            event_name="pull_request",
            event_action="opened",
            event_path=None,
            repository="owner/repo",
            repository_owner="owner",
            server_url="https://github.com",
            run_id="123",
            sha="abc123",
            base_ref="main",
            head_ref="feature",
            pr_number=1,
        )

        # Should raise GitHub2GerritError after closing PR
        with pytest.raises(SystemExit):
            _check_automation_only(mock_pr, gh)

        # Verify PR was closed with correct comment
        mock_close_pr.assert_called_once()
        call_args = mock_close_pr.call_args
        assert call_args[0][0] == mock_pr
        comment = call_args[1]["comment"]
        assert "GitHub mirror does not accept pull requests" in comment
        assert "Gerrit server" in comment

    def test_missing_author_allows_pr(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PRs with missing author should be allowed (with warning)."""
        monkeypatch.setenv("AUTOMATION_ONLY", "true")

        mock_pr = MagicMock()
        mock_pr.user = None

        gh = GitHubContext(
            event_name="pull_request",
            event_action="opened",
            event_path=None,
            repository="owner/repo",
            repository_owner="owner",
            server_url="https://github.com",
            run_id="123",
            sha="abc123",
            base_ref="main",
            head_ref="feature",
            pr_number=1,
        )

        # Should not raise any exception
        _check_automation_only(mock_pr, gh)

    def test_dependabot_without_bot_suffix_allowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Dependabot without [bot] suffix should be allowed."""
        monkeypatch.setenv("AUTOMATION_ONLY", "true")

        mock_pr = MagicMock()
        mock_pr.user.login = "dependabot"

        gh = GitHubContext(
            event_name="pull_request",
            event_action="opened",
            event_path=None,
            repository="owner/repo",
            repository_owner="owner",
            server_url="https://github.com",
            run_id="123",
            sha="abc123",
            base_ref="main",
            head_ref="feature",
            pr_number=1,
        )

        # Should not raise any exception
        _check_automation_only(mock_pr, gh)

    def test_precommit_ci_without_bot_suffix_allowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pre-commit-ci without [bot] suffix should be allowed."""
        monkeypatch.setenv("AUTOMATION_ONLY", "true")

        mock_pr = MagicMock()
        mock_pr.user.login = "pre-commit-ci"

        gh = GitHubContext(
            event_name="pull_request",
            event_action="opened",
            event_path=None,
            repository="owner/repo",
            repository_owner="owner",
            server_url="https://github.com",
            run_id="123",
            sha="abc123",
            base_ref="main",
            head_ref="feature",
            pr_number=1,
        )

        # Should not raise any exception
        _check_automation_only(mock_pr, gh)

    @patch("github2gerrit.github_api.close_pr")
    def test_close_pr_comment_format(
        self, mock_close_pr: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify the exact format of the close PR comment."""
        monkeypatch.setenv("AUTOMATION_ONLY", "true")

        mock_pr = MagicMock()
        mock_pr.user.login = "some-user"

        gh = GitHubContext(
            event_name="pull_request",
            event_action="opened",
            event_path=None,
            repository="owner/repo",
            repository_owner="owner",
            server_url="https://github.com",
            run_id="123",
            sha="abc123",
            base_ref="main",
            head_ref="feature",
            pr_number=42,
        )

        # Should raise SystemExit after closing PR
        with pytest.raises(SystemExit):
            _check_automation_only(mock_pr, gh)

        # Verify exact comment format
        call_args = mock_close_pr.call_args
        comment = call_args[1]["comment"]
        expected_lines = [
            "This GitHub mirror does not accept pull requests.",
            "Please submit changes to the project's Gerrit server.",
        ]
        for line in expected_lines:
            assert line in comment
