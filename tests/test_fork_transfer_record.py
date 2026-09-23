# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""The transfer record on the fork-approval notice (#419).

A sweep revisits every open pull request, including fork pull requests
that were approved and transferred already. ``ALLOW_DUPLICATES``
defaults to true, so nothing downstream stops the re-submission. Once a
transfer succeeds the tool records the head it transferred on the
approval notice, and a sweep that finds the current head recorded
passes the pull request over.

What these tests pin down:

* the record is written only after a real transfer succeeds,
* a sweep skips only when the record names the current head,
* anything short of that means "process it",
* only sweeps read the record; a request naming the pull request does
  not.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from github2gerrit.cli import _handle_single_pr
from github2gerrit.cli import _process_bulk_pr
from github2gerrit.cli import _record_fork_transfer
from github2gerrit.cli import _sweep_can_skip
from github2gerrit.core import Orchestrator
from github2gerrit.core import SubmissionResult
from github2gerrit.models import GitHubContext
from github2gerrit.models import PROperationMode
from github2gerrit.pr_approval import APPROVAL_MARKER
from github2gerrit.pr_approval import ApprovalStatus
from github2gerrit.pr_approval import recorded_transfer
from github2gerrit.pr_approval import render_blocked_comment
from github2gerrit.pr_approval import render_cleared_comment
from github2gerrit.pr_approval import render_transferred_comment


BASE_REPO = "opendaylight/mdsal"
FORK_REPO = "contributor/mdsal"
HEAD_SHA = "0b2abdcf7bb2fb5ed6620f214968ae2b3c5e70e6"
OLD_SHA = "1111111111111111111111111111111111111111"


def _ctx(*, head_repo: str = FORK_REPO) -> GitHubContext:
    return GitHubContext(
        event_name="workflow_dispatch",
        event_action="",
        event_path=None,
        repository=BASE_REPO,
        repository_owner="opendaylight",
        server_url="https://github.com",
        run_id="1",
        sha=HEAD_SHA,
        base_ref="master",
        head_ref="topic/fix",
        pr_number=29,
        head_repo=head_repo,
    )


def _comment(body: str, *, editable: bool = True) -> Any:
    comment = MagicMock()
    comment.body = body
    if not editable:
        comment.edit.side_effect = RuntimeError("403 Forbidden")
    return comment


def _pr(comments: list[Any], *, head_sha: str = HEAD_SHA) -> Any:
    issue = MagicMock()
    issue.get_comments.return_value = comments
    pr = MagicMock()
    pr.number = 29
    pr.as_issue.return_value = issue
    pr.head = MagicMock()
    pr.head.sha = head_sha
    return pr


def _transferred(sha: str = HEAD_SHA) -> str:
    return render_transferred_comment(head_sha=sha)


class TestTheRecordItself:
    """Rendering and reading the record."""

    def test_the_notice_is_still_found_as_ours(self) -> None:
        # Existing notices are located by the v1 marker; the record is
        # a line of its own so they keep being edited in place.
        assert _transferred().startswith(APPROVAL_MARKER)

    def test_the_record_reads_back_as_the_head(self) -> None:
        assert recorded_transfer(_transferred()) == HEAD_SHA

    def test_the_record_is_case_insensitive(self) -> None:
        assert recorded_transfer(_transferred(HEAD_SHA.upper())) == HEAD_SHA

    def test_the_notice_says_what_happened(self) -> None:
        body = _transferred()
        assert "transferred to Gerrit" in body
        assert HEAD_SHA[:7] in body
        assert "fresh approval" in body

    @pytest.mark.parametrize(
        "body",
        [
            render_blocked_comment(
                ApprovalStatus(approved=False, reason="no approval"),
                head_sha=HEAD_SHA,
            ),
            render_cleared_comment(
                ApprovalStatus(approved=True, reason="approved by maintainer"),
                head_sha=HEAD_SHA,
            ),
        ],
        ids=["blocked", "cleared"],
    )
    def test_no_other_notice_records_a_transfer(self, body: str) -> None:
        # The cleared notice is written *before* the transfer, so it
        # must not claim one: a transfer that then failed would be
        # skipped by every later sweep.
        assert recorded_transfer(body) == ""

    def test_an_abbreviated_sha_is_not_a_record(self) -> None:
        # An abbreviation could name more than one commit.
        forged = (
            f"<!-- github2gerrit:fork-approval transferred={HEAD_SHA[:7]} -->"
        )
        assert recorded_transfer(forged) == ""

    def test_an_empty_body_records_nothing(self) -> None:
        assert recorded_transfer("") == ""


