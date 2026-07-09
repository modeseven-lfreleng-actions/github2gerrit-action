# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation
"""
Gerrit query utilities for topic-based change discovery.

This module provides functions to query Gerrit REST API for changes
based on topics, with support for pagination and safe parsing.
"""

import logging
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote
from urllib.parse import urlparse

from .gerrit_rest import GerritRestClient
from .gerrit_rest import warn_gerrit_credentials_unavailable
from .trailers import GITHUB_PR_TRAILER
from .trailers import parse_trailers
from .utils import env_bool
from .utils import log_warning_once


log = logging.getLogger(__name__)


def build_gerrit_topic(
    project_github: str,
    pr_number: int | str | None = None,
) -> str:
    """Build the canonical Gerrit topic for a GitHub pull request.

    The topic format is ``<prefix>-<project_github>[-<pr_number>]``
    where ``<prefix>`` comes from ``G2G_TOPIC_PREFIX`` (default
    ``GH``) and ``project_github`` is the GitHub-style project name
    (Gerrit project path with ``/`` replaced by ``-``, or the GitHub
    repository name when no ``.gitreview`` mapping exists).

    This single helper serves both the push path (git-review ``-t``)
    and every topic-based query/recovery path, guaranteeing that
    lookups match the topics the tool pushes.

    Args:
        project_github: GitHub-style project name (no owner prefix).
        pr_number: Pull request number; omitted from the topic when
            falsy or ``0``/``"0"`` (the documented sentinel meaning
            "process all open PRs", never a real PR number).

    Returns:
        The canonical topic string.
    """
    prefix = os.getenv("G2G_TOPIC_PREFIX", "GH").strip() or "GH"
    if pr_number is not None:
        pr_str = str(pr_number).strip()
        if pr_str and pr_str != "0":
            return f"{prefix}-{project_github}-{pr_str}"
    return f"{prefix}-{project_github}"


def derive_project_github(repository: str) -> str:
    """Derive a GitHub-style project name from ``owner/repo``.

    Fallback used when resolved repository names are unavailable
    (mirrors the fallback branch of
    ``core.Orchestrator._derive_repo_names``).
    """
    if "/" in repository:
        return repository.split("/")[-1]
    return repository


def _gerrit_quote(value: str) -> str:
    """Escape a value for safe use inside Gerrit query double-quotes.

    Gerrit query syntax uses double-quoted strings for values
    containing special characters.  Backslashes and double-quotes
    inside the value must be escaped to prevent query injection or
    malformed queries (e.g. branch names containing ``"``).

    Returns:
        The escaped string (without surrounding quotes).
    """
    return value.replace("\\", "\\\\").replace('"', '\\"')


@dataclass
class GerritChange:
    """Represents a Gerrit change from query results."""

    change_id: str
    number: str
    subject: str
    status: str
    current_revision: str
    files: list[str]
    commit_message: str
    topic: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GerritChange":
        """Create GerritChange from Gerrit REST API response."""
        files: list[str] = []
        commit_message = ""
        current_revision = data.get("current_revision", "")
        if current_revision:
            revisions = data.get("revisions") or {}
            revision_data = revisions.get(current_revision) or {}
            files = list((revision_data.get("files") or {}).keys())
            commit_info = revision_data.get("commit") or {}
            commit_message = commit_info.get("message", "")

        return cls(
            change_id=data.get("change_id", ""),
            number=str(data.get("_number", "")),
            subject=data.get("subject", ""),
            status=data.get("status", ""),
            current_revision=current_revision,
            files=files,
            commit_message=commit_message,
            topic=data.get("topic"),
        )


def query_changes_by_topic(
    client: GerritRestClient,
    topic: str,
    *,
    statuses: list[str] | None = None,
    max_results: int = 100,
) -> list[GerritChange]:
    """
    Query Gerrit for changes matching the given topic.

    Args:
        client: Gerrit REST client
        topic: Topic name to search for
        statuses: List of change statuses to include (default: ["NEW"])
        max_results: Maximum number of results to return

    Returns:
        List of GerritChange objects
    """
    if statuses is None:
        statuses = ["NEW"]

    status_query = " OR ".join(f"status:{status}" for status in statuses)
    query = f'topic:"{_gerrit_quote(topic)}" AND ({status_query})'

    log.debug("Querying Gerrit for changes: %s", query)

    try:
        changes = _execute_query_with_pagination(
            client, query, max_results=max_results
        )
        log.debug(
            "Found %d changes for topic '%s' with statuses %s",
            len(changes),
            topic,
            statuses,
        )
    except Exception as exc:
        log.warning(
            "Failed to query Gerrit changes for topic '%s': %s", topic, exc
        )
        return []
    else:
        return changes


