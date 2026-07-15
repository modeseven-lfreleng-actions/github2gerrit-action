# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for final-state (merged/abandoned) Gerrit change reconciliation.

Covers the regression where a re-run against a PR whose Gerrit change
had already merged attempted to push a new patchset, which Gerrit
rejected with "change ... closed" and the tool then misreported as an
SSH/connection failure.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from github2gerrit.core import GerritInfo
from github2gerrit.core import Orchestrator
from github2gerrit.error_codes import ExitCode
from github2gerrit.error_codes import map_orchestrator_error_to_exit_code
from github2gerrit.gitutils import CommandError
from github2gerrit.models import GitHubContext


def _gh_context(pr_number: int | None = 33) -> GitHubContext:
    return GitHubContext(
        event_name="pull_request_target",
        event_action="synchronize",
        event_path=None,
        repository="onap/policy-opa-pdp",
        repository_owner="onap",
        server_url="https://github.com",
        run_id="123",
        sha="abc123",
        base_ref="master",
        head_ref="dependabot/github_actions/foo",
        pr_number=pr_number,
    )


def _gerrit_info() -> GerritInfo:
    return GerritInfo(
        host="gerrit.example.org",
        port=29418,
        project="policy/opa-pdp",
    )


class TestClosedChangePushAnalysis:
    """Push failure analysis for closed-change rejections."""

    def setup_method(self) -> None:
        self.workspace = Path(tempfile.mkdtemp())
        self.orchestrator = Orchestrator(workspace=self.workspace)

    def test_closed_change_rejection_message(self) -> None:
        output = (
            "remote: error: change is closed\n"
            " ! [remote rejected] HEAD -> refs/for/master "
            "(change https://gerrit.example.org/r/c/policy/opa-pdp/+/146080 "
            "closed)\n"
            "error: failed to push some refs\n"
        )
        exc = CommandError(
            "Command failed",
            cmd=["git", "review"],
            returncode=1,
            stdout="",
            stderr=output,
        )
        result = self.orchestrator._analyze_gerrit_push_failure(exc)

        assert "closed (merged or abandoned)" in result
        assert "146080" in result
        # Must not be reported as a generic rejection
        assert not result.startswith("Gerrit rejected the push:")


class TestClosedChangeExitCode:
    """Exit code mapping for closed-change push failures."""

    def test_closed_change_maps_to_final_state_exit_code(self) -> None:
        msg = (
            "Failed to push changes to Gerrit with git-review: "
            "Gerrit change is closed (merged or abandoned) and cannot "
            "accept new patchsets: change "
            "https://gerrit.example.org/r/c/policy/opa-pdp/+/146080 closed"
        )
        assert (
            map_orchestrator_error_to_exit_code(msg)
            == ExitCode.GERRIT_CHANGE_ALREADY_FINAL
        )

    def test_raw_closed_rejection_maps_to_final_state_exit_code(self) -> None:
        msg = (
            "Gerrit rejected the push: change "
            "https://gerrit.example.org/r/c/x/+/1 closed"
        )
        assert (
            map_orchestrator_error_to_exit_code(msg)
            == ExitCode.GERRIT_CHANGE_ALREADY_FINAL
        )

    def test_ssh_failure_still_maps_to_connection_error(self) -> None:
        msg = (
            "Failed to push changes to Gerrit with git-review: "
            "SSH public key authentication failed."
        )
        assert (
            map_orchestrator_error_to_exit_code(msg)
            == ExitCode.GERRIT_CONNECTION_ERROR
        )


class TestReconcileFinalStateChanges:
    """Pre-push reconciliation of PRs whose Gerrit changes are final."""

    def setup_method(self) -> None:
        self.workspace = Path(tempfile.mkdtemp())
        self.orchestrator = Orchestrator(workspace=self.workspace)
        self.gh = _gh_context()
        self.gerrit = _gerrit_info()

    def _run(self, change_ids: list[str]) -> Any:
        return self.orchestrator._reconcile_final_state_changes(
            gh=self.gh,
            gerrit=self.gerrit,
            change_ids=change_ids,
        )

    def test_no_change_ids_proceeds(self) -> None:
        assert self._run([]) is None

    def test_open_change_proceeds(self) -> None:
        with mock.patch.object(
            self.orchestrator,
            "_lookup_change_state",
            return_value={
                "status": "NEW",
                "number": "146080",
                "current_revision": "deadbeef",
            },
        ):
            assert self._run(["I" + "a" * 40]) is None

    def test_unknown_state_fails_open(self) -> None:
        with mock.patch.object(
            self.orchestrator,
            "_lookup_change_state",
            return_value=None,
        ):
            assert self._run(["I" + "a" * 40]) is None

    def test_merged_change_closes_pr_and_stops(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLOSE_MERGED_PRS", "true")
        with (
            mock.patch.object(
                self.orchestrator,
                "_lookup_change_state",
                return_value={
                    "status": "MERGED",
                    "number": "146080",
                    "current_revision": "deadbeef",
                },
            ),
            mock.patch(
                "github2gerrit.gerrit_pr_closer.close_pr_with_status",
                return_value=True,
            ) as mock_close,
        ):
            result = self._run(["I" + "a" * 40])

        assert result is not None
        assert result.change_numbers == ["146080"]
        assert result.commit_shas == ["deadbeef"]
        assert len(result.change_urls) == 1
        assert "146080" in result.change_urls[0]

        mock_close.assert_called_once()
        kwargs = mock_close.call_args.kwargs
        assert kwargs["pr_url"] == (
            "https://github.com/onap/policy-opa-pdp/pull/33"
        )
        assert kwargs["gerrit_status"] == "MERGED"
        assert kwargs["close_merged_prs"] is True

    def test_abandoned_change_stops_with_abandoned_status(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLOSE_MERGED_PRS", "true")
        with (
            mock.patch.object(
                self.orchestrator,
                "_lookup_change_state",
                return_value={
                    "status": "ABANDONED",
                    "number": "146081",
                    "current_revision": "",
                },
            ),
            mock.patch(
                "github2gerrit.gerrit_pr_closer.close_pr_with_status",
                return_value=True,
            ) as mock_close,
        ):
            result = self._run(["I" + "b" * 40])

        assert result is not None
        assert mock_close.call_args.kwargs["gerrit_status"] == "ABANDONED"

    def test_mixed_states_proceeds_with_warning(self) -> None:
        states = iter(
            [
                {
                    "status": "MERGED",
                    "number": "1",
                    "current_revision": "aaa",
                },
                {
                    "status": "NEW",
                    "number": "2",
                    "current_revision": "bbb",
                },
            ]
        )
        with mock.patch.object(
            self.orchestrator,
            "_lookup_change_state",
            side_effect=lambda *_a, **_k: next(states),
        ):
            assert self._run(["I" + "a" * 40, "I" + "b" * 40]) is None

    def test_close_failure_still_stops_pipeline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLOSE_MERGED_PRS", "true")
        with (
            mock.patch.object(
                self.orchestrator,
                "_lookup_change_state",
                return_value={
                    "status": "MERGED",
                    "number": "146080",
                    "current_revision": "deadbeef",
                },
            ),
            mock.patch(
                "github2gerrit.gerrit_pr_closer.close_pr_with_status",
                side_effect=RuntimeError("GitHub API error"),
            ),
        ):
            result = self._run(["I" + "a" * 40])

        # Even when the PR closure fails, we must not attempt the
        # doomed push to a closed change.
        assert result is not None
