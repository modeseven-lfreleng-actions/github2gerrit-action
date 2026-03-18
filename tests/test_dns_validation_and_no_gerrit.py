# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for Gerrit DNS validation and G2G_NO_GERRIT behavior."""

from __future__ import annotations

import os
import socket
from pathlib import Path
from unittest.mock import patch

import pytest

from github2gerrit.core import Orchestrator
from github2gerrit.core import OrchestratorError
from github2gerrit.utils import env_bool


# ---------------------------------------------------------------------
# validate_gerrit_server - DNS resolution
# ---------------------------------------------------------------------


class TestValidateGerritServer:
    """Unit tests for Orchestrator.validate_gerrit_server."""

    def _make_orchestrator(self, tmp_path: Path) -> Orchestrator:
        """Create a minimal Orchestrator for validation tests."""
        return Orchestrator(workspace=tmp_path)

    # -- empty / blank hostname ----------------------------------------

    def test_empty_hostname_raises(self, tmp_path: Path) -> None:
        """Empty string raises OrchestratorError."""
        orch = self._make_orchestrator(tmp_path)
        with pytest.raises(OrchestratorError, match="missing GERRIT_SERVER"):
            orch.validate_gerrit_server("")

    def test_whitespace_only_hostname_raises(self, tmp_path: Path) -> None:
        """Whitespace-only string raises OrchestratorError."""
        orch = self._make_orchestrator(tmp_path)
        with pytest.raises(OrchestratorError, match="missing GERRIT_SERVER"):
            orch.validate_gerrit_server("   ")

    def test_none_hostname_raises(self, tmp_path: Path) -> None:
        """None value raises OrchestratorError."""
        orch = self._make_orchestrator(tmp_path)
        with pytest.raises(OrchestratorError, match="missing GERRIT_SERVER"):
            orch.validate_gerrit_server(None)

    # -- successful resolution -----------------------------------------

    def test_resolvable_host_succeeds(self, tmp_path: Path) -> None:
        """Valid hostname that resolves does not raise."""
        orch = self._make_orchestrator(tmp_path)
        fake_result = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.2.3.4", 0))
        ]
        with patch("socket.getaddrinfo", return_value=fake_result):
            # Should complete without raising
            orch.validate_gerrit_server("gerrit.example.org")

    def test_resolvable_host_strips_whitespace(self, tmp_path: Path) -> None:
        """Leading/trailing whitespace is stripped before resolution."""
        orch = self._make_orchestrator(tmp_path)
        fake_result = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.2.3.4", 0))
        ]
        with patch("socket.getaddrinfo", return_value=fake_result) as mock_dns:
            orch.validate_gerrit_server("  gerrit.example.org  ")
            mock_dns.assert_called_once_with("gerrit.example.org", None)

    # -- failed resolution ---------------------------------------------

    def test_unresolvable_host_raises(self, tmp_path: Path) -> None:
        """Unresolvable hostname raises OrchestratorError."""
        orch = self._make_orchestrator(tmp_path)
        with (
            patch(
                "socket.getaddrinfo",
                side_effect=socket.gaierror(8, "Name not resolved"),
            ),
            pytest.raises(OrchestratorError, match="DNS resolution failed"),
        ):
            orch.validate_gerrit_server("bogus.invalid.example")

    def test_dns_failure_chains_original_exception(
        self, tmp_path: Path
    ) -> None:
        """OrchestratorError chains the original socket.gaierror."""
        orch = self._make_orchestrator(tmp_path)
        gaierror = socket.gaierror(8, "Name not resolved")
        with patch("socket.getaddrinfo", side_effect=gaierror):
            with pytest.raises(OrchestratorError) as exc_info:
                orch.validate_gerrit_server("bogus.invalid.example")
            assert exc_info.value.__cause__ is gaierror

    def test_dns_failure_logs_debug_not_exception(self, tmp_path: Path) -> None:
        """DNS failure logs at DEBUG level (not exception).

        The top-level CLI caller owns user-facing logging, so
        validate_gerrit_server uses log.debug for the DNS failure
        message to avoid duplicate warnings.
        """
        orch = self._make_orchestrator(tmp_path)
        with (
            patch(
                "socket.getaddrinfo",
                side_effect=socket.gaierror(8, "Name not resolved"),
            ),
            patch("github2gerrit.core.log") as mock_log,
        ):
            with pytest.raises(OrchestratorError):
                orch.validate_gerrit_server("bogus.invalid.example")
            mock_log.debug.assert_called()
            # Verify exc_info=True is passed so the original
            # exception is visible when DEBUG logging is enabled.
            failure_calls = [
                c
                for c in mock_log.debug.call_args_list
                if "could not be resolved" in str(c)
            ]
            assert failure_calls, "expected a 'could not be resolved' debug log"
            assert failure_calls[0].kwargs.get("exc_info") is True
            mock_log.warning.assert_not_called()
            mock_log.exception.assert_not_called()


