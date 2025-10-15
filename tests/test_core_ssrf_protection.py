# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""Tests for SSRF protection in URL validation."""

import socket
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from github2gerrit.core import Orchestrator
from github2gerrit.core import OrchestratorError


class TestSSRFProtection:
    """Test SSRF protection mechanisms in URL validation."""

    def test_validate_hostname_allows_github_domains(self):
        """Test that known safe GitHub domains are allowed."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            orchestrator = Orchestrator(workspace=Path(tmp_dir))

        # These should all pass without DNS resolution
        safe_domains = [
            "github.com",
            "api.github.com",
            "raw.githubusercontent.com",
            "objects.githubusercontent.com",
            "codeload.github.com",
        ]

        for domain in safe_domains:
            # Should not raise an exception
            orchestrator._validate_hostname_against_ssrf(domain)

    def test_validate_hostname_allows_github_subdomains(self):
        """Test that subdomains of safe GitHub domains are allowed."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            orchestrator = Orchestrator(workspace=Path(tmp_dir))

        # These should all pass without DNS resolution
        safe_subdomains = [
            "www.github.com",
            "enterprise.github.com",
            "my-org.github.com",
        ]

        for subdomain in safe_subdomains:
            # Should not raise an exception
            orchestrator._validate_hostname_against_ssrf(subdomain)

    @patch("socket.getaddrinfo")
    def test_validate_hostname_blocks_private_ipv4(self, mock_getaddrinfo):
        """Test that private IPv4 addresses are blocked."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            orchestrator = Orchestrator(workspace=Path(tmp_dir))

        # Mock DNS resolution to return private IP
        mock_getaddrinfo.return_value = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.1", 80))
        ]

        with pytest.raises(OrchestratorError) as exc_info:
            orchestrator._validate_hostname_against_ssrf("evil.example.com")

        assert "private/local addresses not allowed" in str(exc_info.value)
        assert "192.168.1.1" in str(exc_info.value)

    @patch("socket.getaddrinfo")
    def test_validate_hostname_blocks_loopback_ipv4(self, mock_getaddrinfo):
        """Test that loopback IPv4 addresses are blocked."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            orchestrator = Orchestrator(workspace=Path(tmp_dir))

        # Mock DNS resolution to return loopback IP
        mock_getaddrinfo.return_value = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))
        ]

        with pytest.raises(OrchestratorError) as exc_info:
            orchestrator._validate_hostname_against_ssrf("localhost.evil.com")

        assert "private/local addresses not allowed" in str(exc_info.value)
        assert "127.0.0.1" in str(exc_info.value)

    @patch("socket.getaddrinfo")
    def test_validate_hostname_blocks_private_ipv6(self, mock_getaddrinfo):
        """Test that private IPv6 addresses are blocked."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            orchestrator = Orchestrator(workspace=Path(tmp_dir))

        # Mock DNS resolution to return private IPv6
        mock_getaddrinfo.return_value = [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fc00::1", 80, 0, 0))
        ]

        with pytest.raises(OrchestratorError) as exc_info:
            orchestrator._validate_hostname_against_ssrf(
                "evil-ipv6.example.com"
            )

        assert "private/local addresses not allowed" in str(exc_info.value)
        assert "fc00::1" in str(exc_info.value)

    @patch("socket.getaddrinfo")
    def test_validate_hostname_blocks_link_local_ipv6(self, mock_getaddrinfo):
        """Test that link-local IPv6 addresses are blocked."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            orchestrator = Orchestrator(workspace=Path(tmp_dir))

        # Mock DNS resolution to return link-local IPv6
        mock_getaddrinfo.return_value = [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fe80::1", 80, 0, 0))
        ]

        with pytest.raises(OrchestratorError) as exc_info:
            orchestrator._validate_hostname_against_ssrf(
                "link-local.example.com"
            )

        assert "private/local addresses not allowed" in str(exc_info.value)
        assert "fe80::1" in str(exc_info.value)

    @patch("socket.getaddrinfo")
    def test_validate_hostname_blocks_carrier_grade_nat(self, mock_getaddrinfo):
        """Test that carrier-grade NAT addresses are blocked."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            orchestrator = Orchestrator(workspace=Path(tmp_dir))

        # Mock DNS resolution to return carrier-grade NAT IP
        mock_getaddrinfo.return_value = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("100.64.0.1", 80))
        ]

        with pytest.raises(OrchestratorError) as exc_info:
            orchestrator._validate_hostname_against_ssrf("cgnat.example.com")

        assert "private/local addresses not allowed" in str(exc_info.value)
        assert "100.64.0.1" in str(exc_info.value)

    @patch("socket.getaddrinfo")
    def test_validate_hostname_blocks_mixed_ips_when_any_blocked(
        self, mock_getaddrinfo
    ):
        """Test that hostname is blocked if ANY resolved IP is blocked."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            orchestrator = Orchestrator(workspace=Path(tmp_dir))

        # Mock DNS resolution to return both public and private IPs
        mock_getaddrinfo.return_value = [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                ("8.8.8.8", 80),
            ),  # Public (safe)
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                ("192.168.1.1", 80),
            ),  # Private (blocked)
        ]

        with pytest.raises(OrchestratorError) as exc_info:
            orchestrator._validate_hostname_against_ssrf("mixed.example.com")

        error_msg = str(exc_info.value)
        assert "private/local addresses not allowed" in error_msg
        assert "8.8.8.8" in error_msg  # Should show all resolved IPs
        assert "192.168.1.1" in error_msg  # Should show blocked IP

    @patch("socket.getaddrinfo")
    def test_validate_hostname_allows_public_ips(self, mock_getaddrinfo):
        """Test that public IP addresses are allowed."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            orchestrator = Orchestrator(workspace=Path(tmp_dir))

        # Mock DNS resolution to return public IPs
        mock_getaddrinfo.return_value = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 80)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 80)),
        ]

        # Should not raise an exception
        orchestrator._validate_hostname_against_ssrf("public.example.com")

    @patch("socket.getaddrinfo")
    def test_validate_hostname_blocks_documentation_ranges(
        self, mock_getaddrinfo
    ):
        """Test that documentation IP ranges are blocked."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            orchestrator = Orchestrator(workspace=Path(tmp_dir))

        documentation_ips = [
            ("192.0.2.1", 80),  # TEST-NET-1
            ("198.51.100.1", 80),  # TEST-NET-2
            ("203.0.113.1", 80),  # TEST-NET-3
            ("2001:db8::1", 80, 0, 0),  # IPv6 documentation
        ]

        for ip_info in documentation_ips:
            mock_getaddrinfo.return_value = [
                (
                    socket.AF_INET if len(ip_info) == 2 else socket.AF_INET6,
                    socket.SOCK_STREAM,
                    6,
                    "",
                    ip_info,
                )
            ]

            with pytest.raises(OrchestratorError) as exc_info:
                orchestrator._validate_hostname_against_ssrf("doc.example.com")

            assert "private/local addresses not allowed" in str(exc_info.value)

    @patch("socket.getaddrinfo")
    def test_validate_hostname_handles_dns_resolution_failure(
        self, mock_getaddrinfo
    ):
        """Test that DNS resolution failures are handled gracefully."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            orchestrator = Orchestrator(workspace=Path(tmp_dir))

        # Mock DNS resolution failure
        mock_getaddrinfo.side_effect = socket.gaierror("Name resolution failed")

        with pytest.raises(OrchestratorError) as exc_info:
            orchestrator._validate_hostname_against_ssrf(
                "nonexistent.example.com"
            )

        assert "Cannot resolve hostname" in str(exc_info.value)

    @patch("socket.getaddrinfo")
    def test_validate_hostname_blocks_malformed_ips(self, mock_getaddrinfo):
        """Test that malformed IP addresses are blocked for safety."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            orchestrator = Orchestrator(workspace=Path(tmp_dir))

        # Mock getaddrinfo to return something that can't be parsed as IP
        mock_getaddrinfo.return_value = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("not.an.ip", 80))
        ]

        with pytest.raises(OrchestratorError) as exc_info:
            orchestrator._validate_hostname_against_ssrf(
                "malformed.example.com"
            )

        assert "private/local addresses not allowed" in str(exc_info.value)

    def test_validate_and_get_api_base_url_github_com(self):
        """Test that github.com URLs are handled correctly."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            orchestrator = Orchestrator(workspace=Path(tmp_dir))

        result = orchestrator._validate_and_get_api_base_url(
            "https://github.com"
        )
        assert result == "https://api.github.com"

    @patch("socket.getaddrinfo")
    def test_validate_and_get_api_base_url_github_enterprise(
        self, mock_getaddrinfo
    ):
        """Test that GitHub Enterprise URLs are validated and handled correctly."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            orchestrator = Orchestrator(workspace=Path(tmp_dir))

        # Mock DNS resolution to return public IP
        mock_getaddrinfo.return_value = [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                ("203.0.113.100", 80),
            )  # Public IP
        ]

        # This should fail because 203.0.113.100 is in documentation range
        with pytest.raises(OrchestratorError):
            orchestrator._validate_and_get_api_base_url(
                "https://github.internal.company.com"
            )

    @patch("socket.getaddrinfo")
    def test_validate_and_get_api_base_url_github_enterprise_success(
        self, mock_getaddrinfo
    ):
        """Test successful GitHub Enterprise URL validation."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            orchestrator = Orchestrator(workspace=Path(tmp_dir))

        # Mock DNS resolution to return public IP
        mock_getaddrinfo.return_value = [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                ("8.8.8.8", 80),
            )  # Safe public IP
        ]

        result = orchestrator._validate_and_get_api_base_url(
            "https://github.company.com"
        )
        assert result == "https://github.company.com/api/v3"

    def test_validate_and_get_api_base_url_invalid_scheme(self):
        """Test that invalid URL schemes are rejected."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            orchestrator = Orchestrator(workspace=Path(tmp_dir))

        with pytest.raises(OrchestratorError) as exc_info:
            orchestrator._validate_and_get_api_base_url("ftp://github.com")

        assert "Invalid URL scheme" in str(exc_info.value)

    def test_validate_and_get_api_base_url_missing_hostname(self):
        """Test that URLs without hostnames are rejected."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            orchestrator = Orchestrator(workspace=Path(tmp_dir))

        with pytest.raises(OrchestratorError) as exc_info:
            orchestrator._validate_and_get_api_base_url("https://")

        assert "missing hostname" in str(exc_info.value)