class TestSweepSkipsOnlyATransferredHead:
    """A sweep passes over a head it has already transferred, no more."""

    def test_a_recorded_head_is_skipped(self) -> None:
        assert _sweep_can_skip(_pr([_comment(_transferred())]), _ctx()) is True

    def test_a_moved_head_is_processed(self) -> None:
        # The contributor pushed since: the gate has a new commit to
        # judge, and the sweep must bring it before the gate.
        pr = _pr([_comment(_transferred(OLD_SHA))])
        assert _sweep_can_skip(pr, _ctx()) is False

    def test_a_notice_without_a_record_is_processed(self) -> None:
        body = render_cleared_comment(
            ApprovalStatus(approved=True, reason="approved by maintainer"),
            head_sha=HEAD_SHA,
        )
        assert _sweep_can_skip(_pr([_comment(body)]), _ctx()) is False

    def test_no_notice_is_processed(self) -> None:
        pr = _pr([_comment("unrelated chatter")])
        assert _sweep_can_skip(pr, _ctx()) is False

    def test_unreadable_comments_are_processed(self) -> None:
        # A record that cannot be read must degrade to today's
        # behaviour, never to stranding the pull request.
        pr = _pr([])
        pr.as_issue.side_effect = RuntimeError("403")
        assert _sweep_can_skip(pr, _ctx()) is False

    def test_an_unknown_head_is_processed(self) -> None:
        pr = _pr([_comment(_transferred())], head_sha="")
        assert _sweep_can_skip(pr, _ctx()) is False

    def test_no_pull_request_is_processed(self) -> None:
        assert _sweep_can_skip(None, _ctx()) is False

    def test_the_newest_notice_decides(self) -> None:
        # A newer notice without the record hides an older one with it.
        # That errs towards processing, which is the harmless way.
        older = _comment(_transferred())
        newer = _comment(f"{APPROVAL_MARKER}\nsomething else")
        assert _sweep_can_skip(_pr([older, newer]), _ctx()) is False

    def test_a_forged_record_only_delays_the_sweep(self) -> None:
        # Anyone may paste the record, and the reader does not check
        # authorship. That is acceptable only because skipping
        # transfers nothing and the explicit routes ignore the record
        # (see TestExplicitRunsIgnoreTheRecord).
        forged = _comment(_transferred(), editable=False)
        assert _sweep_can_skip(_pr([forged]), _ctx()) is True

    def test_a_same_repository_head_is_never_skipped(self) -> None:
        # The gate never applies there, so the tool never writes a
        # record there, and a planted one must not keep the pull
        # request out of a sweep. The comments are not even read.
        pr = _pr([_comment(_transferred())])
        assert _sweep_can_skip(pr, _ctx(head_repo=BASE_REPO)) is False
        pr.as_issue.assert_not_called()

    def test_unresolved_provenance_is_still_read(self) -> None:
        # The gate applies to a head of unknown origin, so its record
        # is as meaningful as a fork's.
        pr = _pr([_comment(_transferred())])
        assert _sweep_can_skip(pr, _ctx(head_repo="")) is True


class TestTheRecordIsWrittenAfterATransfer:
    """When, and when not, the notice records a transfer."""

    def _record(self, pr: Any, approved_sha: str, *, pushed: bool) -> None:
        with patch("github2gerrit.cli.env_bool", return_value=False):
            _record_fork_transfer(pr, approved_sha, pushed=pushed)

    def test_the_notice_records_the_approved_head(self) -> None:
        notice = _comment(f"{APPROVAL_MARKER}\n### Approved")
        self._record(_pr([notice]), HEAD_SHA, pushed=True)
        notice.edit.assert_called_once()
        assert recorded_transfer(notice.edit.call_args[0][0]) == HEAD_SHA

    def test_a_run_that_pushed_nothing_records_nothing(self) -> None:
        # A dry run, or a pull request reconciled against a change
        # already merged or abandoned, succeeds without reaching
        # Gerrit, so a sweep must still visit.
        notice = _comment(f"{APPROVAL_MARKER}\n### Approved")
        self._record(_pr([notice]), HEAD_SHA, pushed=False)
        notice.edit.assert_not_called()

    def test_an_ungated_pull_request_records_nothing(self) -> None:
        pr = _pr([_comment(f"{APPROVAL_MARKER}\n### Approved")])
        self._record(pr, "", pushed=True)
        pr.as_issue.assert_not_called()

    def test_no_notice_is_created_to_hold_it(self) -> None:
        # A pull request approved before its first run was never
        # blocked, has no notice, and gets no new comment just to carry
        # a record; sweeps then visit it as before.
        other = _comment("unrelated chatter")
        with patch("github2gerrit.cli.create_pr_comment") as created:
            self._record(_pr([other]), HEAD_SHA, pushed=True)
        other.edit.assert_not_called()
        assert created.called is False

    def test_a_comment_failure_does_not_raise(self) -> None:
        pr = _pr([])
        pr.as_issue.side_effect = RuntimeError("boom")
        self._record(pr, HEAD_SHA, pushed=True)


