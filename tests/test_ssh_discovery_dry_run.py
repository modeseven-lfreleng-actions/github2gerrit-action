# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation
#
# Tests for SSH discovery dry-run functionality

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from github2gerrit.core import Orchestrator
from github2gerrit.ssh_discovery import auto_discover_gerrit_host_keys
from github2gerrit.ssh_discovery import save_host_keys_to_config


class TestSSHDiscoveryDryRun:
    """Test SSH discovery behavior in dry-run mode."""

    def setup_method(self) -> None:
        """Set up test fixtures."""
        self.temp_dir = Path(tempfile.mkdtemp())
        self.config_path = self.temp_dir / "config.txt"
        self.workspace = self.temp_dir / "workspace"
        self.workspace.mkdir()
        self.orch = Orchestrator(workspace=self.workspace)

    def teardown_method(self) -> None:
        """Clean up test fixtures."""
        import shutil

        shutil.rmtree(self.temp_dir)

    def test_save_host_keys_to_config_skipped_in_dry_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that save_host_keys_to_config is skipped in dry-run mode."""
        # Create initial config
        original_content = '[testorg]\nGERRIT_SERVER = "gerrit.example.org"\n'
        self.config_path.write_text(original_content)

        # Set up environment for dry-run
        monkeypatch.setenv("DRY_RUN", "true")
        monkeypatch.setenv("G2G_CONFIG_PATH", str(self.config_path))
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)

        # Test host keys
        test_keys = (
            "gerrit.example.org ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQ...\n"
            "gerrit.example.org ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTY..."
        )

        # Attempt to save (should be skipped)
        save_host_keys_to_config(test_keys, "testorg", str(self.config_path))

        # Content should remain unchanged
        updated_content = self.config_path.read_text()
        assert updated_content == original_content
        assert "GERRIT_KNOWN_HOSTS" not in updated_content

    def test_save_host_keys_to_config_works_when_dry_run_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that save_host_keys_to_config works when dry-run is disabled."""
        # Create initial config
        original_content = '[testorg]\nGERRIT_SERVER = "gerrit.example.org"\n'
        self.config_path.write_text(original_content)

        # Set up environment with dry-run disabled
        monkeypatch.setenv("DRY_RUN", "false")
        monkeypatch.setenv("G2G_CONFIG_PATH", str(self.config_path))
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)

        # Test host keys
        test_keys = (
            "gerrit.example.org ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQ...\n"
            "gerrit.example.org ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTY..."
        )

        # Attempt to save (should work)
        save_host_keys_to_config(test_keys, "testorg", str(self.config_path))

        # Content should be updated
        updated_content = self.config_path.read_text()
        assert "GERRIT_KNOWN_HOSTS" in updated_content
        assert test_keys.replace("\n", "\\n") in updated_content

    def test_save_host_keys_to_config_multiple_dry_run_values(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that save_host_keys_to_config respects various dry-run flag values."""
        original_content = '[testorg]\nGERRIT_SERVER = "gerrit.example.org"\n'
        test_keys = (
            "gerrit.example.org ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQ..."
        )

        # Set up environment
        monkeypatch.setenv("G2G_CONFIG_PATH", str(self.config_path))
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)

        # Test various dry-run values
        dry_run_values = ["true", "TRUE", "1", "yes", "YES"]

        for dry_run_value in dry_run_values:
            # Reset config file
            self.config_path.write_text(original_content)

            # Set dry-run flag
            monkeypatch.setenv("DRY_RUN", dry_run_value)

            # Attempt to save (should be skipped)
            save_host_keys_to_config(
                test_keys, "testorg", str(self.config_path)
            )

            # Content should remain unchanged
            updated_content = self.config_path.read_text()
            assert updated_content == original_content, (
                f"Failed for DRY_RUN={dry_run_value}"
            )
            assert "GERRIT_KNOWN_HOSTS" not in updated_content

    @patch("github2gerrit.ssh_discovery.fetch_ssh_host_keys")
    def test_auto_discover_never_saves_immediately(
        self, mock_fetch, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that auto-discovery never saves to config immediately (regardless of dry-run)."""
        # Mock successful key discovery
        test_keys = (
            "gerrit.example.org ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQ..."
        )
        mock_fetch.return_value = test_keys

        # Create initial config
        original_content = '[testorg]\nGERRIT_SERVER = "gerrit.example.org"\n'
        self.config_path.write_text(original_content)

        # Set up environment
        monkeypatch.setenv("DRY_RUN", "true")
        monkeypatch.setenv("G2G_CONFIG_PATH", str(self.config_path))
        monkeypatch.setenv("ORGANIZATION", "testorg")
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)

        # Call auto-discovery (keys are no longer saved immediately)
        result = auto_discover_gerrit_host_keys(
            gerrit_hostname="gerrit.example.org",
            gerrit_port=29418,
            organization="testorg",
        )

        # Should return the discovered keys
        assert result == test_keys

        # Config file should remain unchanged (keys are never saved immediately)
        updated_content = self.config_path.read_text()
        assert updated_content == original_content
        assert "GERRIT_KNOWN_HOSTS" not in updated_content

    def test_orchestrator_save_discovered_ssh_keys_dry_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that Orchestrator._save_discovered_ssh_keys_to_config respects dry-run."""
        # Create initial config
        original_content = '[testorg]\nGERRIT_SERVER = "gerrit.example.org"\n'
        self.config_path.write_text(original_content)

        # Set up environment for dry-run
        monkeypatch.setenv("DRY_RUN", "true")
        monkeypatch.setenv("G2G_CONFIG_PATH", str(self.config_path))
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)

        # Simulate discovered SSH keys
        self.orch._discovered_ssh_keys = (
            "gerrit.example.org ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQ..."
        )
        self.orch._ssh_discovery_organization = "testorg"

        # Attempt to save (should be skipped due to dry-run)
        self.orch._save_discovered_ssh_keys_to_config()

        # Content should remain unchanged
        updated_content = self.config_path.read_text()
        assert updated_content == original_content
        assert "GERRIT_KNOWN_HOSTS" not in updated_content

    def test_orchestrator_save_discovered_ssh_keys_works_normally(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that Orchestrator._save_discovered_ssh_keys_to_config works when not in dry-run."""
        # Create initial config
        original_content = '[testorg]\nGERRIT_SERVER = "gerrit.example.org"\n'
        self.config_path.write_text(original_content)

        # Set up environment with dry-run disabled
        monkeypatch.setenv("DRY_RUN", "false")
        monkeypatch.setenv("G2G_CONFIG_PATH", str(self.config_path))
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)

        # Simulate discovered SSH keys
        test_keys = (
            "gerrit.example.org ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQ..."
        )
        self.orch._discovered_ssh_keys = test_keys
        self.orch._ssh_discovery_organization = "testorg"

        # Attempt to save (should work)
        self.orch._save_discovered_ssh_keys_to_config()

        # Content should be updated
        updated_content = self.config_path.read_text()
        assert "GERRIT_KNOWN_HOSTS" in updated_content
        assert test_keys.replace("\n", "\\n") in updated_content

    def test_orchestrator_no_ssh_keys_to_save(self) -> None:
        """Test that _save_discovered_ssh_keys_to_config handles no keys gracefully."""
        # No discovered keys
        assert self.orch._discovered_ssh_keys is None
        assert self.orch._ssh_discovery_organization is None

        # Should handle gracefully without error
        self.orch._save_discovered_ssh_keys_to_config()

        # Test with keys but no organization
        self.orch._discovered_ssh_keys = "test-keys"
        self.orch._ssh_discovery_organization = None
        self.orch._save_discovered_ssh_keys_to_config()

        # Test with organization but no keys
        self.orch._discovered_ssh_keys = None
        self.orch._ssh_discovery_organization = "testorg"
        self.orch._save_discovered_ssh_keys_to_config()

    def test_save_host_keys_respects_github_actions_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that save_host_keys_to_config still respects GitHub Actions mode."""
        # Create initial config
        original_content = '[testorg]\nGERRIT_SERVER = "gerrit.example.org"\n'
        self.config_path.write_text(original_content)

        # Set up GitHub Actions environment (should skip saving)
        monkeypatch.setenv("DRY_RUN", "false")  # Dry-run disabled
        monkeypatch.setenv("GITHUB_ACTIONS", "true")  # But in GitHub Actions
        monkeypatch.setenv("G2G_CONFIG_PATH", str(self.config_path))

        # Test host keys
        test_keys = (
            "gerrit.example.org ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQ..."
        )

        # Attempt to save (should be skipped due to GitHub Actions mode)
        save_host_keys_to_config(test_keys, "testorg", str(self.config_path))

        # Content should remain unchanged
        updated_content = self.config_path.read_text()
        assert updated_content == original_content
        assert "GERRIT_KNOWN_HOSTS" not in updated_content
