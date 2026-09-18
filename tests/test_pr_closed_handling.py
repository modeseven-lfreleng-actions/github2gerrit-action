# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Closing a pull request, and the runs that arrive alongside it.

Regression coverage for #441. The close handler did run; it queried
the wrong Gerrit project and said nothing about finding no change. A
sibling event landing on the already-closed pull request then failed
with exit 8, and that failure was what the report saw.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from typing import cast
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
import typer

from github2gerrit.cli import _abandon_change_for_closed_pr
from github2gerrit.cli import _handle_pr_closed
from github2gerrit.cli import _stop_for_pr_state
from github2gerrit.error_codes import ExitCode
from github2gerrit.models import GitHubContext
from github2gerrit.models import Inputs


def _ctx(
    event_name: str, action: str = "", pr_number: int = 19
) -> GitHubContext:
    return GitHubContext(
        event_name=event_name,
        event_action=action,
        event_path=None,
        repository="onap/multicloud-openstack",
        repository_owner="onap",
        server_url="https://github.com",
        run_id="1",
        sha="deadbeef",
        base_ref="master",
        head_ref="topic",
        pr_number=pr_number,
        head_repo="onap/multicloud-openstack",
    )


def _inputs(*, server: str = "gerrit.onap.org", project: str) -> Inputs:
    # The handler reads only these; the cast records the partial stand-in.
    return cast(
        Inputs,
        SimpleNamespace(
            gerrit_server=server, gerrit_project=project, dry_run=False
        ),
    )


