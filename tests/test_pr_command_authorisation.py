# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Authorisation of ``@github2gerrit`` PR comment directives.

These mirrors are public. Without an authorship check any GitHub user
able to leave a comment could direct the tool, so command recognition is
gated on the comment author's ``author_association``.
"""

from __future__ import annotations

import inspect
from typing import Any
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from github2gerrit import pr_commands
from github2gerrit.github_api import get_trusted_comment_bodies
from github2gerrit.pr_commands import CMD_CREATE_MISSING
from github2gerrit.pr_commands import MENTION_PREFIX
from github2gerrit.pr_commands import contains_directive
from github2gerrit.pr_commands import find_command
from github2gerrit.pr_commands import has_command
from github2gerrit.pr_commands import parse_commands
from github2gerrit.pr_directives import find_pr_command
from github2gerrit.pr_directives import scan_pr_directives
from github2gerrit.trust import DEFAULT_TRUSTED_ASSOCIATIONS
from github2gerrit.trust import describe_trust_policy
from github2gerrit.trust import is_trusted_association
from github2gerrit.trust import trusted_associations


DIRECTIVE = f"{MENTION_PREFIX} {CMD_CREATE_MISSING.name}"


def _comment(body: str, association: str, login: str = "someone") -> Any:
    comment = MagicMock()
    comment.body = body
    comment.author_association = association
    comment.user = MagicMock()
    comment.user.login = login
    return comment


def _pr_with(comments: list[Any]) -> Any:
    issue = MagicMock()
    issue.get_comments.return_value = comments
    pr = MagicMock()
    pr.as_issue.return_value = issue
    return pr


class TestIsTrustedAssociation:
    """The trust rule itself."""

    @pytest.mark.parametrize("association", ["OWNER", "MEMBER", "COLLABORATOR"])
    def test_trusted_values(self, association: str) -> None:
        assert is_trusted_association(association) is True

    @pytest.mark.parametrize(
        "association",
        [
            "CONTRIBUTOR",
            "FIRST_TIME_CONTRIBUTOR",
            "FIRST_TIMER",
            "MANNEQUIN",
            "NONE",
        ],
    )
    def test_untrusted_values(self, association: str) -> None:
        assert is_trusted_association(association) is False

    def test_contributor_is_not_trusted(self) -> None:
        """CONTRIBUTOR only means a PR was merged once; not authority."""
        assert "CONTRIBUTOR" not in DEFAULT_TRUSTED_ASSOCIATIONS

    @pytest.mark.parametrize("association", [None, "", "   ", "bogus"])
    def test_absent_or_unknown_is_untrusted(
        self, association: str | None
    ) -> None:
        assert is_trusted_association(association) is False

    def test_case_and_whitespace_insensitive(self) -> None:
        assert is_trusted_association("  member  ") is True

    def test_explicit_allowed_set_overrides(self) -> None:
        assert is_trusted_association("MEMBER", allowed=["OWNER"]) is False
        assert is_trusted_association("OWNER", allowed=["OWNER"]) is True


class TestTrustedAssociationsEnv:
    """Operators may narrow or widen the trusted set."""

    def test_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("G2G_TRUSTED_ASSOCIATIONS", raising=False)
        assert trusted_associations() == DEFAULT_TRUSTED_ASSOCIATIONS

    def test_override_narrows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("G2G_TRUSTED_ASSOCIATIONS", "OWNER")
        assert trusted_associations() == frozenset({"OWNER"})
        assert is_trusted_association("MEMBER") is False

    def test_override_is_normalised(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("G2G_TRUSTED_ASSOCIATIONS", " owner , member ")
        assert trusted_associations() == frozenset({"OWNER", "MEMBER"})

    @pytest.mark.parametrize("raw", ["", "   ", ",", " , , "])
    def test_blank_override_keeps_default(
        self, raw: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty override must not silently trust nobody or everybody."""
        monkeypatch.setenv("G2G_TRUSTED_ASSOCIATIONS", raw)
        assert trusted_associations() == DEFAULT_TRUSTED_ASSOCIATIONS

    def test_policy_description_is_stable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("G2G_TRUSTED_ASSOCIATIONS", "MEMBER,OWNER")
        assert describe_trust_policy() == "MEMBER, OWNER"


