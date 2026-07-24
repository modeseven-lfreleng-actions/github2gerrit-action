# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""
Duplicate change detection for github2gerrit.

This module provides functionality to detect potentially duplicate changes
before submitting them to Gerrit, helping to prevent spam and redundant
submissions from automated tools like Dependabot.
"""

import hashlib
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any
from typing import NamedTuple

from .gerrit_urls import create_gerrit_url_builder
from .github_api import GhPullRequest
from .github_api import GhRepository
from .github_api import build_client
from .github_api import get_repo_from_env
from .models import GitHubContext
from .rich_display import safe_console_print
from .trailers import extract_github_metadata


log = logging.getLogger(__name__)

__all__ = [
    "ChangeFingerprint",
    "DuplicateChangeError",
    "DuplicateDetector",
    "check_for_duplicates",
]


class DuplicateChangeError(Exception):
    """Raised when a duplicate change is detected."""

    def __init__(
        self,
        message: str,
        existing_prs: list[int],
        urls: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.existing_prs = existing_prs
        self.urls = urls or []


class ChangeFingerprint:
    """Represents a fingerprint of a change for duplicate detection."""

    def __init__(
        self, title: str, body: str = "", files_changed: list[str] | None = None
    ):
        self.title = title.strip()
        self.body = (body or "").strip()
        self.files_changed = sorted(files_changed or [])
        self._normalized_title = self._normalize_title(title)
        self._content_hash = self._compute_content_hash()

    def _normalize_title(self, title: str) -> str:
        """Normalize PR title for comparison."""
        normalized = title.strip()

        # Remove conventional commit prefixes like "feat:", "fix:", etc.
        normalized = re.sub(
            r"^(feat|fix|docs|style|refactor|test|chore|ci|build|perf)"
            r"(\(.+?\))?: ",
            "",
            normalized,
            flags=re.IGNORECASE,
        )

        normalized = re.sub(r"[*_`]", "", normalized)

        # Remove version number variations for dependency updates
        # E.g., "from 0.6 to 0.8" -> "from x.y.z to x.y.z"
        # Handle v-prefixed versions first, then plain versions
        normalized = re.sub(r"\bv\d+(\.\d+)*(-\w+)?\b", "vx.y.z", normalized)
        normalized = re.sub(r"\b\d+(\.\d+)+(-\w+)?\b", "x.y.z", normalized)
        normalized = re.sub(r"\b\d+\.\d+\b", "x.y.z", normalized)

        normalized = re.sub(r"\b[a-f0-9]{7,40}\b", "commit_hash", normalized)

        # Normalize whitespace
        normalized = re.sub(r"\s+", " ", normalized).strip()

        return normalized.lower()

    def _compute_content_hash(self) -> str:
        """Compute a hash of the change content."""
        content = (
            f"{self._normalized_title}\n{self.body}\n"
            f"{','.join(self.files_changed)}"
        )
        return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]

    def is_similar_to(
        self, other: "ChangeFingerprint", similarity_threshold: float = 0.8
    ) -> bool:
        """Check if this fingerprint is similar to another."""
        # Exact normalized title match
        if self._normalized_title == other._normalized_title:
            return True

        # Content hash match
        if self._content_hash == other._content_hash:
            return True

        # Check for similar file changes (for dependency updates)
        if self.files_changed and other.files_changed:
            common_files = set(self.files_changed) & set(other.files_changed)
            union_files = set(self.files_changed) | set(other.files_changed)
            if common_files and union_files:
                overlap_ratio = len(common_files) / len(union_files)
                # If files overlap, check title similarity (lower threshold)
                if overlap_ratio > 0:
                    return self._titles_similar(other, 0.6)

        return self._titles_similar(other, similarity_threshold)

    def _titles_similar(
        self, other: "ChangeFingerprint", threshold: float
    ) -> bool:
        """Check if titles are similar using simple string similarity."""
        title1 = self._normalized_title
        title2 = other._normalized_title

        if not title1 or not title2:
            return False

        # Simple Jaccard similarity on words
        words1 = set(title1.split())
        words2 = set(title2.split())

        if not words1 or not words2:
            return False

        intersection = len(words1 & words2)
        union = len(words1 | words2)

        return (intersection / union) >= threshold

    def __str__(self) -> str:
        return (
            f"ChangeFingerprint(title='{self.title[:50]}...', "
            f"hash={self._content_hash})"
        )


def _normalize_duplicate_subject(title: str) -> str:
    """Normalize a subject/title for duplicate comparison."""
    normalized = title.strip()
    normalized = re.sub(
        r"^(feat|fix|docs|style|refactor|test|chore|ci|build|perf)"
        r"(\(.+?\))?: ",
        "",
        normalized,
        flags=re.IGNORECASE,
    )
    normalized = re.sub(r"[*_`]", "", normalized)
    normalized = re.sub(r"\bv\d+(\.\d+)*(-\w+)?\b", "vx.y.z", normalized)
    normalized = re.sub(r"\b\d+(\.\d+)+(-\w+)?\b", "x.y.z", normalized)
    normalized = re.sub(r"\b\d+\.\d+\b", "x.y.z", normalized)
    normalized = re.sub(r"\b[a-f0-9]{7,40}\b", "commit_hash", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized.lower()


class _SimilarityInputs(NamedTuple):
    """Precomputed inputs shared across candidate similarity scoring."""

    src_subjects: list[str]
    src_body: str
    src_files: list[str]
    config: Any
    gerrit_host: str
    gerrit_project: str
    successful_base_path: str


class _CandidateScore(NamedTuple):
    """Result of scoring a single Gerrit change candidate."""

    agg: float
    ref: str
    num: int | None
    reasons: list[str]
    is_hit: bool


class DuplicateDetector:
    """Detects duplicate Gerrit changes for GitHub pull requests."""

    def __init__(
        self,
        repo: GhRepository,
        lookback_days: int = 7,
        duplicates_filter: str = "open",
    ):
        self.repo = repo
        self.lookback_days = lookback_days
        self._cutoff_date = datetime.now(UTC) - timedelta(days=lookback_days)
        self.duplicates_filter = duplicates_filter

    def _resolve_gerrit_info_from_env_or_gitreview(
        self, gh: GitHubContext
    ) -> tuple[str, str] | None:
        """Resolve Gerrit host and project from environment or .gitreview file.

        Delegates remote ``.gitreview`` fetching to the shared
        :mod:`github2gerrit.gitreview` module which consolidates parsing,
        URL-encoding, branch deduplication, and URL validation.

        Returns:
            Tuple of (host, project) if found, None otherwise
        """
        from .gitreview import fetch_gitreview_raw

        # First try environment variables (same as core module)
        gerrit_host = os.getenv("GERRIT_SERVER", "").strip()
        gerrit_project = os.getenv("GERRIT_PROJECT", "").strip()

        if gerrit_host and gerrit_project:
            return (gerrit_host, gerrit_project)

        # Skip local .gitreview check in composite action context.
        # The duplicate detection runs before workspace setup, so there's no
        # reliable local .gitreview file to check.  Instead, rely on
        # environment variables or remote fetching.
        log.debug("Skipping local .gitreview check (composite action context)")

        repo_full = gh.repository.strip() if gh.repository else ""
        if not repo_full:
            return None

        # Collect branch hints from the GitHub context
        branches: list[str] = []
        if gh.head_ref:
            branches.append(gh.head_ref)
        if gh.base_ref:
            branches.append(gh.base_ref)

        info = fetch_gitreview_raw(
            repo_full,
            branches=branches,
            include_env_refs=False,  # caller supplies refs explicitly
        )
        if info:
            return (info.host, info.project)

        return None

    def _build_gerrit_rest_client(self, gerrit_host: str) -> Any | None:
        """Build a Gerrit REST API client using centralized framework.

        Credential resolution is handled by build_client_for_host with priority:
        1. Explicit CLI arguments (if passed to build_client_for_host)
        2. .netrc file
        3. Environment variables (GERRIT_HTTP_USER/GERRIT_HTTP_PASSWORD)

        This method does not pass explicit credentials, so only .netrc and
        environment variables are used.
        """
        from .gerrit_rest import build_client_for_host

        try:
            return build_client_for_host(
                gerrit_host,
                timeout=8.0,
                max_attempts=3,
            )
        except Exception as exc:
            log.debug("Failed to create Gerrit REST client: %s", exc)
            return None

    @staticmethod
    def _generate_github_change_hash(gh: GitHubContext) -> str:
        """Generate a deterministic hash for a GitHub PR to identify duplicates.

        This creates a SHA256 hash based on stable PR metadata that uniquely
        identifies the change content, making duplicate detection reliable
        regardless of comment formatting or API issues.

        Args:
            gh: GitHub context containing PR information

        Returns:
            Hex-encoded SHA256 hash string (first 16 characters for readability)
        """
        import hashlib

        # Build hash input from stable, unique PR identifiers
        # Use server_url + repository + pr_number for global uniqueness
        hash_input = f"{gh.server_url}/{gh.repository}/pull/{gh.pr_number}"

        # Create SHA256 hash and take first 16 characters for readability
        hash_bytes = hashlib.sha256(hash_input.encode("utf-8")).digest()
        hash_hex = hash_bytes.hex()[:16]

        log.debug(
            "Generated GitHub change hash for %s: %s", hash_input, hash_hex
        )
        return hash_hex

    def check_for_duplicates(
        self,
        target_pr: GhPullRequest,
        allow_duplicates: bool = False,
        gh: GitHubContext | None = None,
        expected_github_hash: str | None = None,
    ) -> None:
        """Check if the target PR is a duplicate via trailer-aware and subject
        equality against Gerrit.

        Implements a robust, dependency-free duplicate check with trailer
        awareness:
        - First check for existing changes with matching GitHub-Hash trailer
          (short-circuit)
        - Resolve Gerrit host/project from env or .gitreview
        - Query Gerrit changes updated within the lookback window (excluding
          abandoned)
        - Compare normalized subjects (first line) for exact equality
        - If any match, treat as duplicate and either warn or raise

        Args:
            target_pr: The GitHub PR to check for duplicates
            allow_duplicates: If True, log warnings instead of raising errors
            gh: GitHub context for resolving Gerrit configuration
            expected_github_hash: The GitHub-Hash trailer value expected for
                this PR
        """
        pr_number = getattr(target_pr, "number", 0)
        pr_title = (getattr(target_pr, "title", "") or "").strip()

        log.debug(
            "Checking PR #%d for duplicates via subject equality against "
            "Gerrit",
            pr_number,
        )

        if not pr_title:
            log.debug(
                "PR #%d has empty title; skipping duplicate check", pr_number
            )
            return
        if gh is None:
            log.debug("No GitHub context provided; skipping duplicate check")
            return

        # Resolve Gerrit target (host/project)
        gerrit_info = self._resolve_gerrit_info_from_env_or_gitreview(gh)
        if not gerrit_info:
            log.debug(
                "Unable to resolve Gerrit host/project; skipping duplicate "
                "check"
            )
            return
        gerrit_host, gerrit_project = gerrit_info

        normalized_pr_subject = _normalize_duplicate_subject(pr_title)
        log.debug(
            "Normalized PR subject for duplicate check: %s",
            normalized_pr_subject,
        )

        url_builder = create_gerrit_url_builder(gerrit_host)

        # Track which base path actually works for constructing display URLs
        successful_base_path = url_builder.base_path

        # Build query: limit to recent changes, exclude abandoned; prefer open
        query_path, dup_filter, cutoff_date = self._build_duplicate_query_path(
            gerrit_project
        )

        log.debug(
            "Gerrit duplicate query: host=%s project=%s filter=%s cutoff=%s "
            "path=%s",
            gerrit_host,
            gerrit_project or "(any)",
            dup_filter,
            cutoff_date,
            query_path,
        )
        changes = self._load_gerrit_changes(gerrit_host, query_path)
        log.debug(
            "Gerrit query returned %d change(s) for project=%s filter=%s "
            "after=%s",
            len(changes),
            gerrit_project or "(any)",
            dup_filter,
            cutoff_date,
        )

        if changes:
            sample_subjects = ", ".join(
                str(c.get("subject") or "")[:60] for c in changes[:5]
            )
            log.debug("Sample subjects: %s", sample_subjects)

        # First pass: Check for trailer-based matches (GitHub-Hash)
        if expected_github_hash and self._has_trailer_match(
            changes, expected_github_hash, gerrit_project
        ):
            # These are update targets, not duplicates - allow them to proceed
            return

        # Compare normalized subjects for exact equality
        matched = self._find_exact_subject_matches(
            changes, normalized_pr_subject, gerrit_project
        )

        if not matched and self._check_similarity_matches(
            target_pr=target_pr,
            changes=changes,
            gerrit_host=gerrit_host,
            gerrit_project=gerrit_project,
            successful_base_path=successful_base_path,
            allow_duplicates=allow_duplicates,
        ):
            return

        self._handle_exact_matches(
            matched, gerrit_host, successful_base_path, allow_duplicates
        )

    def _build_duplicate_query_path(
        self, gerrit_project: str
    ) -> tuple[str, str, str]:
        """Build the Gerrit REST query path for duplicate detection.

        Returns a tuple of (query_path, dup_filter, cutoff_date) where the
        latter two are reused for diagnostic logging.
        """
        cutoff_date = self._cutoff_date.date().isoformat()
        q_parts = []
        if gerrit_project:
            q_parts.append(f"project:{gerrit_project}")
        # Build status clause from DUPLICATE_TYPES filter (default: open)
        dup_filter = (self.duplicates_filter or "open").strip().lower()
        selected = [s.strip() for s in dup_filter.split(",") if s.strip()]
        valid = {
            "open": "status:open",
            "merged": "status:merged",
            "abandoned": "status:abandoned",
        }
        status_terms = [valid[s] for s in selected if s in valid]
        if not status_terms:
            status_clause = "status:open"
        elif len(status_terms) == 1:
            status_clause = status_terms[0]
        else:
            status_clause = "(" + " OR ".join(status_terms) + ")"
        q_parts.append(status_clause)
        q_parts.append(f"after:{cutoff_date}")
        query = " ".join(q_parts)
        encoded_q = urllib.parse.quote(query, safe="")

        # Build query path for centralized client
        # Try CURRENT_REVISION instead of CURRENT_COMMIT to get revision data
        query_path = (
            f"/changes/?q={encoded_q}&n=50&o=CURRENT_REVISION&o=CURRENT_FILES"
            "&o=MESSAGES"
        )
        return query_path, dup_filter, cutoff_date

    def _load_gerrit_changes(
        self, gerrit_host: str, query_path: str
    ) -> list[dict[str, object]]:
        """Query Gerrit and return the raw change list (empty on failure)."""
        try:
            # Use centralized client that handles base path and auth
            client = self._build_gerrit_rest_client(gerrit_host)
            if client is None:
                log.debug(
                    "Gerrit client not available; skipping duplicate check"
                )
                return []

            log.debug("Querying Gerrit for duplicates: %s", query_path)
            data = client.get(query_path)
            if isinstance(data, list):
                return data
            else:
                return []
        except Exception as exc:
            log.debug("Gerrit query failed for %s: %s", query_path, exc)
            return []

    @staticmethod
    def _get_current_revision(c: dict[str, object]) -> dict[str, Any]:
        """Return the current revision dict for a change (or empty dict)."""
        rev = str(c.get("current_revision") or "")
        revs_obj = c.get("revisions")
        revs = revs_obj if isinstance(revs_obj, dict) else {}
        cur_obj = revs.get(rev) if rev else {}
        return cur_obj if isinstance(cur_obj, dict) else {}

    @staticmethod
    def _extract_commit_message(c: dict[str, object]) -> str:
        """Extract the commit message from a change's revision or messages."""
        # Try to get commit message from revisions first
        cur = DuplicateDetector._get_current_revision(c)
        commit = cur.get("commit") or {}
        msg = str(commit.get("message") or "")

        # If no commit message from revisions, try messages field
        if not msg:
            messages = c.get("messages", [])
            if messages and isinstance(messages, list) and len(messages) > 0:
                # Use the last message (most recent commit)
                last_msg = messages[-1] if messages else {}
                msg = (
                    str(last_msg.get("message", ""))
                    if isinstance(last_msg, dict)
                    else ""
                )
        return msg

    @staticmethod
    def _extract_candidate_files(cur: dict[str, Any]) -> list[str]:
        """Extract candidate file paths from a change's current revision."""
        # Try to get files from current revision; some Gerrit versions may
        # not populate revisions.files, leaving an empty file score.
        files_dict = cur.get("files") or {}
        return [
            p
            for p in files_dict
            if isinstance(p, str) and not p.startswith("/")
        ]

    @staticmethod
    def _extract_pr_files(target_pr: GhPullRequest) -> list[str]:
        """Best-effort retrieval of file paths changed in the PR."""
        src_files: list[str] = []
        try:
            get_files = getattr(target_pr, "get_files", None)
            if callable(get_files):
                files_obj = get_files()
                if isinstance(files_obj, Iterable):
                    for f in files_obj:
                        fname = getattr(f, "filename", None)
                        if fname:
                            src_files.append(str(fname))
        except Exception as exc:
            # Best-effort; if files cannot be retrieved, proceed without them
            log.debug("Failed to retrieve PR files for scoring: %s", exc)
        return src_files

    @staticmethod
    def _build_candidate_ref(
        gerrit_host: str,
        successful_base_path: str,
        proj: str,
        num: int | None,
    ) -> str:
        """Build a display reference/URL for a candidate change."""
        # Use the base path that actually worked for API calls
        display_url_builder = create_gerrit_url_builder(
            gerrit_host, successful_base_path
        )
        return (
            display_url_builder.change_url(proj, num)
            if proj and isinstance(num, int)
            else (f"change {num}" if isinstance(num, int) else "")
        )

    def _has_trailer_match(
        self,
        changes: list[dict[str, object]],
        expected_github_hash: str,
        gerrit_project: str,
    ) -> bool:
        """Return True when a change carries the expected GitHub-Hash trailer.

        Such changes are update targets rather than duplicates.
        """
        log.debug(
            "Checking for GitHub-Hash trailer matches: %s",
            expected_github_hash,
        )
        trailer_matches: list[tuple[int, str]] = []

        for c in changes:
            msg = self._extract_commit_message(c)
            if msg:
                github_metadata = extract_github_metadata(msg)
                change_github_hash = github_metadata.get("GitHub-Hash", "")

                if change_github_hash == expected_github_hash:
                    num = c.get("_number")
                    proj = str(c.get("project") or gerrit_project or "")
                    if isinstance(num, int):
                        trailer_matches.append((num, proj))
                        log.debug(
                            "Found GitHub-Hash trailer match: change %d, "
                            "hash %s",
                            num,
                            change_github_hash,
                        )

        if trailer_matches:
            log.debug(
                "Found %d change(s) with matching GitHub-Hash trailer - "
                "treating as update targets",
                len(trailer_matches),
            )
            return True
        return False

    def _find_exact_subject_matches(
        self,
        changes: list[dict[str, object]],
        normalized_pr_subject: str,
        gerrit_project: str,
    ) -> list[tuple[int, str]]:
        """Return changes whose normalized subject equals the PR subject."""
        matched: list[tuple[int, str]] = []
        for c in changes:
            subj = str(c.get("subject") or "").strip()
            if not subj:
                continue
            if _normalize_duplicate_subject(subj) == normalized_pr_subject:
                num = c.get("_number")
                proj = str(c.get("project") or gerrit_project or "")
                if isinstance(num, int):
                    matched.append((num, proj))
        return matched

    def _check_similarity_matches(
        self,
        *,
        target_pr: GhPullRequest,
        changes: list[dict[str, object]],
        gerrit_host: str,
        gerrit_project: str,
        successful_base_path: str,
        allow_duplicates: bool,
    ) -> bool:
        """Score candidates by similarity when no exact subject match exists.

        Returns ``True`` when a duplicate was detected and allowed (the
        caller should return); raises :class:`DuplicateChangeError` when a
        duplicate is detected and not allowed; returns ``False`` when no
        similar change was found.
        """
        # No exact subject match; proceed with similarity scoring across
        # candidates
        log.debug("No exact-subject matches found; entering similarity scoring")
        from .similarity import ScoringConfig

        config = ScoringConfig()
        # Source features from the PR
        pr_title = (getattr(target_pr, "title", "") or "").strip()
        similarity_inputs = _SimilarityInputs(
            src_subjects=[pr_title],
            src_body=str(getattr(target_pr, "body", "") or ""),
            src_files=self._extract_pr_files(target_pr),
            config=config,
            gerrit_host=gerrit_host,
            gerrit_project=gerrit_project,
            successful_base_path=successful_base_path,
        )

        best_score = 0.0
        best_reasons: list[str] = []
        hits: list[tuple[float, str, int | None]] = []
        all_nums: list[int] = []
        for c in changes:
            score = self._score_candidate(c, similarity_inputs)
            if score is None:
                continue
            # Track best (for reasons)
            if score.agg > best_score:
                best_score = score.agg
                best_reasons = score.reasons
            if score.is_hit:
                hits.append((score.agg, score.ref, score.num))
                if isinstance(score.num, int):
                    all_nums.append(score.num)

        log.debug(
            "Similarity scoring found %d hit(s) (threshold=%.2f)",
            len(hits),
            config.similarity_threshold,
        )
        if hits:
            return self._report_similarity_hits(
                hits,
                best_reasons,
                all_nums,
                config.similarity_threshold,
                allow_duplicates,
            )
        return False

    def _score_candidate(
        self, c: dict[str, object], si: _SimilarityInputs
    ) -> _CandidateScore | None:
        """Score a single Gerrit change candidate against the PR features.

        Returns ``None`` for candidates without a usable subject; otherwise
        a :class:`_CandidateScore` describing the aggregate score, display
        reference and whether it qualifies as a similarity hit.
        """
        from .similarity import aggregate_scores
        from .similarity import remove_commit_trailers
        from .similarity import score_bodies
        from .similarity import score_files
        from .similarity import score_subjects

        subj = str(c.get("subject") or "").strip()
        if not subj:
            return None
        subj = str(c.get("subject") or "")
        cur = self._get_current_revision(c)
        msg = self._extract_commit_message(c)

        cand_body_raw = ""
        if "\n" in msg:
            cand_body_raw = msg.split("\n", 1)[1]
        cand_body = remove_commit_trailers(cand_body_raw)
        cand_files = self._extract_candidate_files(cur)

        # Compute component scores
        s_res = score_subjects(si.src_subjects, subj)
        f_res = score_files(
            si.src_files,
            cand_files,
            workflow_min_floor=si.config.workflow_min_floor,
        )
        b_res = score_bodies(si.src_body, cand_body)

        # Aggregate
        agg = aggregate_scores(
            s_res.score, f_res.score, b_res.score, config=si.config
        )
        log.debug(
            "Aggregate score computed: %.2f (s=%.2f f=%.2f b=%.2f)",
            agg,
            s_res.score,
            f_res.score,
            b_res.score,
        )

        # Build candidate reference and number using successful base path
        num_obj = c.get("_number")
        num = int(num_obj) if isinstance(num_obj, int) else None
        proj = str(c.get("project") or si.gerrit_project or "")
        ref = self._build_candidate_ref(
            si.gerrit_host, si.successful_base_path, proj, num
        )
        log.debug(
            "Scoring candidate: ref=%s agg=%.2f (s=%.2f f=%.2f b=%.2f) "
            "subj='%s'",
            ref or "(none)",
            agg,
            s_res.score,
            f_res.score,
            b_res.score,
            subj[:200],
        )

        # Deduplicate reasons preserving order
        reasons = list(
            dict.fromkeys(s_res.reasons + f_res.reasons + b_res.reasons)
        )

        # Special handling for perfect dependency package matches
        is_perfect_dependency_match = (
            s_res.score == 1.0
            and len(s_res.reasons) > 0
            and any(
                "Same dependency package:" in reason for reason in s_res.reasons
            )
        )

        # Collect candidates above threshold OR perfect dependency matches
        dependency_threshold = 0.45  # Lower threshold for perfect dep matches
        effective_threshold = (
            dependency_threshold
            if is_perfect_dependency_match
            else si.config.similarity_threshold
        )
        is_hit = agg >= effective_threshold and bool(ref)
        if (
            is_hit
            and is_perfect_dependency_match
            and agg < si.config.similarity_threshold
        ):
            log.debug(
                "Perfect dependency match found below normal "
                "threshold: score=%.2f (threshold=%.2f, "
                "dependency_threshold=%.2f)",
                agg,
                si.config.similarity_threshold,
                dependency_threshold,
            )

        return _CandidateScore(
            agg=agg, ref=ref, num=num, reasons=reasons, is_hit=is_hit
        )

    def _report_similarity_hits(
        self,
        hits: list[tuple[float, str, int | None]],
        best_reasons: list[str],
        all_nums: list[int],
        similarity_threshold: float,
        allow_duplicates: bool,
    ) -> bool:
        """Log/announce similarity hits and warn or raise as configured.

        Returns ``True`` when duplicates are allowed (caller should return);
        raises :class:`DuplicateChangeError` otherwise.
        """
        hits_sorted = sorted(hits, key=lambda t: t[0], reverse=True)

        for s, u, _ in hits_sorted:
            if u:
                log.debug("Score: %.2f  URL: %s", s, u)
                safe_console_print(f"🔀 Duplicate change: {u}")
        msg = (
            f"Similar Gerrit change(s) detected [≥ {similarity_threshold:.2f}]"
        )
        if best_reasons:
            msg += f" (Reasons: {', '.join(best_reasons)})"
        if allow_duplicates:
            log.warning("GERRIT DUPLICATE DETECTED (allowed): %s", msg)
            return True
        raise DuplicateChangeError(msg, all_nums)

    def _handle_exact_matches(
        self,
        matched: list[tuple[int, str]],
        gerrit_host: str,
        successful_base_path: str,
        allow_duplicates: bool,
    ) -> None:
        """Announce exact subject matches and warn or raise as configured."""
        # Construct human-friendly references for logs
        matching_numbers: list[int] = []
        match_lines: list[str] = []
        duplicate_urls: list[str] = []
        for n, proj in matched:
            if proj:
                # Use the base path that actually worked for API calls
                display_url_builder = create_gerrit_url_builder(
                    gerrit_host, successful_base_path
                )
                url = display_url_builder.change_url(proj, n)
                match_lines.append(f"Score: 1.0  URL: {url}")
                duplicate_urls.append(url)
                log.debug("Score: 1.0  URL: %s", url)
                safe_console_print(f"🔀 Duplicate change: {url}")
            else:
                match_lines.append(f"Score: 1.0  URL: change {n}")
                duplicate_urls.append(f"change {n}")
                log.debug("Score: 1.0  URL: change %s", n)
            matching_numbers.append(n)

        if not matched:
            log.debug(
                "No exact subject matches and no similarity matches; "
                "duplicate check passes"
            )
            return

        # Remove PR number from message since cli.py already includes it
        full_message = "subject matches existing Gerrit change(s)"
        if allow_duplicates:
            log.debug("GERRIT DUPLICATE DETECTED (allowed): %s", full_message)
            return
        raise DuplicateChangeError(
            full_message, matching_numbers, duplicate_urls
        )


