# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for Gerrit DNS validation and G2G_NO_GERRIT behavior."""

from __future__ import annotations

import socket
from pathlib import Path
from unittest.mock import patch

import pytest

from github2gerrit.core import Orchestrator
from github2gerrit.core import OrchestratorError
from github2gerrit.core import RepoNames
from github2gerrit.gitreview import make_gitreview_info
from github2gerrit.models import Inputs
from github2gerrit.utils import env_bool


# ---------------------------------------------------------------------
# Shared test helpers and constants (used by multiple test classes)
# ---------------------------------------------------------------------


def _minimal_inputs(
    *,
    dry_run: bool = False,
    gerrit_server: str = "gerrit.example.org",
    gerrit_project: str = "example/project",
) -> Inputs:
    """Build a minimal ``Inputs`` for resolve-gerrit-info tests."""
    return Inputs(
        submit_single_commits=False,
        use_pr_as_commit=False,
        fetch_depth=10,
        gerrit_known_hosts="example.org ssh-rsa AAAAB3Nza...",
        gerrit_ssh_privkey_g2g="-----BEGIN KEY-----\nabc\n-----END KEY-----",
        gerrit_ssh_user_g2g="gerrit-bot",
        gerrit_ssh_user_g2g_email="gerrit-bot@example.org",
        github_token="ghp_test_token_123",  # noqa: S106
        organization="example",
        reviewers_email="",
        preserve_github_prs=False,
        dry_run=dry_run,
        normalise_commit=True,
        gerrit_server=gerrit_server,
        gerrit_server_port=29418,
        gerrit_project=gerrit_project,
        issue_id="",
        issue_id_lookup_json="",
        commit_rules_json="",
        allow_duplicates=False,
        ci_testing=False,
    )


_FAKE_DNS_RESULT = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.2.3.4", 0))]

_REPO = RepoNames(project_gerrit="example/project", project_github="project")


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
        with pytest.raises(OrchestratorError, match="Missing Gerrit host"):
            orch.validate_gerrit_server("")

    def test_whitespace_only_hostname_raises(self, tmp_path: Path) -> None:
        """Whitespace-only string raises OrchestratorError."""
        orch = self._make_orchestrator(tmp_path)
        with pytest.raises(OrchestratorError, match="Missing Gerrit host"):
            orch.validate_gerrit_server("   ")

    def test_none_hostname_raises(self, tmp_path: Path) -> None:
        """None value raises OrchestratorError."""
        orch = self._make_orchestrator(tmp_path)
        with pytest.raises(OrchestratorError, match="Missing Gerrit host"):
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

    def test_non_gaierror_oserror_raises(self, tmp_path: Path) -> None:
        """Non-gaierror OSError from getaddrinfo is also caught."""
        orch = self._make_orchestrator(tmp_path)
        oserror = OSError(22, "Invalid argument")
        with patch("socket.getaddrinfo", side_effect=oserror):
            with pytest.raises(OrchestratorError) as exc_info:
                orch.validate_gerrit_server("bad-host.example")
            assert exc_info.value.__cause__ is oserror

    def test_unicode_error_from_idna_hostname_raises(
        self, tmp_path: Path
    ) -> None:
        """UnicodeError from malformed IDNA hostname is caught."""
        orch = self._make_orchestrator(tmp_path)
        uerror = UnicodeError("label too long")
        with patch("socket.getaddrinfo", side_effect=uerror):
            with pytest.raises(OrchestratorError) as exc_info:
                orch.validate_gerrit_server("xn--bad.example")
            assert exc_info.value.__cause__ is uerror

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
    """Verify ``env_bool`` parses ``G2G_NO_GERRIT`` correctly.

    The actual env-mutation side-effects (forcing ``DRY_RUN`` and
    ``G2G_DRYRUN_DISABLE_NETWORK``) are performed inside
    ``cli._process()`` and are validated by the CLI-level integration
    tests.  These unit tests confirm that the flag-parsing primitive
    returns the expected boolean so the guard conditions in production
    code behave correctly.
    """

    def test_no_gerrit_true_is_truthy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``env_bool`` treats ``G2G_NO_GERRIT=true`` as truthy."""
        monkeypatch.setenv("G2G_NO_GERRIT", "true")
        assert env_bool("G2G_NO_GERRIT", False) is True

    def test_no_gerrit_one_is_truthy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``env_bool`` treats ``G2G_NO_GERRIT=1`` as truthy."""
        monkeypatch.setenv("G2G_NO_GERRIT", "1")
        assert env_bool("G2G_NO_GERRIT", False) is True

    def test_no_gerrit_false_is_falsy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``env_bool`` treats ``G2G_NO_GERRIT=false`` as falsy."""
        monkeypatch.setenv("G2G_NO_GERRIT", "false")
        assert env_bool("G2G_NO_GERRIT", False) is False

    def test_no_gerrit_unset_is_falsy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``env_bool`` defaults to ``False`` when unset."""
        monkeypatch.delenv("G2G_NO_GERRIT", raising=False)
        assert env_bool("G2G_NO_GERRIT", False) is False