class TestGetTrustedCommentBodies:
    """Comment partitioning at the API boundary."""

    def test_untrusted_bodies_excluded(self) -> None:
        pr = _pr_with(
            [
                _comment(DIRECTIVE, "NONE", "outsider"),
                _comment("looks good", "MEMBER", "maintainer"),
            ]
        )

        bodies, ignored = get_trusted_comment_bodies(
            pr, directive_detector=contains_directive
        )

        assert bodies == ["looks good"]
        assert ignored == ["outsider (NONE)"]

    def test_trusted_directive_kept(self) -> None:
        pr = _pr_with([_comment(DIRECTIVE, "MEMBER", "maintainer")])

        bodies, ignored = get_trusted_comment_bodies(
            pr, directive_detector=contains_directive
        )

        assert bodies == [DIRECTIVE]
        assert ignored == []

    def test_untrusted_chatter_is_not_reported(self) -> None:
        """Only ignored *directives* are worth reporting."""
        pr = _pr_with([_comment("nice work", "NONE", "outsider")])

        bodies, ignored = get_trusted_comment_bodies(
            pr, directive_detector=contains_directive
        )

        assert bodies == []
        assert ignored == []

    @pytest.mark.parametrize(
        "body",
        [
            MENTION_PREFIX,
            f"{MENTION_PREFIX}   ",
            f"thanks {MENTION_PREFIX}",
        ],
    )
    def test_bare_mention_is_not_a_directive(self, body: str) -> None:
        """A mention with no command must not warn on every scan.

        ``parse_commands`` treats a bare mention as neither a match nor
        an unrecognised directive, so reporting it here would produce a
        persistent warning about a comment nobody can act on.
        """
        pr = _pr_with([_comment(body, "NONE", "outsider")])

        bodies, ignored = get_trusted_comment_bodies(
            pr, directive_detector=contains_directive
        )

        assert bodies == []
        assert ignored == []

    def test_missing_association_is_untrusted(self) -> None:
        comment = MagicMock()
        comment.body = DIRECTIVE
        comment.author_association = None
        comment.user = MagicMock()
        comment.user.login = "ghost"

        bodies, ignored = get_trusted_comment_bodies(
            _pr_with([comment]), directive_detector=contains_directive
        )

        assert bodies == []
        assert ignored == ["ghost (unknown)"]

    def test_no_detector_reports_nothing(self) -> None:
        """Reporting is opt-in; the grammar belongs to the caller."""
        pr = _pr_with([_comment(DIRECTIVE, "NONE", "outsider")])

        bodies, ignored = get_trusted_comment_bodies(pr)

        assert bodies == []
        assert ignored == []

    def test_ordering_preserved(self) -> None:
        pr = _pr_with(
            [
                _comment("first", "OWNER"),
                _comment("second", "COLLABORATOR"),
            ]
        )

        bodies, _ = get_trusted_comment_bodies(pr)

        assert bodies == ["first", "second"]

    def test_empty_bodies_skipped(self) -> None:
        pr = _pr_with([_comment("", "OWNER"), _comment("kept", "OWNER")])

        bodies, _ = get_trusted_comment_bodies(pr)

        assert bodies == ["kept"]


