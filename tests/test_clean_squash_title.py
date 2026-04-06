# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for _clean_squash_title_line (issue #187).

The module-level helper in core.py handles markdown removal, separator
splitting, and length truncation for squashed commit titles.  The bug
reported in #187 caused conventional commit prefixes like
``Build(deps):`` to be treated as sentence break-points, truncating
the entire description when the title exceeded 100 characters.
"""

from __future__ import annotations

import pytest

from github2gerrit.core import _clean_squash_title_line


# -------------------------------------------------------------------
# Issue #187: conventional commit prefix must survive truncation
# -------------------------------------------------------------------


class TestConventionalPrefixPreservation:
    """Titles with a CC prefix must not be split on the prefix colon."""

    def test_long_build_deps_title_preserved(self) -> None:
        """Exact scenario from issue #187."""
        title = (
            "Build(deps): Bump "
            "lfit/releng-reusable-workflows/"
            ".github/workflows/"
            "gerrit-required-info-yaml-verify.yaml "
            "from 0.2.28 to 0.2.31"
        )
        result = _clean_squash_title_line(title)
        # Must NOT be truncated to just "Build(deps):" (the old bug)
        assert result != "Build(deps):"
        # CC titles with long paths pass through in full — the length
        # is inherent to the structured subject, not body leakage.
        assert result == title

    def test_long_chore_deps_title_preserved(self) -> None:
        """Long chore(deps) title must keep content after prefix."""
        title = (
            "chore(deps): bump "
            "very-long-org/very-long-repo-name/"
            "some-deeply-nested-package-name "
            "from 1.0.0 to 2.0.0 in the production group"
        )
        result = _clean_squash_title_line(title)
        # Full title preserved — no word-boundary fallback for CC
        assert result == title

    def test_long_fix_scope_title_preserved(self) -> None:
        """Long fix(scope) title must not truncate at prefix colon."""
        title = (
            "Fix(security): resolve critical vulnerability in "
            "authentication module that allows bypass of "
            "multi-factor authentication checks for admin users"
        )
        result = _clean_squash_title_line(title)
        # No break-points in the content after the prefix, so the
        # full CC title passes through unchanged.
        assert result == title

    def test_long_feat_breaking_title_preserved(self) -> None:
        """Breaking change indicator must be preserved."""
        title = (
            "Feat!: redesign the entire authentication subsystem "
            "to support OAuth2 and SAML providers alongside "
            "existing LDAP integration for enterprise customers"
        )
        result = _clean_squash_title_line(title)
        # Full CC title preserved (no content break-points)
        assert result == title

    def test_second_colon_used_as_break_point(self) -> None:
        """A second ': ' after the prefix IS a valid break-point."""
        title = (
            "Chore(deps): update authentication module"
            ": migrate legacy session handling to modern "
            "token-based approach with enhanced security"
        )
        result = _clean_squash_title_line(title)
        # Should break at the second ": " (after "module"),
        # which IS in the content region, not the prefix.
        assert result == "Chore(deps): update authentication module:"


# -------------------------------------------------------------------
# Underscore preservation (secondary fix in #187)
# -------------------------------------------------------------------


class TestUnderscorePreservation:
    """Underscores in package/path names must not be stripped."""

    def test_underscore_in_package_name(self) -> None:
        """Package names with underscores must survive cleaning."""
        title = "Bump my_org/setup_python from 1.0 to 2.0"
        result = _clean_squash_title_line(title)
        assert "my_org/setup_python" in result

    def test_underscore_in_path(self) -> None:
        """Filesystem paths with underscores must survive."""
        title = (
            "Update .github/workflows/my_custom_workflow.yml from 1.0 to 2.0"
        )
        result = _clean_squash_title_line(title)
        assert "my_custom_workflow" in result

    def test_asterisk_still_removed(self) -> None:
        """Markdown bold asterisks should still be removed."""
        title = "Bump **important** package from 1.0 to 2.0"
        result = _clean_squash_title_line(title)
        assert "**" not in result
        assert "important" in result

    def test_backtick_still_removed(self) -> None:
        """Markdown code backticks should still be removed."""
        title = "Bump `some-package` from 1.0 to 2.0"
        result = _clean_squash_title_line(title)
        assert "`" not in result
        assert "some-package" in result


# -------------------------------------------------------------------
# Markdown link removal
# -------------------------------------------------------------------


class TestMarkdownLinkRemoval:
    """Markdown links should be replaced with their display text."""

    def test_markdown_link_replaced(self) -> None:
        """Standard markdown link."""
        title = "[package](https://example.com) update from 1.0 to 2.0"
        result = _clean_squash_title_line(title)
        assert "package" in result
        assert "https://example.com" not in result
        assert "[" not in result


# -------------------------------------------------------------------
# Trailing ellipsis removal
# -------------------------------------------------------------------