# ---------------------------------------------------------------------
# G2G_NO_GERRIT - DNS validation is skipped
# ---------------------------------------------------------------------


class TestNoGerritSkipsDNS:
    """Verify DNS validation is bypassed via the real Orchestrator code path.

    These tests exercise ``Orchestrator._validate_resolved_gerrit_host()``
    (called by ``_resolve_gerrit_info()``) under the
    ``G2G_DRYRUN_DISABLE_NETWORK`` flag that the orchestrator checks.

    Note: ``G2G_NO_GERRIT`` forces ``G2G_DRYRUN_DISABLE_NETWORK=true``
    inside ``cli._process()`` (not at the orchestrator level), so these
    tests set the flag directly to verify the orchestrator's guard.
    """

    @staticmethod
    def _make_orchestrator(tmp_path: Path) -> Orchestrator:
        return Orchestrator(workspace=tmp_path)

    def test_dryrun_disable_network_skips_dns_gitreview(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DNS validation is skipped for .gitreview hosts when network disabled.

        ``G2G_NO_GERRIT`` forces ``G2G_DRYRUN_DISABLE_NETWORK=true`` in
        the CLI, so verifying that ``_validate_resolved_gerrit_host()``
        respects this flag covers the ``G2G_NO_GERRIT`` path indirectly.
        """
        monkeypatch.setenv("G2G_DRYRUN_DISABLE_NETWORK", "true")
        orch = self._make_orchestrator(tmp_path)
        gitreview = make_gitreview_info(
            host="bogus.invalid.example", port=29418, project="test/repo"
        )
        with patch("socket.getaddrinfo") as mock_dns:
            info = orch._resolve_gerrit_info(
                gitreview, _minimal_inputs(), _REPO
            )
            mock_dns.assert_not_called()
        assert info.host == "bogus.invalid.example"

    def test_dryrun_disable_network_skips_dns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DNS validation skipped with G2G_DRYRUN_DISABLE_NETWORK."""
        monkeypatch.setenv("G2G_DRYRUN_DISABLE_NETWORK", "true")
        orch = self._make_orchestrator(tmp_path)
        with patch("socket.getaddrinfo") as mock_dns:
            info = orch._resolve_gerrit_info(None, _minimal_inputs(), _REPO)
            mock_dns.assert_not_called()
        assert info.host == "gerrit.example.org"

    def test_dns_runs_when_network_enabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DNS validation runs when no skip flags are set."""
        monkeypatch.delenv("G2G_DRYRUN_DISABLE_NETWORK", raising=False)
        monkeypatch.delenv("G2G_NO_GERRIT", raising=False)
        orch = self._make_orchestrator(tmp_path)
        with patch(
            "socket.getaddrinfo", return_value=_FAKE_DNS_RESULT
        ) as mock_dns:
            orch._resolve_gerrit_info(None, _minimal_inputs(), _REPO)
            mock_dns.assert_called_once_with("gerrit.example.org", None)

    def test_dns_failure_raises_when_network_enabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Bogus host raises OrchestratorError when network is enabled."""
        monkeypatch.delenv("G2G_DRYRUN_DISABLE_NETWORK", raising=False)
        monkeypatch.delenv("G2G_NO_GERRIT", raising=False)
        orch = self._make_orchestrator(tmp_path)
        gitreview = make_gitreview_info(
            host="bogus.invalid.example", port=29418, project="test/repo"
        )
        with (
            patch(
                "socket.getaddrinfo",
                side_effect=socket.gaierror(8, "Name not resolved"),
            ),
            pytest.raises(OrchestratorError, match="DNS resolution failed"),
        ):
            orch._resolve_gerrit_info(gitreview, _minimal_inputs(), _REPO)


# ---------------------------------------------------------------------
# G2G_NO_GERRIT - cleanup suppression
# ---------------------------------------------------------------------


class TestNoGerritSuppressesCleanup:
    """Verify ``env_bool`` parses ``G2G_NO_GERRIT`` for cleanup guards.

    The real cleanup call sites in ``cli._process()`` use
    ``if FORCE_ABANDONED_CLEANUP and not no_gerrit:`` where
    ``no_gerrit = env_bool("G2G_NO_GERRIT", False)``.  These tests
    verify the flag-parsing primitive so the guard evaluates correctly.
    Full integration coverage of the cleanup suppression is provided
    by the CLI-level tests that exercise ``_process()`` end-to-end.
    """

    def test_g2g_no_gerrit_true_sets_no_gerrit_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``env_bool`` must treat ``G2G_NO_GERRIT=true`` as truthy."""
        monkeypatch.setenv("G2G_NO_GERRIT", "true")

        no_gerrit = env_bool("G2G_NO_GERRIT", False)
        assert no_gerrit is True

    def test_g2g_no_gerrit_unset_leaves_no_gerrit_flag_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``env_bool`` must treat unset ``G2G_NO_GERRIT`` as false."""
        monkeypatch.delenv("G2G_NO_GERRIT", raising=False)

        no_gerrit = env_bool("G2G_NO_GERRIT", False)
        assert no_gerrit is False


# ---------------------------------------------------------------------
# _resolve_gerrit_info — DNS validation of resolved hosts
# ---------------------------------------------------------------------


class TestResolveGerritInfoDNS:
    """DNS validation inside _resolve_gerrit_info()."""

    @staticmethod
    def _make_orchestrator(tmp_path: Path) -> Orchestrator:
        return Orchestrator(workspace=tmp_path)

    # -- .gitreview path ------------------------------------------------

    def test_gitreview_host_validated_via_dns(self, tmp_path: Path) -> None:
        """Host from .gitreview is validated via DNS."""
        orch = self._make_orchestrator(tmp_path)
        gitreview = make_gitreview_info(
            host="gerrit.example.net", port=29420, project="apps/service"
        )
        with patch(
            "socket.getaddrinfo", return_value=_FAKE_DNS_RESULT
        ) as mock_dns:
            orch._resolve_gerrit_info(gitreview, _minimal_inputs(), _REPO)
            mock_dns.assert_called_once_with("gerrit.example.net", None)

    def test_gitreview_bogus_host_raises(self, tmp_path: Path) -> None:
        """Bogus .gitreview host raises OrchestratorError."""
        orch = self._make_orchestrator(tmp_path)
        gitreview = make_gitreview_info(
            host="bogus.invalid.example", port=29418, project="test/repo"
        )
        with (
            patch(
                "socket.getaddrinfo",
                side_effect=socket.gaierror(8, "Name not resolved"),
            ),
            pytest.raises(OrchestratorError, match="DNS resolution failed"),
        ):
            orch._resolve_gerrit_info(gitreview, _minimal_inputs(), _REPO)

    # -- inputs (GERRIT_SERVER) path ------------------------------------

    def test_inputs_host_validated_via_dns(self, tmp_path: Path) -> None:
        """Host from GERRIT_SERVER input is validated via DNS."""
        orch = self._make_orchestrator(tmp_path)
        with patch(
            "socket.getaddrinfo", return_value=_FAKE_DNS_RESULT
        ) as mock_dns:
            orch._resolve_gerrit_info(None, _minimal_inputs(), _REPO)
            mock_dns.assert_called_once_with("gerrit.example.org", None)

    def test_inputs_bogus_host_raises(self, tmp_path: Path) -> None:
        """Bogus GERRIT_SERVER input raises OrchestratorError."""
        orch = self._make_orchestrator(tmp_path)
        inputs = _minimal_inputs(gerrit_server="bogus.invalid.example")
        with (
            patch(
                "socket.getaddrinfo",
                side_effect=socket.gaierror(8, "Name not resolved"),
            ),
            pytest.raises(OrchestratorError, match="DNS resolution failed"),
        ):
            orch._resolve_gerrit_info(None, inputs, _REPO)

    # -- G2G_DRYRUN_DISABLE_NETWORK skips DNS ---------------------------

    def test_network_disabled_skips_dns_for_gitreview(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DNS validation is skipped when G2G_DRYRUN_DISABLE_NETWORK is set."""
        monkeypatch.setenv("G2G_DRYRUN_DISABLE_NETWORK", "true")
        orch = self._make_orchestrator(tmp_path)
        gitreview = make_gitreview_info(
            host="bogus.invalid.example", port=29418, project="test/repo"
        )
        with patch("socket.getaddrinfo") as mock_dns:
            # Should NOT raise despite bogus hostname
            info = orch._resolve_gerrit_info(
                gitreview, _minimal_inputs(), _REPO
            )
            mock_dns.assert_not_called()
        assert info.host == "bogus.invalid.example"

    def test_network_disabled_skips_dns_for_inputs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DNS validation is skipped for input-sourced host when network disabled."""
        monkeypatch.setenv("G2G_DRYRUN_DISABLE_NETWORK", "true")
        orch = self._make_orchestrator(tmp_path)
        inputs = _minimal_inputs(gerrit_server="bogus.invalid.example")
        with patch("socket.getaddrinfo") as mock_dns:
            info = orch._resolve_gerrit_info(None, inputs, _REPO)
            mock_dns.assert_not_called()
        assert info.host == "bogus.invalid.example"

    # -- error message is concise and includes hostname -----------------

    def test_dns_error_message_includes_hostname(self, tmp_path: Path) -> None:
        """OrchestratorError message is a single line with the hostname."""
        orch = self._make_orchestrator(tmp_path)
        gitreview = make_gitreview_info(
            host="no-such-server.example", port=29418, project="x/y"
        )
        with (
            patch(
                "socket.getaddrinfo",
                side_effect=socket.gaierror(8, "Name not resolved"),
            ),
            pytest.raises(OrchestratorError) as exc_info,
        ):
            orch._resolve_gerrit_info(gitreview, _minimal_inputs(), _REPO)
        msg = str(exc_info.value)
        assert "no-such-server.example" in msg
        # Single-line, concise message
        assert "\n" not in msg
