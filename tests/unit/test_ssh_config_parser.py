# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""
Tests for SSH configuration parser and credential derivation.

This module tests the SSH config parsing logic that extracts user settings
for Gerrit hosts, enabling personalized username derivation instead of
relying solely on organization-based defaults.
"""

import subprocess
from unittest import mock

import pytest

from github2gerrit.ssh_config_parser import SSHConfig
from github2gerrit.ssh_config_parser import clear_credential_cache
from github2gerrit.ssh_config_parser import derive_gerrit_credentials
from github2gerrit.ssh_config_parser import get_git_user_email
from github2gerrit.ssh_config_parser import get_ssh_user_for_gerrit


class TestSSHConfig:
    """Test cases for SSH configuration parsing."""

    def setup_method(self):
        """Clear caches before each test to ensure isolation."""
        clear_credential_cache()

    def test_empty_config(self, tmp_path):
        """Test handling of empty SSH config file."""
        config_file = tmp_path / "config"
        config_file.write_text("")

        ssh_config = SSHConfig(config_file)
        user = ssh_config.get_user_for_host("gerrit.example.com")

        assert user is None

    def test_nonexistent_config(self, tmp_path):
        """Test handling of nonexistent SSH config file."""
        config_file = tmp_path / "nonexistent"

        ssh_config = SSHConfig(config_file)
        user = ssh_config.get_user_for_host("gerrit.example.com")

        assert user is None

    def test_basic_host_matching(self, tmp_path):
        """Test basic host pattern matching."""
        config_content = """
Host gerrit.example.com
    User testuser
    Port 29418

Host github.com
    User gituser
"""
        config_file = tmp_path / "config"
        config_file.write_text(config_content)

        ssh_config = SSHConfig(config_file)

        # Test exact match
        assert ssh_config.get_user_for_host("gerrit.example.com") == "testuser"
        assert ssh_config.get_user_for_host("github.com") == "gituser"

        # Test no match
        assert ssh_config.get_user_for_host("unknown.com") is None

    def test_wildcard_host_matching(self, tmp_path):
        """Test wildcard pattern matching in host entries."""
        config_content = """
Host gerrit.*
    User gerrituser
    Port 29418

Host *.linuxfoundation.org
    User lfuser

Host *
    User defaultuser
"""
        config_file = tmp_path / "config"
        config_file.write_text(config_content)

        ssh_config = SSHConfig(config_file)

        # Test wildcard matching
        assert (
            ssh_config.get_user_for_host("gerrit.example.com") == "gerrituser"
        )
        assert (
            ssh_config.get_user_for_host("gerrit.linuxfoundation.org")
            == "gerrituser"
        )
        assert (
            ssh_config.get_user_for_host("test.linuxfoundation.org") == "lfuser"
        )
        assert (
            ssh_config.get_user_for_host("random.example.com") == "defaultuser"
        )

    def test_host_precedence(self, tmp_path):
        """Test that SSH config respects host entry precedence."""
        config_content = """
Host gerrit.specific.com
    User specificuser

Host gerrit.*
    User wildcarduser

Host *
    User defaultuser
"""
        config_file = tmp_path / "config"
        config_file.write_text(config_content)

        ssh_config = SSHConfig(config_file)

        # More specific pattern should win
        assert (
            ssh_config.get_user_for_host("gerrit.specific.com")
            == "specificuser"
        )
        assert (
            ssh_config.get_user_for_host("gerrit.other.com") == "wildcarduser"
        )
        assert (
            ssh_config.get_user_for_host("other.example.com") == "defaultuser"
        )

    def test_port_specific_matching(self, tmp_path):
        """Test port-specific SSH config matching."""
        config_content = """
Host gerrit.example.com
    User normaluser
    Port 22

Host gerrit.example.com
    User gerrituser
    Port 29418
"""
        config_file = tmp_path / "config"
        config_file.write_text(config_content)

        ssh_config = SSHConfig(config_file)

        # Test port-specific matching
        assert (
            ssh_config.get_user_for_host("gerrit.example.com", 22)
            == "normaluser"
        )
        assert (
            ssh_config.get_user_for_host("gerrit.example.com", 29418)
            == "gerrituser"
        )
        assert (
            ssh_config.get_user_for_host("gerrit.example.com") == "normaluser"
        )

    def test_comments_and_empty_lines(self, tmp_path):
        """Test that comments and empty lines are properly ignored."""
        config_content = """
