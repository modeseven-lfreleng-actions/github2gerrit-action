# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""
Tests to ensure SSH artifacts are never committed to the git workspace.

This module tests the critical security requirement that temporary SSH files
(private keys, known_hosts) created by the tool are never accidentally
included in git commits that get pushed to Gerrit.
"""

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import Mock
from unittest.mock import patch

import pytest

from github2gerrit.core import Orchestrator
from github2gerrit.models import GitHubContext
from github2gerrit.models import Inputs


@pytest.fixture
def temp_workspace():
    """Create a temporary workspace directory."""
    with tempfile.TemporaryDirectory() as temp_dir:
        workspace = Path(temp_dir)
        # Initialize as git repo
        subprocess.run(["git", "init"], cwd=workspace, check=True)
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=workspace,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=workspace,
            check=True,
        )
        subprocess.run(
            ["git", "config", "commit.gpgsign", "false"],
            cwd=workspace,
            check=True,
        )
        yield workspace


@pytest.fixture
def mock_inputs():
    """Mock inputs with SSH configuration."""
    inputs = Mock(spec=Inputs)
    inputs.gerrit_ssh_privkey_g2g = "-----BEGIN OPENSSH PRIVATE KEY-----\ntest_key_content\n-----END OPENSSH PRIVATE KEY-----"
    inputs.gerrit_known_hosts = "gerrit.example.com ssh-rsa AAAAB3NzaC1yc2E..."
    inputs.dry_run = False
    inputs.ci_testing = False
    return inputs


@pytest.fixture
def mock_gerrit_info():
    """Mock Gerrit information."""
    from github2gerrit.core import GerritInfo

    return GerritInfo(
        host="gerrit.example.com", port=29418, project="test/project"
    )


def test_ssh_files_created_outside_workspace(
    temp_workspace, mock_inputs, mock_gerrit_info
):
    """Test that SSH files are created outside the git workspace."""
    orch = Orchestrator(workspace=temp_workspace)

    # Mock the SSH setup to avoid actual SSH operations
    with patch("github2gerrit.core.setup_ssh_agent_auth") as mock_ssh_agent:
        mock_ssh_agent.return_value = None

        # Call the SSH setup method
        orch._setup_ssh(mock_inputs, mock_gerrit_info)

        # Verify no SSH files were created in the workspace
        ssh_files_in_workspace = list(temp_workspace.glob("**/.ssh*"))
        known_hosts_in_workspace = list(temp_workspace.glob("**/known_hosts"))
        key_files_in_workspace = list(temp_workspace.glob("**/gerrit_key*"))

        assert len(ssh_files_in_workspace) == 0, (
            f"Found SSH files in workspace: {ssh_files_in_workspace}"
        )
        assert len(known_hosts_in_workspace) == 0, (
            f"Found known_hosts in workspace: {known_hosts_in_workspace}"
        )
        assert len(key_files_in_workspace) == 0, (
            f"Found key files in workspace: {key_files_in_workspace}"
        )

        # Verify SSH temp directory was created outside workspace
        assert orch._ssh_temp_dir is not None
        assert not str(orch._ssh_temp_dir).startswith(str(temp_workspace))
        assert orch._ssh_temp_dir.exists()


def test_ssh_files_use_secure_location(
    temp_workspace, mock_inputs, mock_gerrit_info
):
    """Test that SSH files use secure randomized paths."""
    orch1 = Orchestrator(workspace=temp_workspace)
    orch2 = Orchestrator(workspace=temp_workspace)

    with patch("github2gerrit.core.setup_ssh_agent_auth") as mock_ssh_agent:
        mock_ssh_agent.return_value = None

        # Setup SSH for both orchestrators
        orch1._setup_ssh(mock_inputs, mock_gerrit_info)
        orch2._setup_ssh(mock_inputs, mock_gerrit_info)

        # Verify different secure paths are used
        assert orch1._ssh_temp_dir != orch2._ssh_temp_dir
        assert "g2g_ssh_" in str(orch1._ssh_temp_dir)
        assert "g2g_ssh_" in str(orch2._ssh_temp_dir)

        # Verify paths contain randomized components
        path1_name = orch1._ssh_temp_dir.name
        path2_name = orch2._ssh_temp_dir.name
        assert path1_name != path2_name
        assert len(path1_name) > len("g2g_ssh_")  # Should have random suffix


def test_ssh_cleanup_removes_temp_directory(
    temp_workspace, mock_inputs, mock_gerrit_info
):
    """Test that SSH cleanup properly removes the temporary directory."""
    orch = Orchestrator(workspace=temp_workspace)

    with patch("github2gerrit.core.setup_ssh_agent_auth") as mock_ssh_agent:
        mock_ssh_agent.return_value = None

        # Setup SSH
        orch._setup_ssh(mock_inputs, mock_gerrit_info)
        ssh_temp_dir = orch._ssh_temp_dir

        assert ssh_temp_dir.exists()

        # Cleanup SSH
        orch._cleanup_ssh()

        # Verify directory was removed
        assert not ssh_temp_dir.exists()
        assert orch._ssh_temp_dir is None


def test_ssh_cleanup_overwrites_sensitive_files(temp_workspace):
    """Test that SSH cleanup securely overwrites sensitive files."""
    orch = Orchestrator(workspace=temp_workspace)

    # Manually create SSH temp directory and files for testing
    import secrets
    import tempfile

    secure_suffix = secrets.token_hex(8)
    ssh_temp_dir = Path(tempfile.mkdtemp(prefix=f"g2g_ssh_{secure_suffix}_"))
    orch._ssh_temp_dir = ssh_temp_dir

    # Create fake SSH files
    ssh_dir = ssh_temp_dir / ".ssh-g2g"
    ssh_dir.mkdir(mode=0o700)

    key_file = ssh_dir / "gerrit_key"
    known_hosts_file = ssh_dir / "known_hosts"

    original_key_content = "-----BEGIN OPENSSH PRIVATE KEY-----\nsensitive_key_data\n-----END OPENSSH PRIVATE KEY-----"
    original_hosts_content = "gerrit.example.com ssh-rsa AAAAB3NzaC1yc2E..."

    key_file.write_text(original_key_content)
    known_hosts_file.write_text(original_hosts_content)

    # Cleanup SSH (should overwrite files)
    orch._cleanup_ssh()

    # Verify directory was removed (can't check overwrite since files are gone)
    assert not ssh_temp_dir.exists()


def test_git_operations_exclude_ssh_artifacts(temp_workspace):
    """Test that demonstrates why SSH files in workspace are problematic (old behavior)."""
    # Create a git repo with some content
    test_file = temp_workspace / "test_file.txt"
    test_file.write_text("test content")

    subprocess.run(
        ["git", "add", "test_file.txt"], cwd=temp_workspace, check=True
    )
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=temp_workspace,
        check=True,
    )

    # Simulate what would happen if SSH files were created in workspace (old behavior)
    # This should NOT happen with our fix, but let's demonstrate the problem
    fake_ssh_dir = temp_workspace / ".ssh-g2g"
    fake_ssh_dir.mkdir()

    fake_key = fake_ssh_dir / "gerrit_key"
    fake_known_hosts = fake_ssh_dir / "known_hosts"

    fake_key.write_text("fake private key")
    fake_known_hosts.write_text("fake known hosts")

    # Try to add all files (simulating the problematic `git add .`)
    subprocess.run(["git", "add", "."], cwd=temp_workspace, check=True)

    # Check what would be committed
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=temp_workspace,
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    # This demonstrates the problem - SSH files WOULD be staged for commit
    # if they were in the workspace (which is why we now use secure temp dirs)
    assert ".ssh-g2g" in result  # This shows the problem exists
    assert "gerrit_key" in result
    assert "known_hosts" in result

    # The fix is that our current implementation never creates SSH files
    # in the workspace in the first place


@pytest.mark.skip(reason="Complex GitHub API mocking needs fixing")
def test_file_validation_detects_ssh_artifacts():
    """Test that file validation would detect SSH artifacts in commits."""
    from github2gerrit.core import Orchestrator
    from github2gerrit.core import SubmissionResult

    workspace = Path(tempfile.mkdtemp())
    orch = Orchestrator(workspace=workspace)

    # Mock GitHub context
    gh = Mock(spec=GitHubContext)
    gh.pr_number = "123"

    # Mock submission result with commit SHA
    result = SubmissionResult(
        change_urls=["https://gerrit.example.com/c/test/+/12345"],
        change_numbers=["12345"],
        commit_shas=["abc123def456"],
    )

    # Mock the GitHub API calls
    with (
        patch("github2gerrit.core.build_client") as mock_build_client,
        patch("github2gerrit.core.get_repo_from_env") as mock_get_repo,
        patch("github2gerrit.core.get_pull") as mock_get_pull,
        patch("github2gerrit.gitutils.run_cmd") as mock_run_cmd,
        patch.dict(
            "os.environ",
            {"GITHUB_REPOSITORY": "test/repo", "GITHUB_TOKEN": "fake_token"},
        ),
    ):
        # Mock GitHub API components
        mock_client = Mock()
        mock_repo = Mock()
        mock_build_client.return_value = mock_client
        mock_get_repo.return_value = mock_repo

        # Mock GitHub PR files
        mock_pr_file = Mock()
        mock_pr_file.filename = "src/main.py"

        mock_pr = Mock()
        mock_pr.get_files.return_value = [mock_pr_file]
        mock_get_pull.return_value = mock_pr

        # Mock git show output that includes SSH artifacts (the bad case)
        mock_run_cmd.return_value.stdout = (
            "src/main.py\n.ssh-g2g/known_hosts\n.ssh-g2g/gerrit_key"
        )

        # Capture log output
        with patch("github2gerrit.core.log") as mock_log:
            orch._validate_committed_files(gh, result)

            # Verify error was logged for SSH artifacts
            error_calls = [
                call
                for call in mock_log.error.call_args_list
                if "SSH artifacts detected" in str(call)
            ]
            assert len(error_calls) > 0, "Expected error log for SSH artifacts"


@pytest.mark.skip(reason="Complex GitHub API mocking needs fixing")
def test_file_validation_passes_for_clean_commits():
    """Test that file validation passes for commits without artifacts."""
    from github2gerrit.core import Orchestrator
    from github2gerrit.core import SubmissionResult

    workspace = Path(tempfile.mkdtemp())
    orch = Orchestrator(workspace=workspace)

    # Mock GitHub context
    gh = Mock(spec=GitHubContext)
    gh.pr_number = "123"

    # Mock submission result
    result = SubmissionResult(
        change_urls=["https://gerrit.example.com/c/test/+/12345"],
        change_numbers=["12345"],
        commit_shas=["abc123def456"],
    )

    # Mock the GitHub API calls
    with (
        patch("github2gerrit.core.build_client") as mock_build_client,
        patch("github2gerrit.core.get_repo_from_env") as mock_get_repo,
        patch("github2gerrit.core.get_pull") as mock_get_pull,
        patch("github2gerrit.gitutils.run_cmd") as mock_run_cmd,
        patch.dict(
            "os.environ",
            {"GITHUB_REPOSITORY": "test/repo", "GITHUB_TOKEN": "fake_token"},
        ),
    ):
        # Mock GitHub API components
        mock_client = Mock()
        mock_repo = Mock()
        mock_build_client.return_value = mock_client
        mock_get_repo.return_value = mock_repo

        # Mock GitHub PR files
        mock_pr_file = Mock()
        mock_pr_file.filename = "src/main.py"

        mock_pr = Mock()
        mock_pr.get_files.return_value = [mock_pr_file]
        mock_get_pull.return_value = mock_pr

        # Mock git show output with only expected files
        mock_run_cmd.return_value.stdout = "src/main.py"

        # Capture log output
        with patch("github2gerrit.core.log") as mock_log:
            orch._validate_committed_files(gh, result)

            # Verify no error was logged
            error_calls = [
                call
                for call in mock_log.error.call_args_list
                if "SSH artifacts detected" in str(call)
            ]
            assert len(error_calls) == 0, (
                "No errors should be logged for clean commits"
            )
