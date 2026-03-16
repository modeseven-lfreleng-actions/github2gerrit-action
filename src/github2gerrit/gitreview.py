# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation
"""
Shared ``.gitreview`` file parsing and fetching utilities.

This module provides the **single source of truth** for reading,
parsing, and remotely fetching ``.gitreview`` files across the entire
``github2gerrit`` package.  Three previously independent implementations
(in ``config.py``, ``core.py``, and ``duplicate_detection.py``) are
consolidated here.

Design goals
~~~~~~~~~~~~

* **Stdlib-only by default** — no mandatory PyGithub / external imports.
  The GitHub API fetch path uses lazy imports so that lightweight callers
  (e.g. ``config.py``) do not pull in heavy dependencies.
* **Superset of all callers** — every field and behaviour required by
  ``config.py`` (host-only), ``core.py`` (host+port+project), and
  ``duplicate_detection.py`` (host+project) is supported, plus the
  ``base_path`` field used by the sister ``dependamerge`` project.
* **Consistent regex** — a single set of precompiled patterns that
  tolerates optional whitespace around ``=`` and is case-insensitive on
  keys (both forms seen in the wild).
* **Bug-free fetching** — URL-encodes branch names, deduplicates the
  branch list, and validates raw URLs before opening them.
"""

from __future__ import annotations

import logging
import os
import re
import urllib.parse
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from typing import Final


log = logging.getLogger(__name__)

# ───────────────────────────────────────────────────────────────────────
# Constants
# ───────────────────────────────────────────────────────────────────────

DEFAULT_GERRIT_PORT: int = 29418
"""Default Gerrit SSH port when the ``port=`` line is absent."""

# ───────────────────────────────────────────────────────────────────────
# Precompiled regex patterns for INI-style .gitreview files
#
# These patterns:
#   • are multiline (``(?m)``)
#   • are case-insensitive on the key name (``(?i)``)
#   • tolerate optional horizontal whitespace around ``=``
# ───────────────────────────────────────────────────────────────────────

_HOST_RE = re.compile(r"(?mi)^host[ \t]*=[ \t]*(.+)$")
_PORT_RE = re.compile(r"(?mi)^port[ \t]*=[ \t]*(\d+)$")
_PROJECT_RE = re.compile(r"(?mi)^project[ \t]*=[ \t]*(.+)$")

# ───────────────────────────────────────────────────────────────────────
# Data model
# ───────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class GitReviewInfo:
    """Parsed contents of a ``.gitreview`` file.

    A typical ``.gitreview`` looks like::

        [gerrit]
        host=gerrit.linuxfoundation.org
        port=29418
        project=releng/gerrit_to_platform.git

    Attributes:
        host: Gerrit server hostname
            (e.g. ``gerrit.linuxfoundation.org``).
        port: Gerrit SSH port (default 29418).  Not used for REST,
            but kept for completeness and parity with ``dependamerge``.
        project: Gerrit project path **without** the ``.git`` suffix.
        base_path: Optional REST API base path derived from well-known
            host conventions (e.g. ``"infra"`` for
            ``gerrit.linuxfoundation.org``).  ``None`` when the host is
            not in the known-hosts table — callers may fall back to
            dynamic discovery (see ``gerrit_urls.py``).
    """

    host: str
    port: int = DEFAULT_GERRIT_PORT
    project: str = ""
    base_path: str | None = None

    @property
    def is_valid(self) -> bool:
        """Minimum validity: *host* must be non-empty."""
        return bool(self.host)


# Backward-compatible alias so that ``from github2gerrit.core import
# GerritInfo`` (or any other existing import) continues to work after
# callers are migrated to import from this module instead.
GerritInfo = GitReviewInfo
"""Alias for :class:`GitReviewInfo`.

Existing code throughout ``core.py``, ``orchestrator/reconciliation.py``,
and 16+ test files references ``GerritInfo``.  This alias lets them
switch their import to ``from github2gerrit.gitreview import GerritInfo``
(or keep importing from ``core`` which will re-export it) without any
other code changes.

``GerritInfo`` and ``GitReviewInfo`` are the **same** frozen dataclass —
the only difference from the old ``GerritInfo(host, port, project)`` is
the addition of the optional ``base_path`` field (default ``None``).
"""


# ───────────────────────────────────────────────────────────────────────
# Well-known Gerrit base paths
# ───────────────────────────────────────────────────────────────────────

_KNOWN_BASE_PATHS: dict[str, str] = {
    "gerrit.linuxfoundation.org": "infra",
}