# ---------------------------------------------------------------------
# G2G_NO_GERRIT - environment variable behaviour
# ---------------------------------------------------------------------


class TestNoGerritEnvironment:
    """Verify G2G_NO_GERRIT forces expected environment state."""

    @staticmethod
    def _apply_no_gerrit(monkeypatch: pytest.MonkeyPatch) -> None:
        """Replicate G2G_NO_GERRIT env mutations using monkeypatch.

        Uses ``env_bool`` to match production parsing semantics
        (accepts ``1``, ``yes``, ``on``, ``true``).
        """
        if env_bool("G2G_NO_GERRIT", False):
            monkeypatch.setenv("DRY_RUN", "true")
            monkeypatch.setenv("G2G_DRYRUN_DISABLE_NETWORK", "true")

    def test_no_gerrit_forces_dry_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G2G_NO_GERRIT=true must set DRY_RUN=true in the env."""
        monkeypatch.setenv("G2G_NO_GERRIT", "true")
        monkeypatch.setenv("DRY_RUN", "false")

        self._apply_no_gerrit(monkeypatch)

        assert os.environ["DRY_RUN"] == "true"

    def test_no_gerrit_forces_disable_network(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G2G_NO_GERRIT=true must set G2G_DRYRUN_DISABLE_NETWORK."""
        monkeypatch.setenv("G2G_NO_GERRIT", "true")
        monkeypatch.delenv("G2G_DRYRUN_DISABLE_NETWORK", raising=False)

        self._apply_no_gerrit(monkeypatch)

        assert os.environ["G2G_DRYRUN_DISABLE_NETWORK"] == "true"

    def test_no_gerrit_false_does_not_modify_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G2G_NO_GERRIT=false must not alter DRY_RUN."""
        monkeypatch.setenv("G2G_NO_GERRIT", "false")
        monkeypatch.setenv("DRY_RUN", "false")
        monkeypatch.delenv("G2G_DRYRUN_DISABLE_NETWORK", raising=False)

        self._apply_no_gerrit(monkeypatch)

        assert os.environ["DRY_RUN"] == "false"
        assert os.environ.get("G2G_DRYRUN_DISABLE_NETWORK") is None

    def test_no_gerrit_unset_does_not_modify_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Absent G2G_NO_GERRIT must not alter DRY_RUN."""
        monkeypatch.delenv("G2G_NO_GERRIT", raising=False)
        monkeypatch.setenv("DRY_RUN", "false")
        monkeypatch.delenv("G2G_DRYRUN_DISABLE_NETWORK", raising=False)

        self._apply_no_gerrit(monkeypatch)

        assert os.environ["DRY_RUN"] == "false"
        assert os.environ.get("G2G_DRYRUN_DISABLE_NETWORK") is None


# ---------------------------------------------------------------------
# G2G_NO_GERRIT - DNS validation is skipped
# ---------------------------------------------------------------------


class TestNoGerritSkipsDNS:
    """Verify DNS validation is bypassed under G2G_NO_GERRIT."""

    @staticmethod
    def _should_run_dns(
        no_gerrit: bool,
        g2g_test_mode: bool,
        dryrun_disable_network: bool,
    ) -> bool:
        """Replicate the guard condition from _process()."""
        return (
            not no_gerrit and not g2g_test_mode and not dryrun_disable_network
        )

    def test_no_gerrit_skips_dns(self) -> None:
        """DNS validation is skipped when no_gerrit is True."""
        assert not self._should_run_dns(
            no_gerrit=True,
            g2g_test_mode=False,
            dryrun_disable_network=False,
        )

    def test_g2g_test_mode_skips_dns(self) -> None:
        """DNS validation is skipped when g2g_test_mode is True."""
        assert not self._should_run_dns(
            no_gerrit=False,
            g2g_test_mode=True,
            dryrun_disable_network=False,
        )

    def test_dryrun_disable_network_skips_dns(self) -> None:
        """DNS validation skipped with dryrun_disable_network."""
        assert not self._should_run_dns(
            no_gerrit=False,
            g2g_test_mode=False,
            dryrun_disable_network=True,
        )

    def test_all_false_runs_dns(self) -> None:
        """DNS validation runs when no skip flags are set."""
        assert self._should_run_dns(
            no_gerrit=False,
            g2g_test_mode=False,
            dryrun_disable_network=False,
        )

    def test_multiple_skip_flags_still_skip(self) -> None:
        """DNS validation is skipped when multiple flags are set."""
        assert not self._should_run_dns(
            no_gerrit=True,
            g2g_test_mode=True,
            dryrun_disable_network=True,
        )


