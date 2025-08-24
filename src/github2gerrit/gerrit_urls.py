# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation
"""
Centralized Gerrit URL construction utilities.

This module provides a unified way to construct Gerrit URLs, ensuring
consistent handling of GERRIT_HTTP_BASE_PATH and eliminating the need
for manual URL construction throughout the codebase.
"""

from __future__ import annotations

import logging
import os
from urllib.parse import urljoin


log = logging.getLogger(__name__)


class GerritUrlBuilder:
    """
    Centralized builder for Gerrit URLs with consistent base path handling.

    This class encapsulates all Gerrit URL construction logic, ensuring that
    GERRIT_HTTP_BASE_PATH is properly handled in all contexts. It provides
    methods for building different types of URLs (API, web, hooks) and handles
    the common fallback patterns used throughout the application.
    """

    def __init__(self, host: str, base_path: str | None = None):
        """
        Initialize the URL builder for a specific Gerrit host.

        Args:
            host: Gerrit hostname (without protocol)
            base_path: Optional base path override. If None, reads from
                      GERRIT_HTTP_BASE_PATH environment variable.
        """
        self.host = host.strip()

        # Normalize base path - remove leading/trailing slashes and whitespace
        if base_path is not None:
            self._base_path = base_path.strip().strip("/")
        else:
            self._base_path = os.getenv("GERRIT_HTTP_BASE_PATH", "").strip().strip("/")

        log.debug(
            "GerritUrlBuilder initialized for host=%s, base_path='%s'",
            self.host,
            self._base_path,
        )

    @property
    def base_path(self) -> str:
        """Get the normalized base path."""
        return self._base_path

    @property
    def has_base_path(self) -> bool:
        """Check if a base path is configured."""
        return bool(self._base_path)

    def _build_base_url(self, base_path_override: str | None = None) -> str:
        """
        Build the base URL with optional base path override.

        Args:
            base_path_override: Optional base path to use instead of the instance default

        Returns:
            Base URL with trailing slash
        """
        path = base_path_override if base_path_override is not None else self._base_path
        if path:
            return f"https://{self.host}/{path}/"
        else:
            return f"https://{self.host}/"

    def api_url(self, endpoint: str = "", base_path_override: str | None = None) -> str:
        """
        Build a Gerrit REST API URL.

        Args:
            endpoint: API endpoint path (e.g., "/changes/", "/accounts/self")
            base_path_override: Optional base path override for fallback scenarios

        Returns:
            Complete API URL
        """
        base_url = self._build_base_url(base_path_override)
        # Ensure endpoint starts with / for proper URL joining
        if endpoint and not endpoint.startswith("/"):
            endpoint = "/" + endpoint
        return urljoin(base_url, endpoint.lstrip("/"))

    def web_url(self, path: str = "", base_path_override: str | None = None) -> str:
        """
        Build a Gerrit web UI URL.

        Args:
            path: Web path (e.g., "c/project/+/123", "dashboard")
            base_path_override: Optional base path override for fallback scenarios

        Returns:
            Complete web URL
        """
        base_url = self._build_base_url(base_path_override)
        if path:
            # Remove leading slash if present to avoid double slashes
            path = path.lstrip("/")
            return urljoin(base_url, path)
        return base_url.rstrip("/")

    def change_url(
        self,
        project: str,
        change_number: int,
        base_path_override: str | None = None,
    ) -> str:
        """
        Build a URL for a specific Gerrit change.

        Args:
            project: Gerrit project name
            change_number: Gerrit change number
            base_path_override: Optional base path override for fallback scenarios

        Returns:
            Complete change URL
        """
        # Don't URL-encode project names - Gerrit expects them as-is (backward compatibility)
        path = f"c/{project}/+/{change_number}"
        return self.web_url(path, base_path_override)

    def hook_url(self, hook_name: str, base_path_override: str | None = None) -> str:
        """
        Build a URL for downloading Gerrit hooks.

        Args:
            hook_name: Name of the hook (e.g., "commit-msg")
            base_path_override: Optional base path override for fallback scenarios

        Returns:
            Complete hook download URL
        """
        path = f"tools/hooks/{hook_name}"
        return self.web_url(path, base_path_override)

    def get_api_url_candidates(self, endpoint: str = "") -> list[str]:
        """
        Get a list of candidate API URLs for fallback scenarios.

        This method returns URLs in order of preference:
        1. URL with configured base path (if any)
        2. URL with /r/ base path (common fallback)
        3. URL with no base path (root)

        Args:
            endpoint: API endpoint path

        Returns:
            List of candidate URLs to try
        """
        candidates = []

        # Primary URL with configured base path
        if self.has_base_path:
            candidates.append(self.api_url(endpoint))

        # Common fallback: /r/ base path
        if self._base_path != "r":
            candidates.append(self.api_url(endpoint, base_path_override="r"))

        # Final fallback: no base path
        if self.has_base_path:
            candidates.append(self.api_url(endpoint, base_path_override=""))

        # If no base path was configured, add the primary URL
        if not self.has_base_path:
            candidates.append(self.api_url(endpoint))

        return candidates

    def get_hook_url_candidates(self, hook_name: str) -> list[str]:
        """
        Get a list of candidate hook URLs for fallback scenarios.

        This method returns URLs in order of preference for downloading hooks:
        1. URL with configured base path (if any)
        2. URL with /r/ base path (common for hooks)
        3. URL with no base path (root)

        Args:
            hook_name: Name of the hook to download

        Returns:
            List of candidate URLs to try
        """
        candidates = []

        # Primary URL with configured base path
        if self.has_base_path:
            candidates.append(self.hook_url(hook_name))

        # Common fallback: /r/ base path (very common for hooks)
        if self._base_path != "r":
            candidates.append(self.hook_url(hook_name, base_path_override="r"))

        # Final fallback: no base path
        if self.has_base_path:
            candidates.append(self.hook_url(hook_name, base_path_override=""))

        # If no base path was configured, add the primary URL
        if not self.has_base_path:
            candidates.append(self.hook_url(hook_name))

        return candidates

    def get_web_base_path(self, base_path_override: str | None = None) -> str:
        """
        Get the web base path for URL construction.

        This is useful when you need just the path component for manual URL building.

        Args:
            base_path_override: Optional base path override

        Returns:
            Web base path with leading and trailing slashes (e.g., "/r/", "/")
        """
        path = base_path_override if base_path_override is not None else self._base_path
        if path:
            return f"/{path}/"
        else:
            return "/"

    def __repr__(self) -> str:
        """String representation for debugging."""
        return f"GerritUrlBuilder(host='{self.host}', base_path='{self._base_path}')"


def create_gerrit_url_builder(host: str, base_path: str | None = None) -> GerritUrlBuilder:
    """
    Factory function to create a GerritUrlBuilder instance.

    This is the preferred way to create URL builders throughout the application.

    Args:
        host: Gerrit hostname
        base_path: Optional base path override

    Returns:
        Configured GerritUrlBuilder instance
    """
    return GerritUrlBuilder(host, base_path)