def derive_base_path(host: str) -> str | None:
    """Return the REST API base path for well-known Gerrit hosts.

    Some Gerrit deployments (notably the Linux Foundation's) serve their
    REST API and web UI under a sub-path such as ``/infra``.

    This performs a **static, zero-I/O** lookup against a built-in table
    of known hosts.  For hosts not in the table it returns ``None`` —
    callers that need a definitive answer should fall back to the
    dynamic HTTP-probe discovery in ``gerrit_urls.py``.

    Args:
        host: Gerrit server hostname (will be lowercased for lookup).

    Returns:
        Base path string (e.g. ``"infra"``) or ``None`` if the host is
        not in the known-hosts table.
    """
    return _KNOWN_BASE_PATHS.get(host.lower().strip())


# Keep a private alias so internal callers don't break if they were
# referencing the underscore-prefixed name during development.
_derive_base_path = derive_base_path


# ───────────────────────────────────────────────────────────────────────
# Pure parser
# ───────────────────────────────────────────────────────────────────────


def parse_gitreview(text: str) -> GitReviewInfo | None:
    """Parse the raw text of a ``.gitreview`` file.

    The format is a simple INI-style file with a ``[gerrit]`` section
    containing ``host=``, ``port=``, and ``project=`` keys.

    This parser is intentionally lenient:

    * Keys are matched case-insensitively.
    * Optional whitespace around ``=`` is tolerated.
    * The ``[gerrit]`` section header itself is **not** required —
      the parser matches the key lines directly.

    Args:
        text: Raw text content of a ``.gitreview`` file.

    Returns:
        A :class:`GitReviewInfo` if at least ``host`` is present and
        non-empty, otherwise ``None``.
    """
    host_match = _HOST_RE.search(text)
    if not host_match:
        log.debug(".gitreview: no host= line found")
        return None

    host = host_match.group(1).strip()
    if not host:
        log.debug(".gitreview: host= line is empty")
        return None

    port_match = _PORT_RE.search(text)
    port = int(port_match.group(1)) if port_match else DEFAULT_GERRIT_PORT

    project = ""
    project_match = _PROJECT_RE.search(text)
    if project_match:
        project = project_match.group(1).strip().removesuffix(".git")

    base_path = derive_base_path(host)

    info = GitReviewInfo(
        host=host,
        port=port,
        project=project,
        base_path=base_path,
    )
    log.debug(
        "Parsed .gitreview: host=%s, port=%d, project=%s, base_path=%s",
        info.host,
        info.port,
        info.project,
        info.base_path,
    )
    return info


# ───────────────────────────────────────────────────────────────────────
# Local file reader
# ───────────────────────────────────────────────────────────────────────


def read_local_gitreview(path: Path | None = None) -> GitReviewInfo | None:
    """Read and parse a local ``.gitreview`` file.

    Args:
        path: Explicit path to the file.  When ``None``, defaults to
            ``Path(".gitreview")`` (i.e. the current working directory).

    Returns:
        A :class:`GitReviewInfo` on success, or ``None`` if the file
        does not exist, is unreadable, or lacks required fields.
    """
    target = path or Path(".gitreview")
    if not target.exists():
        log.debug("Local .gitreview not found: %s", target)
        return None

    try:
        text = target.read_text(encoding="utf-8")
    except Exception as exc:
        log.debug("Failed to read local .gitreview %s: %s", target, exc)
        return None

    info = parse_gitreview(text)
    if info:
        log.debug("Read local .gitreview: %s", info)
    else:
        log.debug("Local .gitreview at %s missing required fields", target)
    return info


# ───────────────────────────────────────────────────────────────────────
# Remote fetch: raw.githubusercontent.com  (stdlib only)
# ───────────────────────────────────────────────────────────────────────


def _build_branch_list(
    *,
    extra_branches: Sequence[str] = (),
    include_env_refs: bool = True,
    default_branches: Sequence[str] = ("master", "main"),
) -> list[str]:
    """Build an ordered, deduplicated list of branches to try.

    Priority order:

    1. ``extra_branches`` (caller-supplied, e.g. from ``GitHubContext``)
    2. ``GITHUB_HEAD_REF`` / ``GITHUB_BASE_REF`` environment variables
       (when *include_env_refs* is ``True``)
    3. ``default_branches`` (``master``, ``main``)

    Empty / ``None`` entries and duplicates are silently dropped.

    Args:
        extra_branches: Additional branch names to prepend.
        include_env_refs: Whether to consult ``GITHUB_HEAD_REF`` and
            ``GITHUB_BASE_REF`` from the environment.
        default_branches: Fallback branch names appended at the end.

    Returns:
        Ordered list of unique, non-empty branch names.
    """
    candidates: list[str] = []
    candidates.extend(extra_branches)

    if include_env_refs:
        for var in ("GITHUB_HEAD_REF", "GITHUB_BASE_REF"):
            ref = (os.getenv(var) or "").strip()
            if ref:
                candidates.append(ref)

    candidates.extend(default_branches)

    # Deduplicate while preserving order
    seen: set[str] = set()
    result: list[str] = []
    for branch in candidates:
        if branch and branch not in seen:
            seen.add(branch)
            result.append(branch)
    return result


