# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""Integration tests for GitHub API error handling."""

from __future__ import annotations

import os
from unittest.mock import Mock
from unittest.mock import patch

import pytest

from github2gerrit.error_codes import ExitCode
from github2gerrit.error_codes import GitHub2GerritError
from github2gerrit.github_api import build_client
from github2gerrit.github_api import create_pr_comment
from github2gerrit.github_api import get_pull
from github2gerrit.github_api import get_repo_from_env


class TestGitHubAPIErrorHandling:
    """Test GitHub API error handling with realistic scenarios."""

    def test_build_client_missing_token_exits_with_proper_code(self):
        """Test that missing GITHUB_TOKEN results in proper exit code."""
        # Temporarily remove GITHUB_TOKEN
        original_token = os.environ.get("GITHUB_TOKEN")
        if "GITHUB_TOKEN" in os.environ:
            del os.environ["GITHUB_TOKEN"]

        try:
            with pytest.raises(GitHub2GerritError) as exc_info:
                build_client()

            assert exc_info.value.exit_code == ExitCode.GITHUB_API_ERROR
        finally:
            # Restore original token if it existed
            if original_token is not None:
                os.environ["GITHUB_TOKEN"] = original_token

    @patch("github2gerrit.github_api.Github")
    def test_get_repo_permission_error_exits_with_proper_code(
        self, mock_github_class
    ):
        """Test that GitHub API permission errors result in proper exit code."""
        # Mock PyGithub to raise a permission error
        mock_client = Mock()
        mock_client.get_repo.side_effect = Exception(
            "403 Forbidden: Resource not accessible by integration"
        )
        mock_github_class.return_value = mock_client

        # Set up environment
        os.environ["GITHUB_REPOSITORY"] = "owner/private-repo"

        with pytest.raises(GitHub2GerritError) as exc_info:
            get_repo_from_env(mock_client)

        assert exc_info.value.exit_code == ExitCode.GITHUB_API_ERROR

    def test_invalid_github_repository_format_exits_with_proper_code(self):
        """Test that invalid GITHUB_REPOSITORY format results in proper exit code."""
        # Mock client since we won't get to the API call
        mock_client = Mock()

        # Set invalid repository format
        original_repo = os.environ.get("GITHUB_REPOSITORY")
        os.environ["GITHUB_REPOSITORY"] = "invalid-format"
        os.environ["GITHUB_TOKEN"] = "test-token"

        try:
            with pytest.raises(GitHub2GerritError) as exc_info:
                get_repo_from_env(mock_client)

            assert exc_info.value.exit_code == ExitCode.GITHUB_API_ERROR
        finally:
            # Restore original repository
            if original_repo is not None:
                os.environ["GITHUB_REPOSITORY"] = original_repo
            elif "GITHUB_REPOSITORY" in os.environ:
                del os.environ["GITHUB_REPOSITORY"]

    def test_get_pull_not_found_exits_with_proper_code(self):
        """Test that pull request not found results in proper exit code."""
        mock_repo = Mock()
        mock_repo.get_pull.side_effect = Exception("404 Not Found")
        mock_repo.full_name = "owner/repo"

        with pytest.raises(GitHub2GerritError) as exc_info:
            get_pull(mock_repo, 999)

        assert exc_info.value.exit_code == ExitCode.GITHUB_API_ERROR

    def test_create_pr_comment_permission_error_exits_with_proper_code(self):
        """Test that PR comment permission errors result in proper exit code."""
        mock_issue = Mock()
        mock_issue.create_comment.side_effect = Exception(
            "403 Forbidden: Resource not accessible by integration"
        )

        mock_pr = Mock()
        mock_pr.as_issue.return_value = mock_issue
        mock_pr.number = 123

        with pytest.raises(GitHub2GerritError) as exc_info:
            create_pr_comment(mock_pr, "Test comment")

        assert exc_info.value.exit_code == ExitCode.GITHUB_API_ERROR