def _submission(*, pushed: bool) -> SubmissionResult:
    return SubmissionResult(
        change_urls=[], change_numbers=[], commit_shas=[], pushed=pushed
    )


class TestOnlyAPushCountsAsATransfer:
    """The orchestrator says whether it pushed; nothing else may guess.

    Several paths end in success without pushing. Taking any of them
    for a transfer would have every later sweep skip a head that never
    reached Gerrit, and on an open pull request that means for good.
    """

    def _orchestrator(self) -> Orchestrator:
        return Orchestrator(workspace=MagicMock())

    def test_a_result_has_not_pushed_unless_told(self) -> None:
        assert (
            SubmissionResult(
                change_urls=[], change_numbers=[], commit_shas=[]
            ).pushed
            is False
        )

    def test_the_push_path_reports_a_push(self) -> None:
        orch = self._orchestrator()
        stubs = {
            name: MagicMock()
            for name in (
                "_push_to_gerrit",
                "_verify_and_sync_after_push",
                "_add_backref_comment_in_gerrit",
                "_comment_on_pull_request",
                "_validate_committed_files",
                "_post_push_supersession_sweep",
                "_close_pull_request_if_required",
                "_cleanup_ssh",
                "_resolve_target_branch",
                "_resolve_reviewers",
            )
        }
        stubs["_query_gerrit_for_results"] = MagicMock(
            return_value=_submission(pushed=False)
        )
        with patch.multiple(orch, **stubs):
            result = orch._push_and_finalize(
                inputs=MagicMock(),
                gh=_ctx(),
                gerrit=MagicMock(),
                repo_names=MagicMock(),
                prep=MagicMock(),
                operation_mode="create",
            )
        assert result.pushed is True

    def test_reconciling_a_merged_change_is_not_a_push(self) -> None:
        # The case Copilot raised on #454: the pull request's changes
        # are already merged or abandoned, the run acts on GitHub
        # instead of pushing, and still reports success.
        orch = self._orchestrator()
        with patch.multiple(
            orch,
            _collect_change_states=MagicMock(
                return_value=[("I1", {"status": "MERGED"})]
            ),
            _collect_final_change_refs=MagicMock(
                return_value=(["https://g/c/1"], ["1"], ["abc"])
            ),
            _reconcile_pr_for_final_changes=MagicMock(),
        ):
            result = orch._reconcile_final_state_changes(
                gh=_ctx(), gerrit=MagicMock(), change_ids=["I1"]
            )
        assert result is not None
        assert result.pushed is False

    def test_a_dry_run_is_not_a_push(self) -> None:
        orch = self._orchestrator()
        with patch.object(orch, "_dry_run_preflight"):
            result = orch._run_dry_run(
                gerrit=MagicMock(),
                inputs=MagicMock(),
                gh=_ctx(),
                repo=MagicMock(),
            )
        assert result.pushed is False


def _inputs(*, dry_run: bool = False) -> Any:
    data = MagicMock()
    data.dry_run = dry_run
    return data