def _validate_raw_url(url: str) -> bool:
    """Return ``True`` only if *url* is an HTTPS raw.githubusercontent URL."""
    parsed = urllib.parse.urlparse(url)
    return (
        parsed.scheme == "https"
        and parsed.netloc == "raw.githubusercontent.com"
    )


def fetch_gitreview_raw(
    repo_full: str,
    *,
    branches: Sequence[str] = (),
    include_env_refs: bool = True,
    default_branches: Sequence[str] = ("master", "main"),
    timeout: float = 5.0,
) -> GitReviewInfo | None:
    """Fetch ``.gitreview`` from ``raw.githubusercontent.com``.

    Iterates over a list of candidate branches (see
    :func:`_build_branch_list`) and returns the first successful parse.

    Branch names are URL-encoded (preserving ``/`` for path separators)
    and the final URL is validated before opening.

    Args:
        repo_full: GitHub repository in ``owner/repo`` format.
        branches: Extra branch names to try first (e.g. from
            ``GitHubContext.head_ref``).
        include_env_refs: Consult ``GITHUB_HEAD_REF`` /
            ``GITHUB_BASE_REF`` environment variables.
        default_branches: Fallback branch names (default:
            ``("master", "main")``).
        timeout: HTTP request timeout in seconds.

    Returns:
        A :class:`GitReviewInfo` on success, or ``None``.
    """
    repo = (repo_full or "").strip()
    if not repo or "/" not in repo:
        log.debug("fetch_gitreview_raw: invalid repo_full=%r", repo)
        return None

    branch_list = _build_branch_list(
        extra_branches=branches,
        include_env_refs=include_env_refs,
        default_branches=default_branches,
    )

    for branch in branch_list:
        safe_branch = urllib.parse.quote(branch, safe="/")
        url = (
            f"https://raw.githubusercontent.com/"
            f"{repo}/refs/heads/{safe_branch}/.gitreview"
        )
        if not _validate_raw_url(url):
            log.debug("Skipping invalid raw URL: %s", url)
            continue

        try:
            log.debug("Fetching .gitreview via raw URL: %s", url)
            with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
                text = resp.read().decode("utf-8")
            info = parse_gitreview(text)
            if info:
                log.debug("Fetched .gitreview from branch %s: %s", branch, info)
                return info
        except Exception as exc:
            log.debug(
                "Failed to fetch .gitreview from %s branch: %s", branch, exc
            )

    return None


# ───────────────────────────────────────────────────────────────────────
# Remote fetch: GitHub API  (requires PyGithub — lazy import)
# ───────────────────────────────────────────────────────────────────────


def fetch_gitreview_github_api(
    repo_obj: Any,
    *,
    ref: str | None = None,
) -> GitReviewInfo | None:
    """Fetch ``.gitreview`` via the PyGithub ``Repository.get_contents`` API.

    This is the highest-fidelity remote fetch path: it honours the
    repository's default branch automatically and can target any ref.

    Args:
        repo_obj: A PyGithub ``Repository`` object (or any object
            exposing ``get_contents(path, ref=...)``).
        ref: Optional git ref (branch / tag / SHA).  When ``None`` the
            API returns the default branch content.

    Returns:
        A :class:`GitReviewInfo` on success, or ``None``.
    """
    info: GitReviewInfo | None = None
    try:
        content = (
            repo_obj.get_contents(".gitreview", ref=ref)
            if ref
            else repo_obj.get_contents(".gitreview")
        )
        text = (getattr(content, "decoded_content", b"") or b"").decode("utf-8")
        if text:
            info = parse_gitreview(text)
        else:
            log.debug("GitHub API: .gitreview is empty")
    except Exception as exc:
        log.debug("GitHub API .gitreview fetch failed: %s", exc)

    if info:
        log.debug("Parsed .gitreview via GitHub API: %s", info)
    elif info is None:
        log.debug("GitHub API: .gitreview not available or missing fields")
    return info


# ───────────────────────────────────────────────────────────────────────
# Unified fetch: tries all strategies in priority order
# ───────────────────────────────────────────────────────────────────────