def _change_belongs_to_repository(
    change: GerritChange, github_repository: str
) -> bool:
    """Return True if a change's GitHub-PR trailer targets the given repo.

    The ``GitHub-PR`` trailer is a pull request URL of the form
    ``https://github.com/{owner}/{repo}/pull/{n}``. Matching on it scopes
    anonymous supersession queries to changes created by GitHub2Gerrit for
    the current repository -- a precise replacement for ``owner:self`` that
    does not require an authenticated session.

    The trailer is parsed as a URL and matched on its **path** prefix
    (``/{owner}/{repo}/pull/``) rather than a bare substring. The value must
    be an absolute ``http(s)`` URL (scheme + host), so malformed or
    relative values (e.g. ``/org/repo/pull/1``) and unrelated text that
    merely contains the substring (for example inside a query string) do
    not produce false positives. The host itself is not compared, so
    GitHub Enterprise URLs still match.
    """
    target = github_repository.strip().strip("/").lower()
    if not target:
        return False
    prefix = f"/{target}/pull/"
    trailers = parse_trailers(change.commit_message or "")
    for value in trailers.get(GITHUB_PR_TRAILER, []):
        if not value:
            continue
        try:
            parsed = urlparse(value.strip())
        except ValueError:
            continue
        # Require an absolute http(s) URL; reject relative/malformed values.
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            continue
        if parsed.path.lower().startswith(prefix):
            return True
    return False


def query_open_changes_by_project(
    client: GerritRestClient,
    project: str,
    *,
    branch: str | None = None,
    max_results: int = 100,
    github_repository: str | None = None,
) -> list[GerritChange]:
    """Query open GitHub2Gerrit changes in a Gerrit project.

    Used by the supersession sweep to discover open changes that may be
    superseded by a newer dependency update.

    When the REST client is authenticated, the query is restricted to
    changes owned by the authenticated user via the ``owner:self``
    predicate (the original, most precise behaviour).

    When the client is unauthenticated, ``owner:self`` cannot be used (the
    server rejects it with HTTP 403). If the anonymous fallback is enabled
    (``G2G_ANON_SUPERSEDE_FALLBACK``, default on) and ``github_repository``
    is provided, the query instead lists all open changes in the
    project/branch and keeps only those whose ``GitHub-PR`` trailer targets
    ``github_repository``. This scopes results to GitHub2Gerrit changes for
    the current repository without requiring credentials. If the fallback
    is disabled or no repository is available, the sweep is skipped (with a
    warning) as before.

    Args:
        client: Gerrit REST client.
        project: Gerrit project name (e.g. ``myorg/myrepo``).
        branch: Optional Gerrit branch name to scope the query.
            When provided, only changes targeting this branch are
            returned.
        max_results: Maximum number of results to return.
        github_repository: Current GitHub repository in ``owner/repo``
            form. Required to enable the anonymous fallback.

    Returns:
        List of open ``GerritChange`` objects.
    """
    base_query = f'project:"{_gerrit_quote(project)}" status:open'
    if branch:
        base_query += f' branch:"{_gerrit_quote(branch)}"'

    repo_filter: str | None = None
    if client.is_authenticated:
        query = f"{base_query} owner:self"
    else:
        # owner:self requires an authenticated session. Fall back to an
        # anonymous project/branch query scoped to the current repository
        # via the GitHub-PR trailer, when permitted and possible.
        normalized_repo = (github_repository or "").strip().strip("/")
        fallback_enabled = env_bool("G2G_ANON_SUPERSEDE_FALLBACK", True)
        if not fallback_enabled or not normalized_repo:
            warn_gerrit_credentials_unavailable()
            log.debug(
                "Skipping owner:self query for project '%s': no Gerrit "
                "REST credentials and anonymous fallback unavailable "
                "(enabled=%s, repository=%s)",
                project,
                fallback_enabled,
                normalized_repo,
            )
            return []
        # owner:self is unavailable without authentication. Narrow the
        # anonymous query server-side to GitHub2Gerrit changes (which all
        # carry the GitHub-PR trailer in their commit message) so the
        # result set stays small even on busy projects; the precise
        # repository scoping is then applied client-side below.
        query = f'{base_query} message:"{GITHUB_PR_TRAILER}"'
        repo_filter = normalized_repo
        # Surface the missing-credentials condition once at default level
        # via the shared warn-once helper (its message already notes that
        # fallback behavior may apply); keep the per-call detail at debug,
        # since this can run multiple times per invocation (e.g. Strategy 5
        # and the post-push sweep).
        warn_gerrit_credentials_unavailable()
        log.debug(
            "No Gerrit REST credentials; using anonymous supersession "
            "fallback for project '%s' scoped to repository '%s'",
            project,
            normalized_repo,
        )

    log.debug("Querying Gerrit for open changes: %s", query)

    try:
        changes = _execute_query_with_pagination(
            client, query, max_results=max_results
        )
    except Exception as exc:
        log.warning(
            "Failed to query open Gerrit changes for project '%s': %s",
            project,
            exc,
        )
        return []

    if repo_filter is not None:
        # The cap may simply have been met exactly, but it can also mean
        # the result set was truncated; surface the possibility once rather
        # than silently missing changes (which could leave a duplicate
        # un-reused/un-abandoned).
        if len(changes) >= max_results:
            log_warning_once(
                log,
                "gerrit_anon_supersede_truncated",
                "Anonymous supersession query for project '%s' returned the "
                "maximum of %d result(s); results may be truncated and some "
                "GitHub2Gerrit changes may not have been examined. Provide "
                "Gerrit REST credentials for a precise owner-scoped query.",
                project,
                max_results,
            )
        scoped = [
            change
            for change in changes
            if _change_belongs_to_repository(change, repo_filter)
        ]
        log.debug(
            "Anonymous fallback: %d of %d open change(s) in project "
            "'%s' belong to repository '%s'",
            len(scoped),
            len(changes),
            project,
            repo_filter,
        )
        return scoped

    log.debug(
        "Found %d open changes in project '%s'",
        len(changes),
        project,
    )
    return changes