# Global settings
# This is a comment

Host gerrit.*
    # Another comment
    User gerrituser
    Port 29418
    # Comment after directive

# Final comment
Host github.com
    User githubuser
"""
        config_file = tmp_path / "config"
        config_file.write_text(config_content)

        ssh_config = SSHConfig(config_file)

        assert (
            ssh_config.get_user_for_host("gerrit.example.com") == "gerrituser"
        )
        assert ssh_config.get_user_for_host("github.com") == "githubuser"

    def test_quoted_values(self, tmp_path):
        """Test handling of quoted values in SSH config."""
        config_content = """
Host "gerrit with spaces"
    User "user with spaces"

Host gerrit.example.com
    User regularuser
"""
        config_file = tmp_path / "config"
        config_file.write_text(config_content)

        ssh_config = SSHConfig(config_file)

        assert (
            ssh_config.get_user_for_host("gerrit with spaces")
            == "user with spaces"
        )
        assert (
            ssh_config.get_user_for_host("gerrit.example.com") == "regularuser"
        )

    def test_multiple_host_patterns(self, tmp_path):
        """Test multiple host patterns in a single Host entry."""
        config_content = """
Host gerrit.example.com gerrit.test.com *.gerrit.org
    User multiuser
    Port 29418

Host github.com gitlab.com
    User gituser
"""
        config_file = tmp_path / "config"
        config_file.write_text(config_content)

        ssh_config = SSHConfig(config_file)

        assert ssh_config.get_user_for_host("gerrit.example.com") == "multiuser"
        assert ssh_config.get_user_for_host("gerrit.test.com") == "multiuser"
        assert ssh_config.get_user_for_host("test.gerrit.org") == "multiuser"
        assert ssh_config.get_user_for_host("github.com") == "gituser"
        assert ssh_config.get_user_for_host("gitlab.com") == "gituser"

    def test_real_world_config(self, tmp_path):
        """Test with a realistic SSH config similar to the user's example."""
        config_content = """
# Global settings
CanonicalDomains example.org.vpn.net corp.local
CanonicalizeHostname yes
ControlMaster auto
ControlPersist 10m
ServerAliveInterval 120
ForwardAgent yes

Host mailserver server1.example.org server2.example.org
    User admin
    IdentityFile ~/.ssh/mailserver

Host gerrit.*
    User gerritbot
    Port 29418
    HostkeyAlgorithms +ssh-rsa
    PubkeyAcceptedAlgorithms +ssh-rsa

Host git.upstream.org
    User gerritbot
    Port 29418
    HostkeyAlgorithms +ssh-rsa
    PubkeyAcceptedAlgorithms +ssh-rsa

Host github.com
    User GitHubUser

Host *.example.org.vpn.net *.corp.local
    User corpuser

Host *
    User defaultuser
    IdentityAgent /path/to/secretive/socket.ssh
"""
        config_file = tmp_path / "config"
        config_file.write_text(config_content)

        ssh_config = SSHConfig(config_file)

        # Test various host patterns
        assert ssh_config.get_user_for_host("gerrit.example.org") == "gerritbot"
        assert ssh_config.get_user_for_host("gerrit.example.com") == "gerritbot"
        assert ssh_config.get_user_for_host("git.upstream.org") == "gerritbot"
        assert ssh_config.get_user_for_host("github.com") == "GitHubUser"
        assert (
            ssh_config.get_user_for_host("test.example.org.vpn.net")
            == "corpuser"
        )
        assert ssh_config.get_user_for_host("example.corp.local") == "corpuser"
        assert (
            ssh_config.get_user_for_host("random.example.com") == "defaultuser"
        )

    def test_case_insensitive_directives(self, tmp_path):
        """Test that SSH config directives are case-insensitive."""
        config_content = """
HOST gerrit.example.com
    USER testuser
    PORT 29418
"""
        config_file = tmp_path / "config"
        config_file.write_text(config_content)

        ssh_config = SSHConfig(config_file)

        assert ssh_config.get_user_for_host("gerrit.example.com") == "testuser"


