# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Maintainer approval gate for fork pull requests.

Transferring a fork pull request pushes someone else's code into Gerrit
under the tool's SSH identity, where Gerrit CI executes it. These tests
pin the conditions under which that is allowed.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from github2gerrit.models import GitHubContext
from github2gerrit.models import PROperationMode
from github2gerrit.pr_approval import APPROVAL_MARKER
from github2gerrit.pr_approval import ApprovalStatus
from github2gerrit.pr_approval import evaluate_fork_approval
from github2gerrit.pr_approval import render_blocked_comment


BASE_REPO = "opendaylight/mdsal"
FORK_REPO = "contributor/mdsal"
HEAD_SHA = "0b2abdcf7bb2fb5ed6620f214968ae2b3c5e70e6"
OLD_SHA = "1111111111111111111111111111111111111111"


def _review(
    state: str,
    login: str,
    association: str = "MEMBER",
    commit_id: str = HEAD_SHA,
) -> Any:
    review = MagicMock()
    review.state = state
    review.commit_id = commit_id
    review.author_association = association
    review.user = MagicMock()
    review.user.login = login
    return review


def _pr(reviews: list[Any], author: str = "contributor") -> Any:
    pr = MagicMock()
    pr.get_reviews.return_value = reviews
    pr.user = MagicMock()
    pr.user.login = author
    pr.head = MagicMock()
    pr.head.sha = HEAD_SHA
    return pr


def _evaluate(reviews: list[Any], author: str = "contributor"):
    return evaluate_fork_approval(
        _pr(reviews, author), head_sha=HEAD_SHA, author_login=author
    )


class TestApprovalEvaluation:
    """Which reviews authorise a transfer."""

    def test_no_reviews_blocks(self) -> None:
        status = _evaluate([])
        assert status.approved is False
        assert "no maintainer has approved" in status.reason

    @pytest.mark.parametrize("association", ["OWNER", "MEMBER", "COLLABORATOR"])
    def test_trusted_approval_passes(self, association: str) -> None:
        status = _evaluate([_review("APPROVED", "maintainer", association)])
        assert status.approved is True
        assert status.approvers == ["maintainer"]

    @pytest.mark.parametrize(
        "association", ["CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", "NONE"]
    )
    def test_untrusted_approval_ignored(self, association: str) -> None:
        status = _evaluate([_review("APPROVED", "outsider", association)])
        assert status.approved is False

    def test_missing_association_ignored(self) -> None:
        status = _evaluate([_review("APPROVED", "ghost", "")])
        assert status.approved is False


class TestApprovalBindsToHeadSha:
    """The approve-then-force-push bypass."""

    def test_approval_of_older_commit_does_not_count(self) -> None:
        """GitHub keeps approvals across pushes unless told otherwise."""
        status = _evaluate(
            [_review("APPROVED", "maintainer", commit_id=OLD_SHA)]
        )
        assert status.approved is False
        assert status.stale_approvers == ["maintainer"]
        assert "predates the current commit" in status.reason

    def test_reapproval_after_push_counts(self) -> None:
        status = _evaluate(
            [
                _review("APPROVED", "maintainer", commit_id=OLD_SHA),
                _review("APPROVED", "maintainer", commit_id=HEAD_SHA),
            ]
        )
        assert status.approved is True

    def test_sha_comparison_is_case_insensitive(self) -> None:
        status = _evaluate(
            [_review("APPROVED", "maintainer", commit_id=HEAD_SHA.upper())]
        )
        assert status.approved is True


class TestSelfApproval:
    """GitHub forbids it; the tool does not rely on that alone."""

    def test_author_cannot_approve_own_pr(self) -> None:
        """Org membership is weak, so the author is excluded outright."""
        status = _evaluate(
            [_review("APPROVED", "contributor", "MEMBER")],
            author="contributor",
        )
        assert status.approved is False

    def test_author_exclusion_is_case_insensitive(self) -> None:
        status = _evaluate(
            [_review("APPROVED", "Contributor", "MEMBER")],
            author="contributor",
        )
        assert status.approved is False

    def test_other_member_still_counts(self) -> None:
        status = _evaluate(
            [
                _review("APPROVED", "contributor", "MEMBER"),
                _review("APPROVED", "maintainer", "MEMBER"),
            ],
            author="contributor",
        )
        assert status.approved is True
        assert status.approvers == ["maintainer"]


class TestReviewHistoryReduction:
    """The endpoint returns history, not current state."""

    def test_changes_requested_blocks(self) -> None:
        status = _evaluate(
            [
                _review("APPROVED", "one"),
                _review("CHANGES_REQUESTED", "two"),
            ]
        )
        assert status.approved is False
        assert status.blockers == ["two"]

    def test_later_approval_supersedes_changes_requested(self) -> None:
        status = _evaluate(
            [
                _review("CHANGES_REQUESTED", "maintainer"),
                _review("APPROVED", "maintainer"),
            ]
        )
        assert status.approved is True

    def test_later_changes_requested_supersedes_approval(self) -> None:
        status = _evaluate(
            [
                _review("APPROVED", "maintainer"),
                _review("CHANGES_REQUESTED", "maintainer"),
            ]
        )
        assert status.approved is False

    def test_trailing_comment_does_not_erase_approval(self) -> None:
        """COMMENTED expresses no position and must not displace one."""
        status = _evaluate(
            [
                _review("APPROVED", "maintainer"),
                _review("COMMENTED", "maintainer"),
            ]
        )
        assert status.approved is True

    def test_dismissed_approval_does_not_count(self) -> None:
        status = _evaluate(
            [
                _review("APPROVED", "maintainer"),
                _review("DISMISSED", "maintainer"),
            ]
        )
        assert status.approved is False

    def test_pending_review_does_not_count(self) -> None:
        status = _evaluate([_review("PENDING", "maintainer")])
        assert status.approved is False