def _execute_query_with_pagination(
    client: GerritRestClient,
    query: str,
    *,
    max_results: int = 100,
    page_size: int = 25,
) -> list[GerritChange]:
    """
    Execute Gerrit query with pagination support.

    Args:
        client: Gerrit REST client
        query: Gerrit query string
        max_results: Maximum total results to return
        page_size: Results per page

    Returns:
        List of GerritChange objects
    """
    all_changes: list[GerritChange] = []
    start = 0

    while len(all_changes) < max_results:
        remaining = max_results - len(all_changes)
        current_limit = min(page_size, remaining)

        try:
            # Build query URL with parameters
            # Gerrit REST API: /changes/?q=query&n=limit&S=skip&o=options
            query_params = [
                f"q={quote(query, safe='')}",
                f"n={current_limit}",
                f"S={start}",
                "o=CURRENT_REVISION",
                "o=CURRENT_FILES",
                "o=CURRENT_COMMIT",
            ]
            query_path = f"/changes/?{'&'.join(query_params)}"

            response = client.get(query_path)

            if not response:
                break

            # Gerrit REST API returns a list of change objects
            if not isinstance(response, list):
                log.warning(
                    "Unexpected Gerrit query response format: %s",
                    type(response),
                )
                break

            page_changes = []
            for change_data in response:
                try:
                    change = GerritChange.from_dict(change_data)
                    page_changes.append(change)
                except Exception as exc:
                    log.debug("Skipping malformed change data: %s", exc)
                    continue

            all_changes.extend(page_changes)

            # If we got fewer results than requested, we've reached the end
            if len(page_changes) < current_limit:
                break

            start += len(page_changes)

        except Exception as exc:
            log.warning(
                "Failed to fetch Gerrit changes page (start=%d, limit=%d): %s",
                start,
                current_limit,
                exc,
            )
            break

    return all_changes[:max_results]


def extract_pr_metadata_from_commit_message(
    commit_message: str,
) -> dict[str, str]:
    """
    Extract GitHub PR metadata trailers from a commit message.

    Args:
        commit_message: Full commit message text

    Returns:
        Dictionary with extracted metadata (GitHub-PR, GitHub-Hash, etc.)
    """
    metadata = {}

    # Look for trailer-style metadata at the end of the commit message
    lines = commit_message.strip().split("\n")

    # Find the start of trailers (after the last blank line)
    trailer_start = 0
    for i in range(len(lines) - 1, -1, -1):
        if not lines[i].strip():
            trailer_start = i + 1
            break

    for raw_line in lines[trailer_start:]:
        line = raw_line.strip()
        if ":" in line:
            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip()
            if key.startswith("GitHub-"):
                metadata[key] = value

    return metadata


def validate_pr_metadata_match(
    gerrit_changes: list[GerritChange],
    expected_pr_url: str,
    expected_github_hash: str,
) -> list[GerritChange]:
    """
    Filter Gerrit changes to only those matching the expected PR metadata.

    This prevents cross-PR contamination by ensuring changes belong to
    the same GitHub PR based on trailer metadata.

    Args:
        gerrit_changes: List of changes from Gerrit query
        expected_pr_url: Expected GitHub PR URL
        expected_github_hash: Expected GitHub-Hash trailer value

    Returns:
        Filtered list of changes matching the PR metadata
    """
    validated_changes = []

    for change in gerrit_changes:
        metadata = extract_pr_metadata_from_commit_message(
            change.commit_message
        )

        pr_url = metadata.get("GitHub-PR", "")
        if pr_url and pr_url != expected_pr_url:
            log.debug(
                "Excluding change %s: PR URL mismatch (expected=%s, found=%s)",
                change.change_id,
                expected_pr_url,
                pr_url,
            )
            continue

        github_hash = metadata.get("GitHub-Hash", "")
        if github_hash and github_hash != expected_github_hash:
            log.debug(
                "Excluding change %s: GitHub-Hash mismatch "
                "(expected=%s, found=%s)",
                change.change_id,
                expected_github_hash,
                github_hash,
            )
            continue

        validated_changes.append(change)

    if len(validated_changes) != len(gerrit_changes):
        log.info(
            "Filtered Gerrit changes: %d -> %d "
            "(excluded %d due to metadata mismatch)",
            len(gerrit_changes),
            len(validated_changes),
            len(gerrit_changes) - len(validated_changes),
        )

    return validated_changes