class TestStopForPrState:
    """A closed pull request is only an error when somebody asked for it."""

    @pytest.mark.parametrize(
        "event_name", ["pull_request_target", "pull_request"]
    )
    def test_pull_request_event_stops_cleanly(
        self, event_name: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        # GitHub delivers several events around a close. Dependabot
        # edited the body of onap/multicloud-openstack#19 two seconds
        # before closing it, and the `edited` run landed after the
        # `closed` one had already finished. It has nothing to do.
        with (
            caplog.at_level(logging.INFO, logger="github2gerrit"),
            pytest.raises(typer.Exit) as exc,
        ):
            _stop_for_pr_state(_ctx(event_name, "edited"), "closed")
        assert exc.value.exit_code == int(ExitCode.SUCCESS)
        assert "nothing to do" in caplog.text
        assert "'edited'" in caplog.text

    @pytest.mark.parametrize("event_name", ["workflow_dispatch", ""])
    def test_a_deliberate_request_still_errors(self, event_name: str) -> None:
        # Somebody named this pull request; telling them it cannot be
        # processed is the useful answer.
        with pytest.raises(typer.Exit) as exc:
            _stop_for_pr_state(_ctx(event_name), "closed")
        assert exc.value.exit_code == int(ExitCode.PR_STATE_ERROR)

    def test_clean_stop_releases_the_progress_display(self) -> None:
        # The caller's own stop() is downstream of the raise. An
        # in-process caller catching the exit would otherwise be left
        # with a live Rich context.
        tracker = MagicMock()
        with pytest.raises(typer.Exit):
            _stop_for_pr_state(
                _ctx("pull_request", "edited"), "closed", tracker
            )
        tracker.stop.assert_called_once_with()


class TestCloseHandlerDiagnostics:
    """Finding nothing must be audible, and must name the project."""

    def test_no_match_is_logged_with_the_project_queried(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # At debug level this outcome hid 239 stranded changes for six
        # months. Naming the project is what lets a reader see that
        # multicloud-openstack is not multicloud/openstack.
        with (
            patch(
                "github2gerrit.cli.abandon_gerrit_change_for_closed_pr",
                return_value=None,
            ),
            caplog.at_level(logging.INFO, logger="github2gerrit"),
        ):
            _abandon_change_for_closed_pr(
                _inputs(project="multicloud-openstack"),
                _ctx("pull_request_target", "closed"),
            )
        assert "No open Gerrit change found" in caplog.text
        assert "'multicloud-openstack'" in caplog.text
        assert "gerrit.onap.org" in caplog.text

    def test_a_failed_lookup_is_not_reported_as_no_change(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Authentication and network failures used to come back as
        # None, and so as "nothing to abandon". The failure may have
        # landed on either side of the abandon, so the report claims
        # neither state; it says the state is unconfirmed.
        with (
            patch(
                "github2gerrit.cli.abandon_gerrit_change_for_closed_pr",
                side_effect=RuntimeError("HTTP 401"),
            ),
            caplog.at_level(logging.INFO, logger="github2gerrit"),
        ):
            _abandon_change_for_closed_pr(
                _inputs(project="multicloud/openstack"),
                _ctx("pull_request_target", "closed"),
            )
        assert "No open Gerrit change" not in caplog.text
        warnings = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.WARNING
        ]
        assert len(warnings) == 1
        assert "HTTP 401" in warnings[0]
        assert "could not be confirmed" in warnings[0]
        assert "still open" not in warnings[0]
        assert "'multicloud/openstack'" in warnings[0]

    def test_missing_repository_is_a_warning_not_a_silent_skip(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # The repository builds the pull request URL the trailer match
        # needs. Falling through without it left no trace either.
        gh = replace(_ctx("pull_request_target", "closed"), repository="")
        with (
            patch("github2gerrit.cli._run_gerrit_cleanup_tasks"),
            patch("github2gerrit.cli._abandon_change_for_closed_pr") as abandon,
            caplog.at_level(logging.WARNING, logger="github2gerrit"),
        ):
            _handle_pr_closed(
                _inputs(project="multicloud/openstack"), gh, no_gerrit=False
            )
        abandon.assert_not_called()
        assert "Cannot look for a Gerrit change" in caplog.text
        assert "repository=''" in caplog.text

    def test_missing_project_is_a_warning_not_a_silent_skip(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Skipping silently is indistinguishable from a cleanup that
        # found nothing, and the change stays open with no trace of why.
        with (
            patch("github2gerrit.cli._run_gerrit_cleanup_tasks"),
            patch("github2gerrit.cli._abandon_change_for_closed_pr") as abandon,
            caplog.at_level(logging.WARNING, logger="github2gerrit"),
        ):
            _handle_pr_closed(
                _inputs(project=""),
                _ctx("pull_request_target", "closed"),
                no_gerrit=False,
            )
        abandon.assert_not_called()
        assert "Cannot look for a Gerrit change" in caplog.text
        assert "GERRIT_PROJECT" in caplog.text

    def test_no_gerrit_mode_skips_quietly(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Keyless by design; a warning here would be noise on every run.
        with (
            patch("github2gerrit.cli._run_gerrit_cleanup_tasks"),
            patch("github2gerrit.cli._abandon_change_for_closed_pr") as abandon,
            caplog.at_level(logging.WARNING, logger="github2gerrit"),
        ):
            _handle_pr_closed(
                _inputs(project=""),
                _ctx("pull_request_target", "closed"),
                no_gerrit=True,
            )
        abandon.assert_not_called()
        assert "Cannot look for" not in caplog.text

    def test_resolved_project_reaches_the_query(self) -> None:
        # The whole of #441 in one assertion: the handler must query
        # the project derivation resolved, and derivation now resolves
        # it from .gitreview.
        seen: dict[str, Any] = {}

        def _capture(**kwargs: Any) -> None:
            seen.update(kwargs)
            return None

        with (
            patch("github2gerrit.cli._run_gerrit_cleanup_tasks"),
            patch(
                "github2gerrit.cli.abandon_gerrit_change_for_closed_pr",
                side_effect=_capture,
            ),
        ):
            _handle_pr_closed(
                _inputs(project="multicloud/openstack"),
                _ctx("pull_request_target", "closed"),
                no_gerrit=False,
            )
        assert seen["gerrit_project"] == "multicloud/openstack"
        assert seen["pr_number"] == 19

    def test_bulk_sweep_receives_the_resolved_project_too(self) -> None:
        # The CLEANUP_GERRIT sweep shared the defect: it is why the
        # stranded changes accumulated instead of being caught later.
        seen: dict[str, Any] = {}

        def _capture(**kwargs: Any) -> None:
            seen.update(kwargs)

        with (
            patch("github2gerrit.cli._abandon_change_for_closed_pr"),
            patch("github2gerrit.cli.cleanup_abandoned_prs_bulk"),
            patch(
                "github2gerrit.cli.cleanup_closed_github_prs",
                side_effect=_capture,
            ),
            patch("github2gerrit.cli.FORCE_GERRIT_CLEANUP", True),
            patch("github2gerrit.cli.FORCE_ABANDONED_CLEANUP", False),
        ):
            _handle_pr_closed(
                _inputs(project="multicloud/openstack"),
                _ctx("pull_request_target", "closed"),
                no_gerrit=False,
            )
        assert seen["gerrit_project"] == "multicloud/openstack"