def check_for_duplicates(
    gh: GitHubContext,
    allow_duplicates: bool = False,
    lookback_days: int = 7,
    expected_github_hash: str | None = None,
) -> None:
    """Convenience function to check for duplicates.

    Args:
        gh: GitHub context containing PR information
        allow_duplicates: If True, only log warnings; if False, raise exception
        lookback_days: Number of days to look back for similar PRs
        expected_github_hash: The GitHub-Hash trailer value expected for this PR

    Raises:
        DuplicateChangeError: If duplicates found and allow_duplicates=False
    """
    if not gh.pr_number:
        log.debug("No PR number provided, skipping duplicate check")
        return

    # Skip duplicate check entirely if duplicates are allowed
    if allow_duplicates:
        log.debug(
            "Duplicate detection skipped for PR #%s (allow_duplicates=True)",
            gh.pr_number,
        )
        return

    log.debug("Starting duplicate detection for PR #%s", gh.pr_number)
    log.debug(
        "Duplicate check parameters: allow_duplicates=%s, lookback_days=%s",
        allow_duplicates,
        lookback_days,
    )
    log.debug("Expected GitHub hash: %s", expected_github_hash)

    try:
        client = build_client()
        repo = get_repo_from_env(client)
        log.debug(
            "GitHub repository: %s", getattr(repo, "full_name", "unknown")
        )

        target_pr = repo.get_pull(gh.pr_number)
        log.debug("Retrieved PR #%s: %s", target_pr.number, target_pr.title)

        duplicate_types = os.getenv("DUPLICATE_TYPES", "open")
        log.debug(
            "Checking for duplicates in Gerrit changes with states: %s",
            duplicate_types,
        )
        detector = DuplicateDetector(
            repo,
            lookback_days=lookback_days,
            duplicates_filter=duplicate_types,
        )
        detector.check_for_duplicates(
            target_pr,
            allow_duplicates=allow_duplicates,
            gh=gh,
            expected_github_hash=expected_github_hash,
        )

        log.debug(
            "Duplicate check completed successfully for PR #%d", gh.pr_number
        )

    except DuplicateChangeError:
        # Re-raise duplicate errors
        raise
    except Exception as exc:
        log.warning(
            "Duplicate detection failed for PR #%d: %s", gh.pr_number, exc
        )
        # Don't fail the entire process if duplicate detection has issues