class TestEllipsisRemoval:
    """Trailing ellipsis (and everything after) should be stripped."""

    def test_trailing_ellipsis_removed(self) -> None:
        """Three dots at the end."""
        title = "Bump some-package from 1.0 to..."
        result = _clean_squash_title_line(title)
        assert "..." not in result
        assert result.startswith("Bump some-package from 1.0")

    def test_ellipsis_with_trailing_content(self) -> None:
        """Ellipsis followed by extra content."""
        title = "Bump some-package from 1.0 to... (truncated)"
        result = _clean_squash_title_line(title)
        assert "..." not in result
        assert "truncated" not in result


# -------------------------------------------------------------------
# Separator splitting
# -------------------------------------------------------------------


class TestSeparatorSplitting:
    """Titles with body-leak separators should be split correctly."""

    def test_bumps_separator(self) -> None:
        """'. Bumps ' separator splits correctly."""
        title = "Update deps. Bumps requests from 2.31 to 2.32"
        result = _clean_squash_title_line(title)
        assert result == "Update deps"

    def test_space_bumps_separator(self) -> None:
        """' Bumps ' separator splits correctly."""
        title = "Update deps Bumps requests from 2.31 to 2.32"
        result = _clean_squash_title_line(title)
        assert result == "Update deps"

    def test_dot_dash_separator(self) -> None:
        """'. - ' separator splits correctly."""
        title = "Update package. - This is the body content"
        result = _clean_squash_title_line(title)
        assert result == "Update package"

    def test_dash_separator(self) -> None:
        """' - ' separator splits correctly."""
        title = "Update package - This is extra context"
        result = _clean_squash_title_line(title)
        assert result == "Update package"


# -------------------------------------------------------------------
# Length truncation (non-CC titles)
# -------------------------------------------------------------------


class TestLengthTruncation:
    """Titles exceeding 100 chars should be truncated sensibly."""

    def test_short_title_not_truncated(self) -> None:
        """Titles under 100 chars pass through unchanged."""
        title = "Bump requests from 2.31.0 to 2.32.0"
        result = _clean_squash_title_line(title)
        assert result == title

    def test_exactly_100_chars_not_truncated(self) -> None:
        """Title at exactly 100 chars should not be truncated."""
        title = "x" * 100
        result = _clean_squash_title_line(title)
        assert result == title

    def test_long_title_truncated_at_period(self) -> None:
        """Period break-point used for non-CC long titles."""
        title = (
            "This is a long sentence that describes the change. "
            "And this is extra content that pushes it well "
            "over the hundred character limit for subjects"
        )
        result = _clean_squash_title_line(title)
        assert result == "This is a long sentence that describes the change."

    def test_long_title_truncated_at_words(self) -> None:
        """Fallback to word-boundary truncation when no break-points."""
        # No break-point characters — just a long string of words
        title = " ".join(["word"] * 30)  # 149 chars
        result = _clean_squash_title_line(title)
        assert len(result) <= 100
        # Should not cut mid-word
        assert result.endswith("word")

    def test_non_cc_colon_break_point_still_works(self) -> None:
        """Colon break-point works for non-CC titles (no CC prefix)."""
        title = (
            "This is a long title describing something important"
            ": and then continues with even more detail "
            "that pushes it well beyond the limit"
        )
        result = _clean_squash_title_line(title)
        assert result == "This is a long title describing something important:"


# -------------------------------------------------------------------
# Edge cases
# -------------------------------------------------------------------


class TestEdgeCases:
    """Boundary and degenerate inputs."""

    def test_empty_string(self) -> None:
        """Empty input returns empty output."""
        assert _clean_squash_title_line("") == ""

    def test_whitespace_only(self) -> None:
        """Whitespace-only input returns empty string."""
        assert _clean_squash_title_line("   ") == ""

    def test_just_cc_prefix(self) -> None:
        """A bare CC prefix (no description) passes through."""
        result = _clean_squash_title_line("chore:")
        assert result == "chore:"

    def test_short_cc_title(self) -> None:
        """Short CC title under 100 chars is untouched."""
        title = "fix(core): resolve null pointer in parser"
        result = _clean_squash_title_line(title)
        assert result == title

    @pytest.mark.parametrize(
        "title",
        [
            "CI(workflow): update release pipeline and add new deployment stages for multi-region support across all environments",
            "Build(deps): Bump org/repo/.github/workflows/very-long-workflow-name.yaml from 0.2.28 to 0.2.31",
            "Refactor(auth): reorganize authentication middleware to support pluggable providers including OAuth2 SAML and LDAP",
        ],
    )
    def test_various_cc_types_not_truncated_at_prefix(self, title: str) -> None:
        """Various CC types must not lose content after the prefix."""
        result = _clean_squash_title_line(title)
        prefix_end = title.index(": ") + 2
        prefix = title[:prefix_end].rstrip()
        # The prefix must be intact
        assert result.startswith(prefix)
        # There must be content after the prefix
        after_prefix = result[len(prefix) :].strip()
        assert len(after_prefix) > 0, (
            f"Content after prefix was lost: {result!r}"
        )
