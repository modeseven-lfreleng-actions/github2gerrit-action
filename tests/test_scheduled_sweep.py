# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""A scheduled sweep lifts the fork gate without a comment (#421).

The comment doorbell transfers an approved fork pull request once
somebody posts ``@github2gerrit check``. A caller that also subscribes
to ``schedule`` gets the original zero-touch design: a maintainer
approves in the ordinary way and the next sweep notices.

#421 recorded what a correct sweep has to satisfy, and these tests hold
the implementation to each point:

* every transfer is serialised against the per-pull-request runs for
  the same pull request, by running in that pull request's lock;
* the sweep visits only pull requests the gate can block, keyed on the
  same trust rule as ``head_is_trusted``, so unresolved provenance is
  still visited;
* an already-transferred pull request is not submitted again (#419);
* none of it rests on documentation alone.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from workflow_harness import REPO
from workflow_harness import evaluate
from workflow_harness import load_jobs
from workflow_harness import matrix_of
from workflow_harness import render
from workflow_harness import request_of
from workflow_harness import run_enumeration
from workflow_harness import truthy

from github2gerrit.cli import _recheck_has_nothing_to_unblock
from github2gerrit.models import RECHECK_EVENTS
from github2gerrit.models import GitHubContext
from github2gerrit.models import PROperationMode


FORK = "contributor/mdsal"
HEAD = "0b2abdcf7bb2fb5ed6620f214968ae2b3c5e70e6"
OLD = "1111111111111111111111111111111111111111"
SCHEDULE = {"github.event_name": "schedule"}


@pytest.fixture(scope="module")
def jobs() -> dict[str, Any]:
    return load_jobs()


def _node(
    number: int,
    *,
    head_repo: str | None = FORK,
    approved_at: tuple[str, ...] = (HEAD,),
    truncated: bool = False,
) -> dict[str, Any]:
    """A pull request as the GraphQL listing returns it."""
    return {
        "number": number,
        "headRefOid": HEAD,
        "headRepository": (
            None if head_repo is None else {"nameWithOwner": head_repo}
        ),
        "reviews": {
            "totalCount": len(approved_at) + (1 if truncated else 0),
            "nodes": [{"commit": {"oid": sha}} for sha in approved_at],
        },
    }


class TestAScheduleStartsASweep:
    """Routing: a schedule fans out, and nothing else runs for it."""

    def test_the_enumeration_runs(self, jobs: dict[str, Any]) -> None:
        assert truthy(evaluate(jobs["enumerate"]["if"], SCHEDULE))

    def test_the_single_job_does_not(self, jobs: dict[str, Any]) -> None:
        # It has no pull request to act on, and a schedule processing
        # every pull request in one job is the race #421 describes.
        assert not truthy(evaluate(jobs["github2gerrit"]["if"], SCHEDULE))

    @pytest.mark.parametrize(
        ("event", "expected"),
        [("schedule", "true"), ("workflow_dispatch", "false")],
    )
    def test_the_listing_knows_which_sweep_it_is(
        self, jobs: dict[str, Any], event: str, expected: str
    ) -> None:
        env = jobs["enumerate"]["steps"][0]["env"]
        scheduled = render(env["SCHEDULED"], {"github.event_name": event})
        assert scheduled == expected


class TestAScheduledLegTakesItsPullRequestsLock:
    """Requirement one: no transfer runs beside another for its PR."""

    @pytest.mark.parametrize(
        "event",
        [
            {
                "github.event_name": "pull_request_target",
                "github.event.pull_request.number": 29,
            },
            {
                "github.event_name": "issue_comment",
                "github.event.issue.number": 29,
            },
        ],
        ids=["pull_request_target", "issue_comment"],
    )
    def test_it_queues_behind_the_events_for_that_pull_request(
        self, jobs: dict[str, Any], event: dict[str, Any]
    ) -> None:
        # The comment doorbell is the likeliest collision: a maintainer
        # comments while the sweep is already transferring (#421).
        leg = render(
            jobs["github2gerrit-sweep"]["concurrency"]["group"],
            {**SCHEDULE, "matrix.pr": 29},
        )
        run = render(jobs["github2gerrit"]["concurrency"]["group"], event)
        assert leg == run == f"g2g-{REPO}-29"


@pytest.mark.skipif(shutil.which("jq") is None, reason="needs jq")
class TestTheScheduledListing:
    """Requirement two: visit only what the gate could be holding back."""

    def _legs(
        self, jobs: dict[str, Any], tmp_path: Path, nodes: list[Any]
    ) -> tuple[subprocess.CompletedProcess[str], str]:
        result, output, _ = run_enumeration(
            jobs, tmp_path, [nodes], SCHEDULED="true"
        )
        assert result.returncode == 0, result.stderr
        return result, matrix_of(output)

    def test_an_approved_fork_is_visited(
        self, jobs: dict[str, Any], tmp_path: Path
    ) -> None:
        _result, matrix = self._legs(jobs, tmp_path, [_node(29)])
        assert json.loads(matrix) == {"pr": [29]}

    def test_there_is_no_cleanup_leg(
        self, jobs: dict[str, Any], tmp_path: Path
    ) -> None:
        # A schedule is for lifting the gate; the repository-wide
        # cleanup stays with the events and bulk dispatch.
        _result, matrix = self._legs(jobs, tmp_path, [_node(29), _node(30)])
        assert 0 not in json.loads(matrix)["pr"]

    @pytest.mark.parametrize(
        "head_repo", [REPO, REPO.upper()], ids=["same", "case"]
    )
    def test_a_same_repository_head_is_not_visited(
        self, jobs: dict[str, Any], tmp_path: Path, head_repo: str
    ) -> None:
        # The gate never applies there, approval or not, and re-running
        # every automation pull request on every interval is what the
        # straightforward sweep got wrong. The name compares without
        # case, as head_repo_is_trusted does.
        _result, matrix = self._legs(
            jobs, tmp_path, [_node(29, head_repo=head_repo)]
        )
        assert matrix == ""

    def test_unresolved_provenance_is_visited(
        self, jobs: dict[str, Any], tmp_path: Path
    ) -> None:
        # A head whose repository is gone is not known to be in this
        # one, so the gate applies; skipping it would strand it.
        _result, matrix = self._legs(
            jobs, tmp_path, [_node(29, head_repo=None)]
        )
        assert json.loads(matrix) == {"pr": [29]}

    def test_an_unapproved_fork_is_not_visited(
        self, jobs: dict[str, Any], tmp_path: Path
    ) -> None:
        _result, matrix = self._legs(
            jobs, tmp_path, [_node(29, approved_at=())]
        )
        assert matrix == ""

    def test_an_approval_of_an_older_commit_is_not_enough(
        self, jobs: dict[str, Any], tmp_path: Path
    ) -> None:
        # The gate binds an approval to the head it was given for, so
        # this pull request waits for re-approval either way.
        _result, matrix = self._legs(
            jobs, tmp_path, [_node(29, approved_at=(OLD,))]
        )
        assert matrix == ""

    def test_a_truncated_review_list_is_visited(
        self, jobs: dict[str, Any], tmp_path: Path
    ) -> None:
        # The approval covering the head may be among the reviews
        # GitHub left out; the leg's gate reads them all.
        node = _node(29, approved_at=(OLD,), truncated=True)
        _result, matrix = self._legs(jobs, tmp_path, [node])
        assert json.loads(matrix) == {"pr": [29]}

    def test_nothing_to_visit_starts_no_legs(
        self, jobs: dict[str, Any], tmp_path: Path
    ) -> None:
        result, matrix = self._legs(
            jobs,
            tmp_path,
            [_node(29, head_repo=REPO), _node(30, approved_at=())],
        )
        assert matrix == ""
        assert "No approved pull request is awaiting transfer" in result.stdout

    def test_a_mixed_page_keeps_only_candidates(
        self, jobs: dict[str, Any], tmp_path: Path
    ) -> None:
        nodes = [
            _node(31),
            _node(29, head_repo=REPO),
            _node(30, approved_at=()),
            _node(32, head_repo=None),
        ]
        _result, matrix = self._legs(jobs, tmp_path, nodes)
        assert json.loads(matrix) == {"pr": [31, 32]}

    def test_a_bulk_dispatch_still_visits_everything(
        self, jobs: dict[str, Any], tmp_path: Path
    ) -> None:
        # The narrowing is the schedule's alone: a dispatch is somebody
        # asking for every open pull request.
        nodes = [_node(29, head_repo=REPO, approved_at=()), _node(30)]
        result, output, _ = run_enumeration(jobs, tmp_path, [nodes])
        assert result.returncode == 0, result.stderr
        assert json.loads(matrix_of(output)) == {"pr": [29, 30, 0]}

    def test_the_query_asks_for_what_the_filter_reads(
        self, jobs: dict[str, Any], tmp_path: Path
    ) -> None:
        _result, _output, calls = run_enumeration(
            jobs, tmp_path, [[_node(29)]], SCHEDULED="true"
        )
        query = " ".join(request_of(calls[0])["query"].split())
        for field in (
            "headRefOid",
            "headRepository { nameWithOwner }",
            "reviews(states: [APPROVED], last: 100)",
            "totalCount",
            "commit { oid }",
        ):
            assert field in query

    def test_the_largest_matrix_github_allows(
        self, jobs: dict[str, Any], tmp_path: Path
    ) -> None:
        # No cleanup leg, so all 256 jobs are available.
        pages = [[_node(n) for n in range(s, s + 100)] for s in (1, 101)]
        pages.append([_node(n) for n in range(201, 257)])
        result, output, _ = run_enumeration(
            jobs, tmp_path, pages, SCHEDULED="true"
        )
        assert result.returncode == 0, result.stderr
        assert len(json.loads(matrix_of(output))["pr"]) == 256

    def test_more_than_github_allows_fails(
        self, jobs: dict[str, Any], tmp_path: Path
    ) -> None:
        pages = [[_node(n) for n in range(s, s + 100)] for s in (1, 101)]
        pages.append([_node(n) for n in range(201, 258)])
        result, output, _ = run_enumeration(
            jobs, tmp_path, pages, SCHEDULED="true"
        )
        assert result.returncode == 1
        assert "257 pull requests await a scheduled transfer" in result.stdout
        assert "matrix=" not in output


class TestAScheduledRunIsARecheck:
    """The tool treats a scheduled leg as a comment re-check.

    Membership of RECHECK_EVENTS is what gives it the behaviour a
    re-check needs, and the parametrised tests over that set in
    test_fork_approval_gate.py now cover ``schedule`` too: UPDATE mode,
    so an existing change gains a patchset rather than a sibling; the
    create-missing fallback, so the first transfer is not refused; and
    no transfer for a same-repository head.
    """

    def test_a_schedule_is_a_recheck(self) -> None:
        assert "schedule" in RECHECK_EVENTS

    def _ctx(self, head_repo: str) -> GitHubContext:
        return GitHubContext(
            event_name="schedule",
            event_action="",
            event_path=None,
            repository=REPO,
            repository_owner="opendaylight",
            server_url="https://github.com",
            run_id="1",
            sha=HEAD,
            base_ref="",
            head_ref="",
            pr_number=29,
            head_repo=head_repo,
        )

    def test_a_scheduled_leg_updates_rather_than_duplicates(self) -> None:
        assert self._ctx(FORK).get_operation_mode() is PROperationMode.UPDATE

    def test_a_same_repository_head_is_left_alone(self) -> None:
        # Defence in depth behind the listing's own filter.
        assert _recheck_has_nothing_to_unblock(self._ctx(REPO)) is True
        assert _recheck_has_nothing_to_unblock(self._ctx(FORK)) is False


@pytest.fixture(scope="module")
def action_steps() -> dict[str, Any]:
    path = Path(__file__).parent.parent / "action.yaml"
    steps = yaml.safe_load(path.read_text())["runs"]["steps"]
    return {step["name"]: step for step in steps if "name" in step}


class TestTheActionAcceptsAScheduledLeg:
    """A leg reaches the action on ``schedule`` with its PR_NUMBER."""

    VALIDATE = "Validate PR_NUMBER usage"
    NORMALIZE = "Normalize PR_NUMBER"
    EXTRACT = "Extract PR number, validate context"

    @staticmethod
    def _condition(step: dict[str, Any], event: str, pr_number: str) -> bool:
        return truthy(
            evaluate(
                step["if"],
                {
                    "steps.disabled-check.outputs.disabled": "false",
                    "github.event_name": event,
                    "inputs.PR_NUMBER": pr_number,
                },
            )
        )

    def test_a_scheduled_pr_number_is_not_refused(
        self, action_steps: dict[str, Any]
    ) -> None:
        step = action_steps[self.VALIDATE]
        assert not self._condition(step, "schedule", "29")
        # Other events still may not name one.
        assert self._condition(step, "pull_request_target", "29")

    def test_a_scheduled_pr_number_is_normalised(
        self, action_steps: dict[str, Any]
    ) -> None:
        assert self._condition(action_steps[self.NORMALIZE], "schedule", "29")

    def _extract(
        self, action_steps: dict[str, Any], tmp_path: Path, **env: str
    ) -> tuple[subprocess.CompletedProcess[str], str]:
        output = tmp_path / "github_output"
        output.touch()
        result = subprocess.run(
            ["bash", "-c", action_steps[self.EXTRACT]["run"]],
            capture_output=True,
            text=True,
            check=False,
            env={
                **os.environ,
                "GITHUB_EVENT_NAME": "schedule",
                "GITHUB_OUTPUT": str(output),
                "EVENT_PR_NUMBER": "",
                "DISPATCH_PR_NUMBER": "",
                "DISPATCH_SYNC_ALL": "",
                **env,
            },
        )
        return result, output.read_text()

    def test_a_leg_passes_its_pull_request_on(
        self, action_steps: dict[str, Any], tmp_path: Path
    ) -> None:
        result, output = self._extract(
            action_steps, tmp_path, DISPATCH_PR_NUMBER="29"
        )
        assert result.returncode == 0, result.stdout
        assert "pr_number=29" in output
        assert "sync_all" not in output

    @pytest.mark.parametrize(
        "env",
        [{"DISPATCH_SYNC_ALL": "true"}, {}],
        ids=["sweep-everything", "no-pull-request"],
    )
    def test_a_schedule_cannot_sweep_in_one_job(
        self, action_steps: dict[str, Any], tmp_path: Path, env: dict[str, str]
    ) -> None:
        # A composite action called straight from a schedule has no way
        # to fan out, and one job over every pull request is the race.
        result, output = self._extract(action_steps, tmp_path, **env)
        assert result.returncode == 2
        assert "needs PR_NUMBER naming one pull request" in result.stdout
        assert "pr_number=" not in output
