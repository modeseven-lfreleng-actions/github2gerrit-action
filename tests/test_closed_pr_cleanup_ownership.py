# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Closed-PR cleanup abandons only the pull request's own changes.

Both cleanup paths find a closed pull request's changes by the
``GitHub-PR`` trailer. Gerrit cherry-picks copy that trailer, so a
maintainer's backport of a merged change names the pull request it came
from, and was abandoned with it (#502).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from github2gerrit.gerrit_pr_closer import abandon_gerrit_change_for_closed_pr
from github2gerrit.gerrit_pr_closer import cleanup_closed_github_prs


REPOSITORY = "opendaylight/mdsal"
MODULE = "github2gerrit.gerrit_pr_closer"


def _change(
    number: int, pr: int, branch: str, owner: str | None
) -> dict[str, Any]:
    message = (
        "Bump foo from 1.0 to 1.1\n\n"
        f"GitHub-PR: https://github.com/{REPOSITORY}/pull/{pr}\n"
    )
    change: dict[str, Any] = {
        "_number": number,
        "subject": "Bump foo from 1.0 to 1.1",
        "branch": branch,
        "current_revision": "rev",
        "revisions": {"rev": {"commit": {"message": message}}},
    }
    if owner is not None:
        change["owner"] = {"_account_id": number, "username": owner}
    return change


def _closed_pr(number: int, base_ref: str) -> MagicMock:
    pr = MagicMock()
    pr.number = number
    pr.state = "closed"
    pr.base.ref = base_ref
    return pr


@pytest.fixture
def abandoned(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the change numbers that get abandoned."""
    numbers: list[str] = []
    monkeypatch.setattr(
        f"{MODULE}._abandon_gerrit_change",
        lambda _client, number, _message: numbers.append(str(number)),
    )
    monkeypatch.setattr(
        f"{MODULE}._build_gerrit_abandon_message", lambda *_args: "closed"
    )
    monkeypatch.setattr(f"{MODULE}.build_client", MagicMock())
    return numbers


def _serve(
    monkeypatch: pytest.MonkeyPatch,
    changes: list[dict[str, Any]],
    prs: dict[int, MagicMock],
) -> None:
    gerrit = MagicMock()
    gerrit.get.return_value = changes
    for target in (
        f"{MODULE}.build_client_for_host",
        "github2gerrit.gerrit_rest.build_client_for_host",
    ):
        monkeypatch.setattr(target, lambda _host, **_kwargs: gerrit)
    monkeypatch.setattr(f"{MODULE}.get_pull", lambda _repo, number: prs[number])


def test_sweep_spares_a_maintainers_cherry_pick(
    monkeypatch: pytest.MonkeyPatch, abandoned: list[str]
) -> None:
    monkeypatch.setenv("GERRIT_SSH_USER_G2G", "gh2gerrit")
    _serve(
        monkeypatch,
        [
            _change(126368, 31, "14.0.x", "rovarga"),
            _change(127190, 40, "master", "gh2gerrit"),
        ],
        {31: _closed_pr(31, "master"), 40: _closed_pr(40, "15.0.x")},
    )
    cleanup_closed_github_prs("gerrit.example.org", "mdsal")
    assert abandoned == ["127190"]


def test_sweep_without_ownership_spares_other_branches(
    monkeypatch: pytest.MonkeyPatch, abandoned: list[str]
) -> None:
    monkeypatch.delenv("GERRIT_SSH_USER_G2G", raising=False)
    _serve(
        monkeypatch,
        [
            _change(126368, 31, "14.0.x", None),
            _change(126300, 31, "master", None),
        ],
        {31: _closed_pr(31, "master")},
    )
    cleanup_closed_github_prs("gerrit.example.org", "mdsal")
    assert abandoned == ["126300"]


def test_sweep_still_abandons_its_own_change(
    monkeypatch: pytest.MonkeyPatch, abandoned: list[str]
) -> None:
    monkeypatch.setenv("GERRIT_SSH_USER_G2G", "gh2gerrit")
    _serve(
        monkeypatch,
        [_change(126300, 31, "master", "gh2gerrit")],
        {31: _closed_pr(31, "master")},
    )
    cleanup_closed_github_prs("gerrit.example.org", "mdsal")
    assert abandoned == ["126300"]


def test_closed_pr_abandons_its_own_change_not_a_backport(
    monkeypatch: pytest.MonkeyPatch, abandoned: list[str]
) -> None:
    monkeypatch.setenv("GERRIT_SSH_USER_G2G", "gh2gerrit")
    _serve(
        monkeypatch,
        [
            _change(126368, 31, "14.0.x", "rovarga"),
            _change(126300, 31, "master", "gh2gerrit"),
        ],
        {31: _closed_pr(31, "master")},
    )
    result = abandon_gerrit_change_for_closed_pr(
        pr_number=31,
        gerrit_server="gerrit.example.org",
        gerrit_project="mdsal",
        repository=REPOSITORY,
    )
    assert result == "126300"
    assert abandoned == ["126300"]


def test_closed_pr_prefers_its_change_on_the_base_branch(
    monkeypatch: pytest.MonkeyPatch, abandoned: list[str]
) -> None:
    """Of two GitHub2Gerrit changes, the misrouted one waits for the sweep."""
    monkeypatch.setenv("GERRIT_SSH_USER_G2G", "gh2gerrit")
    _serve(
        monkeypatch,
        [
            _change(127186, 37, "master", "gh2gerrit"),
            _change(127300, 37, "stable/titanium", "gh2gerrit"),
        ],
        {37: _closed_pr(37, "stable/titanium")},
    )
    result = abandon_gerrit_change_for_closed_pr(
        pr_number=37,
        gerrit_server="gerrit.example.org",
        gerrit_project="openflowplugin",
        repository=REPOSITORY,
        base_ref="stable/titanium",
    )
    assert result == "127300"