class TestPrDirectivesEntryPoint:
    """The composed entry point is the supported way in.

    Consumers reaching for the registry must not have to remember to
    filter by author trust themselves; forgetting is exactly how the
    original defect arose.
    """

    def test_scan_authorises_before_parsing(self) -> None:
        pr = _pr_with(
            [
                _comment(DIRECTIVE, "NONE", "outsider"),
                _comment("looks good", "MEMBER", "maintainer"),
            ]
        )

        scan = scan_pr_directives(pr)

        assert scan.result.has(CMD_CREATE_MISSING.name) is False
        assert scan.ignored == ["outsider (NONE)"]

    def test_scan_keeps_trusted_command(self) -> None:
        pr = _pr_with([_comment(DIRECTIVE, "OWNER", "owner")])

        scan = scan_pr_directives(pr)

        assert scan.result.has(CMD_CREATE_MISSING.name) is True
        assert scan.ignored == []

    def test_find_pr_command_matches_trusted_only(self) -> None:
        untrusted = _pr_with([_comment(DIRECTIVE, "CONTRIBUTOR")])
        trusted = _pr_with([_comment(DIRECTIVE, "COLLABORATOR")])

        assert find_pr_command(untrusted, CMD_CREATE_MISSING.name) is None
        match = find_pr_command(trusted, CMD_CREATE_MISSING.name)
        assert match is not None
        assert match.command_name == CMD_CREATE_MISSING.name

    def test_unknown_command_returns_none(self) -> None:
        pr = _pr_with([_comment(DIRECTIVE, "OWNER")])

        assert find_pr_command(pr, "no such command") is None

    def test_refusal_logged_once_by_the_entry_point(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        pr = _pr_with([_comment(DIRECTIVE, "NONE", "outsider")])

        with caplog.at_level("WARNING"):
            find_pr_command(pr, CMD_CREATE_MISSING.name)

        assert caplog.text.count("outsider (NONE)") == 1


class TestRegistryDocumentsTheGate:
    """Issue #382 asks that new commands inherit the gate.

    The parser cannot enforce that itself without reaching the network,
    so the contract is carried in the naming and the documentation.
    A future contributor adding command number two should meet it.
    """

    def test_parser_parameters_name_the_contract(self) -> None:
        for func in (parse_commands, has_command, find_command):
            first = next(iter(inspect.signature(func).parameters))
            assert first == "trusted_comment_bodies", (
                f"{func.__name__} should name its input as trusted, so a "
                "caller passing raw API comments notices"
            )

    def test_registry_module_warns_against_direct_use(self) -> None:
        doc = pr_commands.__doc__ or ""
        assert "no authorisation" in doc.lower()
        assert "pr_directives" in doc


class TestShouldCreateMissingAuthorisation:
    """End-to-end gating of the one registered command."""

    def _run(self, comments: list[Any]) -> bool:
        from github2gerrit.core import Orchestrator

        orch = Orchestrator.__new__(Orchestrator)
        inputs = MagicMock()
        inputs.create_missing = False
        gh = MagicMock()
        gh.pr_number = 29

        pr = _pr_with(comments)
        with (
            patch("github2gerrit.core.build_client"),
            patch("github2gerrit.core.get_repo_from_env"),
            patch("github2gerrit.core.get_pull", return_value=pr),
        ):
            return orch._should_create_missing(inputs, gh)[0]

    def test_directive_from_outsider_ignored(self) -> None:
        assert self._run([_comment(DIRECTIVE, "NONE", "outsider")]) is False

    def test_directive_from_contributor_ignored(self) -> None:
        """A merged PR in the past confers no authority."""
        assert self._run([_comment(DIRECTIVE, "CONTRIBUTOR")]) is False

    @pytest.mark.parametrize("association", ["OWNER", "MEMBER", "COLLABORATOR"])
    def test_directive_from_trusted_author_honoured(
        self, association: str
    ) -> None:
        assert self._run([_comment(DIRECTIVE, association)]) is True

    def test_outsider_cannot_ride_on_trusted_chatter(self) -> None:
        """A trusted unrelated comment must not launder the directive."""
        comments = [
            _comment("looks good to me", "MEMBER", "maintainer"),
            _comment(DIRECTIVE, "NONE", "outsider"),
        ]
        assert self._run(comments) is False

    def test_ignored_directive_is_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Silent refusal would leave the author with no explanation."""
        with caplog.at_level("WARNING"):
            self._run([_comment(DIRECTIVE, "NONE", "outsider")])

        assert "outsider (NONE)" in caplog.text
        assert "untrusted" in caplog.text.lower()
