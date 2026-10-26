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


BOT = "github-actions[bot]"


def _comment(body: str, login: str = BOT) -> MagicMock:
    """Return a PR comment; bot accounts have type ``Bot``."""
    kind = "Bot" if login.endswith("[bot]") else "User"
    return MagicMock(body=body, user=MagicMock(login=login, type=kind))


def _pull_request(base_ref: str, *comments: str | MagicMock) -> MagicMock:
    pr = MagicMock()
    pr.number = 39
    pr.base.ref = base_ref
    pr.as_issue.return_value.get_comments.return_value = [
        c if isinstance(c, MagicMock) else _comment(c) for c in comments
    ]
    return pr


def _sweep(
    monkeypatch: pytest.MonkeyPatch,
    pr: MagicMock,
    changes: dict[int, tuple[str, str]],
    automation_login: str = "",
) -> MagicMock:
    """Run the sweep over one pull request; return the closer mock."""
    closer = MagicMock(return_value=True)
    module = "github2gerrit.gerrit_pr_closer"
    client = MagicMock()
    if automation_login:
        client.get_user.return_value.login = automation_login
    else:
        client.get_user.side_effect = RuntimeError("403 integration")
    monkeypatch.setattr(f"{module}.build_client", lambda: client)
    monkeypatch.setattr(f"{module}.iter_open_pulls", lambda _repo: [pr])
    monkeypatch.setattr(
        f"{module}.build_client_for_host",
        lambda _host, **_kwargs: _Gerrit(changes),
    )
    monkeypatch.setattr(f"{module}.close_pr_with_status", closer)
    cleanup_abandoned_prs_bulk("owner", "repo")
    return closer


def test_abandoned_change_on_another_branch_leaves_the_pr_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pr = _pull_request(STABLE, _raised(101))
    closer = _sweep(monkeypatch, pr, {101: ("ABANDONED", "master")})
    closer.assert_not_called()


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


@pytest.mark.parametrize(
    "forgery",
    [
        _comment(_raised(303), login="mallory"),
        _comment(f"> {_raised(303)}\n\nWhy was this one abandoned?"),
    ],
    ids=["copied-by-a-person", "quoted"],
)
def test_only_a_standalone_record_by_automation_counts(
    monkeypatch: pytest.MonkeyPatch, forgery: MagicMock
) -> None:
    pr = _pull_request(STABLE, _raised(202), forgery)
    closer = _sweep(
        monkeypatch,
        pr,
        {202: ("NEW", STABLE), 303: ("ABANDONED", STABLE)},
    )
    closer.assert_not_called()


def test_a_record_by_the_tokens_own_account_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A personal access token posts records as its user, not a bot."""
    record = _comment(_raised(202), login="g2g-automation")
    pr = _pull_request(STABLE, record)
    closer = _sweep(
        monkeypatch,
        pr,
        {202: ("ABANDONED", STABLE)},
        automation_login="g2g-automation",
    )
    closer.assert_called_once()