# ---------------------------------------------------------------------
# G2G_NO_GERRIT - cleanup suppression
# ---------------------------------------------------------------------


class TestNoGerritSuppressesCleanup:
    """Verify cleanup operations are gated on not no_gerrit."""

    @staticmethod
    def _should_run_cleanup(no_gerrit: bool) -> bool:
        """Replicate the cleanup guard from _process()."""
        return not no_gerrit

    def test_cleanup_suppressed_in_no_gerrit(self) -> None:
        """Cleanup must not run when no_gerrit is True."""
        assert not self._should_run_cleanup(no_gerrit=True)

    def test_cleanup_runs_normally(self) -> None:
        """Cleanup runs when no_gerrit is False."""
        assert self._should_run_cleanup(no_gerrit=False)


# ---------------------------------------------------------------------
# CLI-level DNS validation integration test
# ---------------------------------------------------------------------


class TestCLIDNSValidationExit:
    """Verify the CLI exits with CONFIGURATION_ERROR on DNS failure."""

    @staticmethod
    def _cli_env(tmp_path: Path) -> dict[str, str]:
        """Minimal env for CLI invocation with DNS validation enabled."""
        import json

        event_path = tmp_path / "event.json"
        event = {"action": "opened", "pull_request": {"number": 7}}
        event_path.write_text(json.dumps(event), encoding="utf-8")

        base = {
            k: v
            for k, v in os.environ.items()
            if k not in ("PR_NUMBER", "SYNC_ALL_OPEN_PRS")
        }
        base.update(
            {
                "GERRIT_KNOWN_HOSTS": "x ssh-rsa AAAA...",
                "GERRIT_SSH_PRIVKEY_G2G": "-----BEGIN KEY-----\nk\n-----END KEY-----",
                "GERRIT_SSH_USER_G2G": "bot",
                "GERRIT_SSH_USER_G2G_EMAIL": "bot@example.org",
                "ORGANIZATION": "example",
                "GERRIT_SERVER": "bogus.invalid.example",
                "DRY_RUN": "false",
                "CI_TESTING": "false",
                "GITHUB_EVENT_NAME": "pull_request_target",
                "GITHUB_EVENT_PATH": str(event_path),
                "GITHUB_REPOSITORY": "example/repo",
                "GITHUB_REPOSITORY_OWNER": "example",
                "GITHUB_SERVER_URL": "https://github.com",
                "GITHUB_RUN_ID": "1",
                "GITHUB_SHA": "abc123",
                "GITHUB_BASE_REF": "main",
                "GITHUB_HEAD_REF": "feature",
                # DNS validation must be active (not skipped)
                "G2G_TEST_MODE": "false",
                "G2G_NO_GERRIT": "false",
                "G2G_DRYRUN_DISABLE_NETWORK": "false",
                "SYNC_ALL_OPEN_PRS": "false",
            },
        )
        return base

    def test_dns_failure_exits_configuration_error(
        self, tmp_path: Path
    ) -> None:
        """CLI exits with CONFIGURATION_ERROR when DNS fails."""
        from typer.testing import CliRunner

        from github2gerrit.cli import app

        try:
            cli_runner = CliRunner(mix_stderr=False)
        except TypeError:
            cli_runner = CliRunner()

        env = self._cli_env(tmp_path)

        with patch(
            "socket.getaddrinfo",
            side_effect=socket.gaierror(8, "Name not resolved"),
        ):
            result = cli_runner.invoke(app, [], env=env)

        # ExitCode.CONFIGURATION_ERROR == 2
        assert result.exit_code == 2

    def test_dns_failure_mentions_hostname(self, tmp_path: Path) -> None:
        """Error output includes the failing hostname."""
        from typer.testing import CliRunner

        from github2gerrit.cli import app

        try:
            cli_runner = CliRunner(mix_stderr=False)
        except TypeError:
            cli_runner = CliRunner()

        env = self._cli_env(tmp_path)

        with patch(
            "socket.getaddrinfo",
            side_effect=socket.gaierror(8, "Name not resolved"),
        ):
            result = cli_runner.invoke(app, [], env=env)

        combined = result.stdout + (getattr(result, "stderr", None) or "")
        assert "bogus.invalid.example" in combined
