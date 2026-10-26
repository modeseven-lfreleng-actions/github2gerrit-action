# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""The abandoned-PR sweep judges a pull request by its own change.

The sweep closes open pull requests whose Gerrit change was abandoned.
It took the first Gerrit URL in a pull request's comments and closed
the pull request whenever that change was abandoned, wherever it lived.
A change pushed to the wrong branch, abandoned there and replaced on the
right one, then closed the pull request it was replaced for (#502).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from github2gerrit.gerrit_pr_closer import cleanup_abandoned_prs_bulk


STABLE = "stable/vanadium"
HOST = "gerrit.example.org"
_PENDING = pytest.mark.xfail(
    strict=True, reason="sweep ignores the change's branch (#502)"
)


def _url(number: int) -> str:
    return f"https://{HOST}/c/project/+/{number}"


def _raised(number: int, verb: str = "raised") -> str:
    return f"Change {verb} in Gerrit by GitHub2Gerrit: {_url(number)}"


class _Gerrit:
    """Serve ``GET /changes/<number>`` from a status and branch table."""

    def __init__(self, changes: dict[int, tuple[str, str]]) -> None:
        self.changes = changes

    def get(self, path: str) -> dict[str, Any]:
        number = int(path.rstrip("/").rsplit("/", 1)[-1])
        status, branch = self.changes[number]
        return {"_number": number, "status": status, "branch": branch}


def _pull_request(base_ref: str, *comments: str) -> MagicMock:
    pr = MagicMock()
    pr.number = 39
    pr.base.ref = base_ref
    pr.as_issue.return_value.get_comments.return_value = [
        MagicMock(body=body) for body in comments
    ]
    return pr


def _sweep(
    monkeypatch: pytest.MonkeyPatch,
    pr: MagicMock,
    changes: dict[int, tuple[str, str]],
) -> MagicMock:
    """Run the sweep over one pull request; return the closer mock."""
    closer = MagicMock(return_value=True)
    module = "github2gerrit.gerrit_pr_closer"
    monkeypatch.setattr(f"{module}.build_client", MagicMock())
    monkeypatch.setattr(f"{module}.iter_open_pulls", lambda _repo: [pr])
    monkeypatch.setattr(
        f"{module}.build_client_for_host",
        lambda _host, **_kwargs: _Gerrit(changes),
    )
    monkeypatch.setattr(f"{module}.close_pr_with_status", closer)
    cleanup_abandoned_prs_bulk("owner", "repo")
    return closer


@_PENDING
def test_abandoned_change_on_another_branch_leaves_the_pr_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pr = _pull_request(STABLE, _raised(101))
    closer = _sweep(monkeypatch, pr, {101: ("ABANDONED", "master")})
    closer.assert_not_called()


@_PENDING
def test_a_later_change_supersedes_the_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pr = _pull_request(STABLE, _raised(101), _raised(202, "updated"))
    closer = _sweep(
        monkeypatch,
        pr,
        {101: ("ABANDONED", "master"), 202: ("NEW", STABLE)},
    )
    closer.assert_not_called()


def test_abandoned_change_on_the_pr_branch_closes_the_pr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pr = _pull_request(STABLE, _raised(202))
    closer = _sweep(monkeypatch, pr, {202: ("ABANDONED", STABLE)})
    closer.assert_called_once()
    assert closer.call_args.kwargs["gerrit_change_url"] == _url(202)


def test_a_mention_of_another_change_is_not_the_prs_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pr = _pull_request(STABLE, _raised(202), f"Compare with {_url(303)} please")
    closer = _sweep(
        monkeypatch,
        pr,
        {202: ("NEW", STABLE), 303: ("ABANDONED", STABLE)},
    )
    closer.assert_not_called()