class TestTheBulkSweepConsultsTheRecord:
    """The in-tool bulk sweep (``PR_NUMBER=0``) honours the record."""

    def _process(
        self, pr: Any, *, outcome: str = "success", pushed: bool = True
    ) -> tuple[str, MagicMock, MagicMock]:
        result = (outcome, _submission(pushed=pushed), None)
        with (
            patch("github2gerrit.cli._check_automation_only"),
            patch("github2gerrit.cli.env_bool", return_value=False),
            patch(
                "github2gerrit.cli._check_fork_approval",
                return_value=(True, HEAD_SHA),
            ) as gate,
            patch(
                "github2gerrit.cli._check_bulk_pr_duplicates",
                return_value=None,
            ),
            patch(
                "github2gerrit.cli._submit_bulk_pr", return_value=result
            ) as submit,
        ):
            status, _result, _exc = _process_bulk_pr(
                (pr, _ctx()), _inputs(), _ctx(), MagicMock()
            )
        return status, gate, submit

    def test_a_transferred_head_is_skipped_before_the_gate(self) -> None:
        pr = _pr([_comment(_transferred())])
        status, gate, submit = self._process(pr)
        assert status == "skipped"
        gate.assert_not_called()
        submit.assert_not_called()

    def test_a_moved_head_goes_through_the_gate(self) -> None:
        notice = _comment(_transferred(OLD_SHA))
        status, gate, submit = self._process(_pr([notice]))
        assert status == "success"
        gate.assert_called_once()
        submit.assert_called_once()

    def test_a_successful_transfer_is_recorded(self) -> None:
        notice = _comment(f"{APPROVAL_MARKER}\n### Approved")
        self._process(_pr([notice]))
        assert recorded_transfer(notice.edit.call_args[0][0]) == HEAD_SHA

    def test_a_failed_transfer_is_not_recorded(self) -> None:
        notice = _comment(f"{APPROVAL_MARKER}\n### Approved")
        status, _gate, _submit = self._process(_pr([notice]), outcome="failed")
        assert status == "failed"
        notice.edit.assert_not_called()

    def test_a_success_without_a_push_is_not_recorded(self) -> None:
        notice = _comment(f"{APPROVAL_MARKER}\n### Approved")
        status, _gate, _submit = self._process(_pr([notice]), pushed=False)
        assert status == "success"
        notice.edit.assert_not_called()


class TestExplicitRunsIgnoreTheRecord:
    """A run naming the pull request transfers whatever the record says.

    A dispatch for one pull request, a push and an ``@github2gerrit
    check`` comment all reach the single-PR pipeline, where somebody has
    asked for this pull request. A record there must change nothing, or
    a forged one could block a deliberate request.
    """

    def _run(
        self,
        pr: Any,
        monkeypatch: pytest.MonkeyPatch,
        *,
        pushed: bool = True,
    ) -> dict[str, Any]:
        monkeypatch.setenv("G2G_SHOW_PROGRESS", "false")
        monkeypatch.delenv("CI_TESTING", raising=False)
        monkeypatch.delenv("G2G_SWEEP_LEG", raising=False)
        ctx = _ctx()
        with (
            patch(
                "github2gerrit.cli._augment_pr_refs_if_needed", return_value=ctx
            ),
            patch(
                "github2gerrit.cli._extract_and_display_pr_info",
                return_value=pr,
            ),
            patch("github2gerrit.cli._recover_pr_metadata", return_value=ctx),
            patch(
                "github2gerrit.cli._check_fork_approval",
                return_value=(True, HEAD_SHA),
            ) as gate,
            patch("github2gerrit.cli._check_single_pr_duplicates"),
            patch(
                "github2gerrit.cli._process_single",
                return_value=(True, _submission(pushed=pushed)),
            ) as pipeline,
            patch("github2gerrit.cli._run_gerrit_cleanup_tasks"),
            patch("github2gerrit.cli.log_api_metrics_summary"),
        ):
            _handle_single_pr(
                _inputs(), ctx, PROperationMode.UNKNOWN, no_gerrit=False
            )
        return {"gate": gate, "pipeline": pipeline}

    def test_a_recorded_head_is_still_transferred(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        notice = _comment(_transferred())
        calls = self._run(_pr([notice]), monkeypatch)
        calls["gate"].assert_called_once()
        calls["pipeline"].assert_called_once()

    def test_the_transfer_is_recorded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        notice = _comment(f"{APPROVAL_MARKER}\n### Approved")
        self._run(_pr([notice]), monkeypatch)
        assert recorded_transfer(notice.edit.call_args[0][0]) == HEAD_SHA

    def test_a_success_without_a_push_is_not_recorded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        notice = _comment(f"{APPROVAL_MARKER}\n### Approved")
        self._run(_pr([notice]), monkeypatch, pushed=False)
        notice.edit.assert_not_called()