class TestConvenienceFunctions:
    """Test cases for convenience functions."""

    def setup_method(self):
        """Clear caches before each test to ensure isolation."""
        clear_credential_cache()

    def test_get_ssh_user_for_gerrit(self, tmp_path):
        """Test the convenience function for getting Gerrit SSH user."""
        config_content = """
Host gerrit.*
    User gerrituser
    Port 29418
"""
        config_file = tmp_path / "config"
        config_file.write_text(config_content)

        # Mock SSHConfig to use our temporary config file
        with mock.patch(
            "github2gerrit.ssh_config_parser.SSHConfig"
        ) as mock_ssh_config:
            mock_ssh_config.return_value = SSHConfig(config_path=config_file)
            user = get_ssh_user_for_gerrit("gerrit.linuxfoundation.org")
            assert user == "gerrituser"

    @mock.patch("github2gerrit.ssh_config_parser._validate_git_executable")
    @mock.patch("subprocess.run")
    @mock.patch("shutil.which")
    def test_get_git_user_email_success(
        self, mock_which, mock_run, mock_validate
    ):
        """Test successful git user email retrieval."""
        mock_which.return_value = "git"
        mock_validate.return_value = True
        mock_run.return_value = mock.MagicMock(
            returncode=0, stdout="user@example.com\n"
        )

        email = get_git_user_email()
        assert email == "user@example.com"

        mock_which.assert_called_once_with("git")
        mock_validate.assert_called_once_with("git")
        mock_run.assert_called_once_with(
            ["git", "config", "user.email"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )

    @mock.patch("github2gerrit.ssh_config_parser._validate_git_executable")
    @mock.patch("subprocess.run")
    @mock.patch("shutil.which")
    def test_get_git_user_email_not_configured(
        self, mock_which, mock_run, mock_validate
    ):
        """Test git user email when not configured."""
        mock_which.return_value = "git"
        mock_validate.return_value = True
        mock_run.return_value = mock.MagicMock(returncode=1, stdout="")

        email = get_git_user_email()
        assert email is None

    @mock.patch("github2gerrit.ssh_config_parser._validate_git_executable")
    @mock.patch("subprocess.run")
    @mock.patch("shutil.which")
    def test_get_git_user_email_command_not_found(
        self, mock_which, mock_run, mock_validate
    ):
        """Test git user email when git command not found."""
        mock_which.return_value = None

        email = get_git_user_email()
        assert email is None

        mock_which.assert_called_once_with("git")
        mock_validate.assert_not_called()

    @mock.patch("github2gerrit.ssh_config_parser._validate_git_executable")
    @mock.patch("subprocess.run")
    @mock.patch("shutil.which")
    def test_get_git_user_email_validation_failed(
        self, mock_which, mock_run, mock_validate
    ):
        """Test git user email when git validation fails."""
        mock_which.return_value = "/fake/git"
        mock_validate.return_value = False

        email = get_git_user_email()
        assert email is None

        mock_which.assert_called_once_with("git")
        mock_validate.assert_called_once_with("/fake/git")
        mock_run.assert_not_called()

    @mock.patch("github2gerrit.ssh_config_parser._validate_git_executable")
    @mock.patch("subprocess.run")
    @mock.patch("shutil.which")
    def test_get_git_user_email_timeout(
        self, mock_which, mock_run, mock_validate
    ):
        """Test git user email with timeout."""
        mock_which.return_value = "git"
        mock_validate.return_value = True
        mock_run.side_effect = subprocess.TimeoutExpired("git", 5)

        email = get_git_user_email()
        assert email is None

    def test_validate_git_executable_valid_git(self):
        """Test git executable validation with actual git."""
        import shutil

        from github2gerrit.ssh_config_parser import _validate_git_executable

        git_path = shutil.which("git")
        if git_path:
            # Should validate successfully with real git
            assert _validate_git_executable(git_path) is True
        else:
            pytest.skip("Git not available in PATH")

    def test_validate_git_executable_invalid_path(self):
        """Test git executable validation with invalid path."""
        from github2gerrit.ssh_config_parser import _validate_git_executable

        # Non-existent path should fail
        assert _validate_git_executable("/nonexistent/path/to/git") is False

    @mock.patch("subprocess.run")
    def test_validate_git_executable_not_git(self, mock_run):
        """Test git executable validation with non-git executable."""
        import os
        import tempfile

        from github2gerrit.ssh_config_parser import _validate_git_executable

        # Create a temporary executable file that's not git
        with tempfile.NamedTemporaryFile(
            mode="w", delete=False, suffix="_fake_git"
        ) as f:
            f.write('#!/bin/bash\necho "fake program"\n')
            fake_git_path = f.name

        try:
            # Make it executable
            os.chmod(
                fake_git_path, 0o700
            )  # More restrictive permissions for test

            # Mock subprocess to return non-git output
            mock_run.return_value = mock.MagicMock(
                returncode=0, stdout="fake program version 1.0"
            )

            # Should fail validation since it doesn't respond like git
            assert _validate_git_executable(fake_git_path) is False

        finally:
            os.unlink(fake_git_path)

    @mock.patch("subprocess.run")
    def test_validate_git_executable_timeout(self, mock_run):
        """Test git executable validation with timeout."""
        import os
        import subprocess
        import tempfile

        from github2gerrit.ssh_config_parser import _validate_git_executable

        # Create a temporary executable file
        with tempfile.NamedTemporaryFile(
            mode="w", delete=False, suffix="_timeout_git"
        ) as f:
            f.write("#!/bin/bash\nsleep 10\n")
            fake_git_path = f.name

        try:
            # Make it executable
            os.chmod(
                fake_git_path, 0o700
            )  # More restrictive permissions for test

            # Mock subprocess to timeout
            mock_run.side_effect = subprocess.TimeoutExpired("fake_git", 3)

            # Should fail validation on timeout
            assert _validate_git_executable(fake_git_path) is False

        finally:
            os.unlink(fake_git_path)


class TestCredentialDerivation:
    """Test cases for credential derivation logic."""

    def setup_method(self):
        """Clear caches before each test to ensure isolation."""
        clear_credential_cache()

    @mock.patch("github2gerrit.ssh_config_parser.get_git_user_email")
    @mock.patch("github2gerrit.ssh_config_parser.get_ssh_user_for_gerrit")
    @mock.patch.dict("os.environ", {"G2G_RESPECT_USER_SSH": "true"})
    def test_derive_gerrit_credentials_ssh_and_git(
        self, mock_ssh_user, mock_git_email
    ):
        """Test credential derivation when both SSH and git config are available."""
        mock_ssh_user.return_value = "sshuser"
        mock_git_email.return_value = "user@example.com"

        user, email = derive_gerrit_credentials("gerrit.example.com", "testorg")

        assert user == "sshuser"
        assert email == "user@example.com"

        mock_ssh_user.assert_called_once_with("gerrit.example.com", 29418)
        mock_git_email.assert_called_once()

    @mock.patch("github2gerrit.ssh_config_parser.get_git_user_email")
    @mock.patch("github2gerrit.ssh_config_parser.get_ssh_user_for_gerrit")
    @mock.patch.dict("os.environ", {"G2G_RESPECT_USER_SSH": "true"})
    def test_derive_gerrit_credentials_ssh_only(
        self, mock_ssh_user, mock_git_email
    ):
        """Test credential derivation with SSH config only."""
        mock_ssh_user.return_value = "sshuser"
        mock_git_email.return_value = None

        user, email = derive_gerrit_credentials("gerrit.example.com", "testorg")

        assert user == "sshuser"
        assert email == "releng+testorg-gh2gerrit@linuxfoundation.org"

    @mock.patch("github2gerrit.ssh_config_parser.get_git_user_email")
    @mock.patch("github2gerrit.ssh_config_parser.get_ssh_user_for_gerrit")
    @mock.patch.dict("os.environ", {"G2G_RESPECT_USER_SSH": "true"})
    def test_derive_gerrit_credentials_git_only(
        self, mock_ssh_user, mock_git_email
    ):
        """Test credential derivation with git config only."""
        mock_ssh_user.return_value = None
        mock_git_email.return_value = "user@example.com"

        user, email = derive_gerrit_credentials("gerrit.example.com", "testorg")

        assert user == "testorg.gh2gerrit"
        assert email == "user@example.com"

    @mock.patch("github2gerrit.ssh_config_parser.get_git_user_email")
    @mock.patch("github2gerrit.ssh_config_parser.get_ssh_user_for_gerrit")
    def test_derive_gerrit_credentials_fallback(
        self, mock_ssh_user, mock_git_email
    ):
        """Test credential derivation fallback to organization-based."""
        mock_ssh_user.return_value = None
        mock_git_email.return_value = None

        user, email = derive_gerrit_credentials("gerrit.example.com", "testorg")

        assert user == "testorg.gh2gerrit"
        assert email == "releng+testorg-gh2gerrit@linuxfoundation.org"

    @mock.patch.dict("os.environ", {"G2G_RESPECT_USER_SSH": "true"})
    def test_derive_gerrit_credentials_custom_port(self):
        """Test credential derivation with custom port."""
        with (
            mock.patch(
                "github2gerrit.ssh_config_parser.get_ssh_user_for_gerrit"
            ) as mock_ssh_user,
            mock.patch(
                "github2gerrit.ssh_config_parser.get_git_user_email"
            ) as mock_git_email,
        ):
            mock_ssh_user.return_value = "sshuser"
            mock_git_email.return_value = "user@example.com"

            user, email = derive_gerrit_credentials(
                "gerrit.example.com", "testorg", 2222
            )

            assert user == "sshuser"
            assert email == "user@example.com"

            mock_ssh_user.assert_called_once_with("gerrit.example.com", 2222)


class TestPatternMatching:
    """Test cases for SSH host pattern matching."""

    def setup_method(self):
        """Clear caches before each test to ensure isolation."""
        clear_credential_cache()

    def test_pattern_matching_exact(self):
        """Test exact host pattern matching."""
        ssh_config = SSHConfig()

        assert (
            ssh_config._pattern_matches(
                "gerrit.example.com", "gerrit.example.com"
            )
            is True
        )
        assert (
            ssh_config._pattern_matches(
                "gerrit.example.com", "gerrit.other.com"
            )
            is False
        )

    def test_pattern_matching_wildcards(self):
        """Test wildcard pattern matching."""
        ssh_config = SSHConfig()

        # Test * wildcard
        assert (
            ssh_config._pattern_matches("gerrit.example.com", "gerrit.*")
            is True
        )
        assert (
            ssh_config._pattern_matches(
                "gerrit.linuxfoundation.org", "gerrit.*"
            )
            is True
        )
        assert ssh_config._pattern_matches("github.com", "gerrit.*") is False

        # Test ? wildcard
        assert (
            ssh_config._pattern_matches(
                "gerrit1.example.com", "gerrit?.example.com"
            )
            is True
        )
        assert (
            ssh_config._pattern_matches(
                "gerrit22.example.com", "gerrit?.example.com"
            )
            is False
        )

        # Test suffix wildcard
        assert (
            ssh_config._pattern_matches(
                "test.linuxfoundation.org", "*.linuxfoundation.org"
            )
            is True
        )
        assert (
            ssh_config._pattern_matches(
                "gerrit.linuxfoundation.org", "*.linuxfoundation.org"
            )
            is True
        )
        assert (
            ssh_config._pattern_matches(
                "linuxfoundation.org", "*.linuxfoundation.org"
            )
            is False
        )

    def test_pattern_matching_quoted(self):
        """Test pattern matching with quoted patterns."""
        ssh_config = SSHConfig()

        assert (
            ssh_config._pattern_matches(
                "gerrit.example.com", '"gerrit.example.com"'
            )
            is True
        )
        assert (
            ssh_config._pattern_matches(
                "gerrit.example.com", "'gerrit.example.com'"
            )
            is True
        )

    def test_pattern_matching_global_wildcard(self):
        """Test global wildcard pattern."""
        ssh_config = SSHConfig()

        assert ssh_config._pattern_matches("any.host.com", "*") is True
        assert ssh_config._pattern_matches("gerrit.example.com", "*") is True


class TestConfigLineHandling:
    """Test cases for SSH config line parsing."""

    def setup_method(self):
        """Clear caches before each test to ensure isolation."""
        clear_credential_cache()

    def test_split_config_line_basic(self):
        """Test basic config line splitting."""
        ssh_config = SSHConfig()

        result = ssh_config._split_config_line("Host gerrit.example.com")
        assert result == ["Host", "gerrit.example.com"]

        result = ssh_config._split_config_line("    User testuser")
        assert result == ["User", "testuser"]

    def test_split_config_line_quoted(self):
        """Test config line splitting with quotes."""
        ssh_config = SSHConfig()

        result = ssh_config._split_config_line('Host "gerrit with spaces"')
        assert result == ["Host", "gerrit with spaces"]

        result = ssh_config._split_config_line('User "user with spaces"')
        assert result == ["User", "user with spaces"]

    def test_split_config_line_multiple_spaces(self):
        """Test config line splitting with multiple spaces."""
        ssh_config = SSHConfig()

        result = ssh_config._split_config_line(
            "Host   gerrit.example.com    gerrit.test.com"
        )
        assert result == ["Host", "gerrit.example.com", "gerrit.test.com"]


@pytest.fixture
def sample_ssh_config():
    """Fixture providing a realistic SSH configuration for testing."""
    return """
# Global settings
CanonicalDomains example.org.vpn.net corp.local
CanonicalizeHostname yes
ControlMaster auto
ControlPersist 10m
ServerAliveInterval 120
ForwardAgent yes

Host mailserver server1.example.org server2.example.org
    User admin
    IdentityFile ~/.ssh/mailserver

Host gerrit.*
    User gerritbot
    Port 29418
    HostkeyAlgorithms +ssh-rsa
    PubkeyAcceptedAlgorithms +ssh-rsa

Host git.upstream.org
    User gerritbot
    Port 29418
    HostkeyAlgorithms +ssh-rsa
    PubkeyAcceptedAlgorithms +ssh-rsa

Host github.com
    User GitHubUser

Host *.example.org.vpn.net *.corp.local
    User corpuser

Host *
    User defaultuser
    IdentityAgent /path/to/secretive/socket.ssh
"""


class TestIntegration:
    """Integration tests using realistic configurations."""

    def setup_method(self):
        """Clear caches before each test to ensure isolation."""
        clear_credential_cache()

    @mock.patch.dict("os.environ", {"G2G_RESPECT_USER_SSH": "true"})
    def test_lfit_organization_config(self, tmp_path, sample_ssh_config):
        """Test credential derivation for lfit organization with real SSH config."""
        config_file = tmp_path / "config"
        config_file.write_text(sample_ssh_config)

        with mock.patch(
            "github2gerrit.ssh_config_parser.Path.home"
        ) as mock_home:
            mock_home.return_value = tmp_path.parent
            ssh_dir = tmp_path.parent / ".ssh"
            ssh_dir.mkdir(exist_ok=True)
            (ssh_dir / "config").write_text(sample_ssh_config)

            with mock.patch(
                "github2gerrit.ssh_config_parser.get_git_user_email"
            ) as mock_git:
                mock_git.return_value = "user@example.org"

                user, email = derive_gerrit_credentials(
                    "gerrit.example.org", "exampleorg"
                )

                assert user == "gerritbot"
                assert email == "user@example.org"

    @mock.patch.dict("os.environ", {"G2G_RESPECT_USER_SSH": "true"})
    def test_unknown_organization_fallback(self, tmp_path, sample_ssh_config):
        """Test fallback behavior for unknown organization."""
        config_file = tmp_path / "config"
        config_file.write_text(sample_ssh_config)

        with mock.patch(
            "github2gerrit.ssh_config_parser.Path.home"
        ) as mock_home:
            mock_home.return_value = tmp_path.parent
            ssh_dir = tmp_path.parent / ".ssh"
            ssh_dir.mkdir(exist_ok=True)
            (ssh_dir / "config").write_text(sample_ssh_config)

            with mock.patch(
                "github2gerrit.ssh_config_parser.get_git_user_email"
            ) as mock_git:
                mock_git.return_value = None

                # Test with host that matches gerrit.* pattern
                user, email = derive_gerrit_credentials(
                    "gerrit.unknown.org", "unknownorg"
                )

                assert user == "gerritbot"  # From gerrit.* pattern
                assert (
                    email == "releng+unknownorg-gh2gerrit@linuxfoundation.org"
                )


class TestCaching:
    """Test cases for caching behavior in SSH config parsing and credential derivation."""

    def setup_method(self):
        """Clear caches before each test to ensure isolation."""
        clear_credential_cache()

    @mock.patch("github2gerrit.ssh_config_parser.get_git_user_email")
    @mock.patch("github2gerrit.ssh_config_parser.get_ssh_user_for_gerrit")
    @mock.patch("github2gerrit.ssh_config_parser._get_respect_user_ssh_setting")
    def test_derive_gerrit_credentials_caching(
        self, mock_respect_ssh, mock_ssh_user, mock_git_email
    ):
        """Test that derive_gerrit_credentials caches results to avoid repeated calls."""
        # Set up mocks to enable SSH config usage
        mock_respect_ssh.return_value = True
        mock_ssh_user.return_value = "sshuser"
        mock_git_email.return_value = "user@example.com"

        # Call the function twice with the same parameters
        user1, email1 = derive_gerrit_credentials(
            "gerrit.example.com", "testorg"
        )
        user2, email2 = derive_gerrit_credentials(
            "gerrit.example.com", "testorg"
        )

        # Results should be identical
        assert user1 == user2 == "sshuser"
        assert email1 == email2 == "user@example.com"

        # The underlying functions should only be called once due to caching
        # Note: mock_respect_ssh will be called twice because it's also cached separately
        assert mock_ssh_user.call_count == 1
        assert mock_git_email.call_count == 1

    @mock.patch("github2gerrit.ssh_config_parser.env_bool")
    def test_respect_user_ssh_setting_caching(self, mock_env_bool):
        """Test that G2G_RESPECT_USER_SSH environment variable is cached."""
        mock_env_bool.return_value = True

        # Import the cached function
        from github2gerrit.ssh_config_parser import (
            _get_respect_user_ssh_setting,
        )

        # Call multiple times
        result1 = _get_respect_user_ssh_setting()
        result2 = _get_respect_user_ssh_setting()
        result3 = _get_respect_user_ssh_setting()

        # All should return the same value
        assert result1 == result2 == result3 is True

        # env_bool should only be called once due to caching
        assert mock_env_bool.call_count == 1

    def test_clear_credential_cache_functionality(self):
        """Test that clear_credential_cache properly clears all caches."""
        # Import the cache clearing function and cached functions
        from github2gerrit.ssh_config_parser import _get_cached_git_user_email
        from github2gerrit.ssh_config_parser import (
            _get_respect_user_ssh_setting,
        )
        from github2gerrit.ssh_config_parser import clear_credential_cache

        # Call functions to populate caches (using mocks to avoid actual system calls)
        with mock.patch(
            "github2gerrit.ssh_config_parser.env_bool"
        ) as mock_env_bool:
            mock_env_bool.return_value = True
            _get_respect_user_ssh_setting()

        with mock.patch(
            "github2gerrit.ssh_config_parser.get_git_user_email"
        ) as mock_git_email:
            mock_git_email.return_value = "test@example.com"
            _get_cached_git_user_email()

        with mock.patch(
            "github2gerrit.ssh_config_parser.get_ssh_user_for_gerrit"
        ) as mock_ssh_user:
            mock_ssh_user.return_value = "testuser"
            derive_gerrit_credentials("gerrit.example.com", "testorg")

        # Clear all caches
        clear_credential_cache()

        # Verify caches are cleared by checking cache info
        assert _get_respect_user_ssh_setting.cache_info().currsize == 0
        assert _get_cached_git_user_email.cache_info().currsize == 0
        assert derive_gerrit_credentials.cache_info().currsize == 0