def fetch_gitreview(
    *,
    local_path: Path | None = Path(".gitreview"),
    skip_local: bool = False,
    repo_obj: Any | None = None,
    api_ref: str | None = None,
    repo_full: str = "",
    branches: Sequence[str] = (),
    include_env_refs: bool = True,
    default_branches: Sequence[str] = ("master", "main"),
    raw_timeout: float = 5.0,
) -> GitReviewInfo | None:
    """Fetch and parse ``.gitreview`` using all available strategies.

    Strategies are attempted in priority order:

    1. **Local file** — read from *local_path* (skipped when
       *skip_local* is ``True``).
    2. **GitHub API** — call ``repo_obj.get_contents()`` when a
       PyGithub ``Repository`` object is provided.
    3. **Raw URL** — fetch from ``raw.githubusercontent.com`` over a
       list of candidate branches.

    The first strategy that yields a valid :class:`GitReviewInfo` wins;
    subsequent strategies are not attempted.

    Args:
        local_path: Path to a local ``.gitreview`` file.  Pass ``None``
            to skip the local read (equivalent to *skip_local=True*).
        skip_local: Explicitly skip the local file read (useful for
            composite-action contexts where no reliable local file
            exists).
        repo_obj: Optional PyGithub ``Repository`` object for the
            GitHub API strategy.
        api_ref: Git ref to pass to ``get_contents()`` (branch / tag /
            SHA).  Ignored when *repo_obj* is ``None``.
        repo_full: GitHub repository in ``owner/repo`` format for the
            raw URL strategy.
        branches: Extra branch names to try in the raw URL strategy.
        include_env_refs: Consult ``GITHUB_HEAD_REF`` /
            ``GITHUB_BASE_REF`` for the raw URL strategy.
        default_branches: Fallback branch names for the raw URL
            strategy.
        raw_timeout: HTTP timeout for the raw URL strategy.

    Returns:
        A :class:`GitReviewInfo` on success, or ``None`` when all
        strategies fail or are skipped.
    """
    # 1. Local file
    if not skip_local and local_path is not None:
        info = read_local_gitreview(local_path)
        if info:
            return info

    # 2. GitHub API (when a repo object is available)
    if repo_obj is not None:
        info = fetch_gitreview_github_api(repo_obj, ref=api_ref)
        if info:
            return info

    # 3. Raw URL fallback
    info = fetch_gitreview_raw(
        repo_full,
        branches=branches,
        include_env_refs=include_env_refs,
        default_branches=default_branches,
        timeout=raw_timeout,
    )
    if info:
        return info

    log.debug("All .gitreview fetch strategies exhausted")
    return None


# ───────────────────────────────────────────────────────────────────────
# Convenience: host-only accessor (replaces config._read_gitreview_host)
# ───────────────────────────────────────────────────────────────────────


def read_gitreview_host(
    repository: str | None = None,
    *,
    local_path: Path | None = None,
) -> str | None:
    """Return just the Gerrit **host** from ``.gitreview``.

    This is a thin convenience wrapper around :func:`fetch_gitreview`
    that mirrors the previous ``config._read_gitreview_host()``
    signature for a smooth migration path.

    Args:
        repository: GitHub repository in ``owner/repo`` format
            (optional).  Falls back to ``GITHUB_REPOSITORY`` env var.
        local_path: Explicit path to a local ``.gitreview``.  When
            ``None`` (the default), ``Path(".gitreview")`` is used.

    Returns:
        The ``host`` string, or ``None`` if unavailable.
    """
    repo_full = (repository or os.getenv("GITHUB_REPOSITORY") or "").strip()

    info = fetch_gitreview(
        local_path=local_path or Path(".gitreview"),
        repo_full=repo_full,
    )
    return info.host if info else None


# ───────────────────────────────────────────────────────────────────────
# Factory: build GitReviewInfo from explicit parameters
# ───────────────────────────────────────────────────────────────────────

_BASE_PATH_UNSET: Final[object] = object()
"""Sentinel value for :func:`make_gitreview_info` to distinguish
"caller did not pass *base_path*" from an explicit ``None``."""


def make_gitreview_info(
    host: str,
    port: int = DEFAULT_GERRIT_PORT,
    project: str = "",
    *,
    base_path: str | None | object = _BASE_PATH_UNSET,
) -> GitReviewInfo:
    """Construct a :class:`GitReviewInfo` from explicit parameters.

    When *base_path* is not supplied the static known-hosts table is
    consulted automatically (via :func:`derive_base_path`).  Pass
    ``base_path=None`` explicitly to suppress this lookup.

    This factory is the preferred way to build ``GitReviewInfo`` (a.k.a.
    ``GerritInfo``) instances outside of ``.gitreview`` parsing — for
    example when resolving Gerrit connection info from CLI inputs or
    environment variables.

    Args:
        host: Gerrit server hostname.
        port: Gerrit SSH port (default 29418).
        project: Gerrit project path (without ``.git`` suffix).
        base_path: REST API base path override.  When omitted, derived
            automatically from *host*.

    Returns:
        A frozen :class:`GitReviewInfo` instance.
    """
    resolved_base_path: str | None = (
        derive_base_path(host)
        if base_path is _BASE_PATH_UNSET
        else base_path
        if isinstance(base_path, str)
        else None
    )
    return GitReviewInfo(
        host=host,
        port=port,
        project=project,
        base_path=resolved_base_path,
    )