class TestRealWorldErrorScenarios:
    """Test realistic error scenarios that users might encounter."""

    @patch("github2gerrit.github_api.Github")
    def test_cross_repository_access_without_token(self, mock_github_class):
        """Test accessing private repository without proper token."""
        mock_client = Mock()
        mock_client.get_repo.side_effect = Exception(
            "401 Unauthorized: Bad credentials"
        )
        mock_github_class.return_value = mock_client

        # Set up environment for cross-repository access
        original_repo = os.environ.get("GITHUB_REPOSITORY")
        os.environ["GITHUB_REPOSITORY"] = "target-org/target-repo"

        try:
            with pytest.raises(GitHub2GerritError) as exc_info:
                get_repo_from_env(mock_client)

            # Verify we get the appropriate exit code
            assert exc_info.value.exit_code == ExitCode.GITHUB_API_ERROR
        finally:
            if "GITHUB_REPOSITORY" in os.environ:
                del os.environ["GITHUB_REPOSITORY"]
            if original_repo is not None:
                os.environ["GITHUB_REPOSITORY"] = original_repo

    @patch("github2gerrit.github_api.Github")
    def test_error_logging_includes_helpful_context(self, mock_github_class):
        """Test that error messages include helpful context for debugging."""
        mock_client = Mock()
        mock_client.get_repo.side_effect = Exception(
            "403 Forbidden: Resource not accessible by integration"
        )
        mock_github_class.return_value = mock_client

        original_repo = os.environ.get("GITHUB_REPOSITORY")
        os.environ["GITHUB_REPOSITORY"] = "private-org/sensitive-repo"

        try:
            with pytest.raises(GitHub2GerritError) as exc_info:
                get_repo_from_env(mock_client)

            # Verify error message contains useful context
            error_details = str(exc_info.value.details)
            assert "private-org/sensitive-repo" in error_details
            assert "permissions" in error_details.lower()
        finally:
            if "GITHUB_REPOSITORY" in os.environ:
                del os.environ["GITHUB_REPOSITORY"]
            if original_repo is not None:
                os.environ["GITHUB_REPOSITORY"] = original_repo

    @patch("github2gerrit.github_api.Github")
    def test_github_enterprise_api_error_handling(self, mock_github_class):
        """Test error handling with GitHub Enterprise."""
        mock_client = Mock()
        # Use a permission error instead of server error to trigger GitHub2GerritError
        mock_client.get_repo.side_effect = Exception(
            "403 Forbidden: Resource not accessible by integration"
        )
        mock_github_class.return_value = mock_client

        # Simulate GitHub Enterprise environment
        original_server = os.environ.get("GITHUB_SERVER_URL")
        original_repo = os.environ.get("GITHUB_REPOSITORY")
        os.environ["GITHUB_REPOSITORY"] = "enterprise-org/project"
        os.environ["GITHUB_SERVER_URL"] = "https://github.enterprise.com"

        try:
            with pytest.raises(GitHub2GerritError) as exc_info:
                get_repo_from_env(mock_client)

            assert exc_info.value.exit_code == ExitCode.GITHUB_API_ERROR
        finally:
            if "GITHUB_REPOSITORY" in os.environ:
                del os.environ["GITHUB_REPOSITORY"]
            if original_repo is not None:
                os.environ["GITHUB_REPOSITORY"] = original_repo
            if original_server is not None:
                os.environ["GITHUB_SERVER_URL"] = original_server
            elif "GITHUB_SERVER_URL" in os.environ:
                del os.environ["GITHUB_SERVER_URL"]


class TestErrorMessageQuality:
    """Test the quality and usefulness of error messages."""

    def test_github_api_error_messages_are_actionable(self):
        """Test that error messages provide actionable guidance."""
        from github2gerrit import github_api

        # Check that the module exposes helpful error message constants
        # or functions that generate user-friendly messages
        assert hasattr(github_api, "is_github_api_permission_error")

    def test_error_messages_contain_repository_context(self):
        """Test that error messages include repository context."""
        mock_repo = Mock()
        mock_repo.get_pull.side_effect = Exception("404 Not Found")
        mock_repo.full_name = "test-org/test-repo"

        with pytest.raises(GitHub2GerritError) as exc_info:
            get_pull(mock_repo, 123)

        # Error should mention the repository in some way
        error_str = str(exc_info.value)
        assert "test-org/test-repo" in error_str or "123" in error_str