class TestEvaluationFailsClosed:
    """An unreadable review list is not an approval."""

    def test_api_failure_blocks(self) -> None:
        pr = MagicMock()
        pr.get_reviews.side_effect = RuntimeError("403")

        status = evaluate_fork_approval(
            pr, head_sha=HEAD_SHA, author_login="contributor"
        )

        assert status.approved is False
        assert "could not read" in status.reason


class TestBlockedComment:
    """What the contributor is told."""

    def test_carries_marker_for_idempotent_updates(self) -> None:
        body = render_blocked_comment(
            ApprovalStatus(approved=False, reason="no approval"),
            head_sha=HEAD_SHA,
        )
        assert body.startswith(APPROVAL_MARKER)

    def test_explains_stale_approval_distinctly(self) -> None:
        """Being told 'no approval' when you approved is confusing."""
        body = render_blocked_comment(
            ApprovalStatus(
                approved=False,
                reason="the approval from maintainer predates it",
                stale_approvers=["maintainer"],
            ),
            head_sha=HEAD_SHA,
        )
        assert "Re-approve" in body
        assert HEAD_SHA[:7] in body

    def test_names_the_trust_policy(self) -> None:
        body = render_blocked_comment(
            ApprovalStatus(approved=False, reason="no approval"),
            head_sha=HEAD_SHA,
        )
        assert "MEMBER" in body
        assert "cannot approve their own" in body


def _ctx(
    *,
    head_repo: str = FORK_REPO,
    event_name: str = "pull_request_target",
    pr_number: int | None = 29,
) -> GitHubContext:
    return GitHubContext(
        event_name=event_name,
        event_action="opened",
        event_path=None,
        repository=BASE_REPO,
        repository_owner="opendaylight",
        server_url="https://github.com",
        run_id="1",
        sha=HEAD_SHA,
        base_ref="master",
        head_ref="topic/fix",
        pr_number=pr_number,
        head_repo=head_repo,
    )


class TestForkApprovalGate:
    """The gate's decision at the CLI boundary."""

    def _gate(self, ctx: GitHubContext, pr: Any) -> bool:
        from github2gerrit.cli import _check_fork_approval

        with patch("github2gerrit.cli._post_fork_approval_notice"):
            return _check_fork_approval(pr, ctx)

    def test_same_repo_pr_is_never_gated(self) -> None:
        """A same-repo head branch already implies write access."""
        pr = _pr([])
        assert self._gate(_ctx(head_repo=BASE_REPO), pr) is True

    def test_fork_without_approval_blocked(self) -> None:
        assert self._gate(_ctx(), _pr([])) is False

    def test_fork_with_approval_allowed(self) -> None:
        pr = _pr([_review("APPROVED", "maintainer")])
        assert self._gate(_ctx(), pr) is True

    def test_unresolvable_pr_blocks(self) -> None:
        """Fail closed: unknown provenance is not permission."""
        assert self._gate(_ctx(), None) is False

    def test_unresolvable_pr_on_same_repo_still_passes(self) -> None:
        assert self._gate(_ctx(head_repo=BASE_REPO), None) is True

    def test_unknown_provenance_is_not_gated(self) -> None:
        """is_fork_pr is factual; #384 keeps it False when unknown.

        The gate deliberately follows that rather than blocking every
        pull request whose provenance the event did not carry.
        """
        assert self._gate(_ctx(head_repo=""), _pr([])) is True


class TestReviewEventOperationMode:
    """A review must not create a sibling change."""

    def test_review_event_maps_to_update(self) -> None:
        ctx = _ctx(event_name="pull_request_review")
        assert ctx.get_operation_mode() is PROperationMode.UPDATE

    def test_pull_request_events_unchanged(self) -> None:
        ctx = _ctx(event_name="pull_request_target")
        assert ctx.get_operation_mode() is PROperationMode.CREATE


class TestReviewTriggersCreateMissing:
    """First approval of a fork PR has no change to update."""

    def _should_create(self, event_name: str) -> bool:
        from github2gerrit.core import Orchestrator

        orch = Orchestrator.__new__(Orchestrator)
        inputs = MagicMock()
        inputs.create_missing = False
        gh = _ctx(event_name=event_name)

        with patch("github2gerrit.core.build_client", side_effect=OSError):
            return orch._should_create_missing(inputs, gh)

    def test_review_event_authorises_create(self) -> None:
        assert self._should_create("pull_request_review") is True

    def test_synchronize_event_does_not(self) -> None:
        assert self._should_create("pull_request_target") is False
