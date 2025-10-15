# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""Tests for SSH agent ownership tracking."""

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from github2gerrit.ssh_agent_setup import SSHAgentManager


class TestSSHAgentOwnership:
    """Test SSH agent ownership tracking to prevent killing borrowed agents."""

    def test_initial_state_not_owned(self):
        """Test that newly created manager shows no ownership."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager = SSHAgentManager(workspace=Path(tmp_dir))

            assert manager._agent_owned_by_us is False
            assert manager.agent_pid is None
            assert manager.auth_sock is None

    @patch("github2gerrit.ssh_agent_setup.run_cmd")
    @patch("github2gerrit.ssh_agent_setup._ensure_tool_available")
    def test_start_agent_sets_ownership(self, mock_ensure_tool, mock_run_cmd):
        """Test that starting a new agent sets ownership flag."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager = SSHAgentManager(workspace=Path(tmp_dir))

            # Mock ssh-agent output
            mock_ensure_tool.return_value = "/usr/bin/ssh-agent"
            mock_run_cmd.return_value = MagicMock(
                stdout=f"SSH_AUTH_SOCK={tmp_dir}/ssh-agent.sock; export SSH_AUTH_SOCK;\nSSH_AGENT_PID=12345; export SSH_AGENT_PID;\n"
            )

            # Store original environment
            original_env = os.environ.copy()

            try:
                manager.start_agent()

                assert manager._agent_owned_by_us is True
                assert manager.agent_pid == 12345
                assert manager.auth_sock == f"{tmp_dir}/ssh-agent.sock"

            finally:
                # Restore environment
                os.environ.clear()
                os.environ.update(original_env)

    @patch("subprocess.run")
    @patch("github2gerrit.ssh_agent_setup._ensure_tool_available")
    @patch("os.path.exists")
    def test_use_existing_agent_no_ownership(
        self, mock_exists, mock_ensure_tool, mock_subprocess
    ):
        """Test that using existing agent does not set ownership."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager = SSHAgentManager(workspace=Path(tmp_dir))

            # Mock environment and file system
            mock_exists.return_value = True
            mock_ensure_tool.return_value = "/usr/bin/ssh-add"
            mock_subprocess.return_value = MagicMock(
                returncode=0,
                stdout="2048 SHA256:abc123 test@example.com (RSA)\n",
            )

            # Set up environment to simulate existing agent
            original_env = os.environ.copy()
            os.environ["SSH_AUTH_SOCK"] = f"{tmp_dir}/existing-agent.sock"
            os.environ["SSH_AGENT_PID"] = "54321"

            try:
                result = manager.use_existing_agent()

                assert result is True
                assert manager._agent_owned_by_us is False
                assert manager.agent_pid == 54321  # Should store PID for info
                assert manager.auth_sock == f"{tmp_dir}/existing-agent.sock"

            finally:
                # Restore environment
                os.environ.clear()
                os.environ.update(original_env)

    @patch("subprocess.run")
    @patch("github2gerrit.ssh_agent_setup._ensure_tool_available")
    @patch("os.path.exists")
    def test_use_existing_agent_no_pid_in_env(
        self, mock_exists, mock_ensure_tool, mock_subprocess
    ):
        """Test using existing agent when SSH_AGENT_PID is not set."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager = SSHAgentManager(workspace=Path(tmp_dir))

            # Mock environment and file system
            mock_exists.return_value = True
            mock_ensure_tool.return_value = "/usr/bin/ssh-add"
            mock_subprocess.return_value = MagicMock(
                returncode=0,
                stdout="2048 SHA256:abc123 test@example.com (RSA)\n",
            )

            # Set up environment to simulate existing agent without PID
            original_env = os.environ.copy()
            os.environ["SSH_AUTH_SOCK"] = f"{tmp_dir}/existing-agent.sock"
            if "SSH_AGENT_PID" in os.environ:
                del os.environ["SSH_AGENT_PID"]

            try:
                result = manager.use_existing_agent()

                assert result is True
                assert manager._agent_owned_by_us is False
                assert manager.agent_pid is None  # No PID available
                assert manager.auth_sock == f"{tmp_dir}/existing-agent.sock"

            finally:
                # Restore environment
                os.environ.clear()
                os.environ.update(original_env)

    @patch("github2gerrit.ssh_agent_setup.run_cmd")
    def test_cleanup_kills_owned_agent(self, mock_run_cmd):
        """Test that cleanup kills agents we own."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager = SSHAgentManager(workspace=Path(tmp_dir))

            # Simulate owned agent
            manager.agent_pid = 12345
            manager.auth_sock = f"{tmp_dir}/ssh-agent.sock"
            manager._agent_owned_by_us = True

            manager.cleanup()

            # Should have tried to kill the agent
            mock_run_cmd.assert_called_once_with(
                ["/bin/kill", "12345"], timeout=5
            )

            # Should reset state
            assert manager.agent_pid is None
            assert manager.auth_sock is None
            assert manager._agent_owned_by_us is False

    @patch("github2gerrit.ssh_agent_setup.run_cmd")
    def test_cleanup_does_not_kill_borrowed_agent(self, mock_run_cmd):
        """Test that cleanup does not kill borrowed agents."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager = SSHAgentManager(workspace=Path(tmp_dir))

            # Simulate borrowed agent
            manager.agent_pid = 54321
            manager.auth_sock = f"{tmp_dir}/existing-agent.sock"
            manager._agent_owned_by_us = False

            manager.cleanup()

            # Should NOT have tried to kill the agent
            mock_run_cmd.assert_not_called()

            # Should reset state
            assert manager.agent_pid is None
            assert manager.auth_sock is None
            assert manager._agent_owned_by_us is False

    @patch("github2gerrit.ssh_agent_setup.run_cmd")
    def test_cleanup_handles_kill_failure_gracefully(self, mock_run_cmd):
        """Test that cleanup handles kill failures gracefully."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager = SSHAgentManager(workspace=Path(tmp_dir))

            # Simulate owned agent
            manager.agent_pid = 12345
            manager.auth_sock = f"{tmp_dir}/ssh-agent.sock"
            manager._agent_owned_by_us = True

            # Mock kill failure
            mock_run_cmd.side_effect = Exception("Process not found")

            # Should not raise exception
            manager.cleanup()

            # Should still reset state
            assert manager.agent_pid is None
            assert manager.auth_sock is None
            assert manager._agent_owned_by_us is False

    def test_get_ssh_env_with_pid(self):
        """Test get_ssh_env includes PID when available."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager = SSHAgentManager(workspace=Path(tmp_dir))

            manager.auth_sock = f"{tmp_dir}/ssh-agent.sock"
            manager.agent_pid = 12345

            env = manager.get_ssh_env()

            assert env["SSH_AUTH_SOCK"] == f"{tmp_dir}/ssh-agent.sock"
            assert env["SSH_AGENT_PID"] == "12345"

    def test_get_ssh_env_without_pid(self):
        """Test get_ssh_env works without PID (borrowed agent case)."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager = SSHAgentManager(workspace=Path(tmp_dir))

            manager.auth_sock = f"{tmp_dir}/ssh-agent.sock"
            manager.agent_pid = None

            env = manager.get_ssh_env()

            assert env["SSH_AUTH_SOCK"] == f"{tmp_dir}/ssh-agent.sock"
            assert "SSH_AGENT_PID" not in env

    def test_get_ssh_env_missing_auth_sock_raises_error(self):
        """Test get_ssh_env raises error when auth_sock is missing."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager = SSHAgentManager(workspace=Path(tmp_dir))

            with pytest.raises(Exception, match="SSH agent not started"):
                manager.get_ssh_env()

    def test_internal_get_ssh_env_includes_current_environment(self):
        """Test _get_ssh_env preserves current environment variables."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager = SSHAgentManager(workspace=Path(tmp_dir))

            manager.auth_sock = f"{tmp_dir}/ssh-agent.sock"
            manager.agent_pid = 12345

            # Set a test environment variable

            os.environ["TEST_VAR"] = "test_value"

            try:
                env = manager._get_ssh_env()

                assert env["SSH_AUTH_SOCK"] == f"{tmp_dir}/ssh-agent.sock"
                assert env["SSH_AGENT_PID"] == "12345"
                assert env["TEST_VAR"] == "test_value"
                assert "PATH" in env  # Should preserve existing env vars

            finally:
                if "TEST_VAR" in os.environ:
                    del os.environ["TEST_VAR"]

    def test_ownership_tracking_prevents_double_kill(self):
        """Test that ownership tracking prevents accidentally killing borrowed agents."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager = SSHAgentManager(workspace=Path(tmp_dir))

            # Scenario: Agent starts as borrowed, then we might get confused
            manager.agent_pid = 12345
            manager.auth_sock = f"{tmp_dir}/ssh-agent.sock"
            manager._agent_owned_by_us = False  # Borrowed

            with patch("github2gerrit.ssh_agent_setup.run_cmd") as mock_run_cmd:
                manager.cleanup()

                # Even though we have a PID, we should NOT kill it
                mock_run_cmd.assert_not_called()
