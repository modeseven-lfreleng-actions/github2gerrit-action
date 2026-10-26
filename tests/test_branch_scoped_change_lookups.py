# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Change state and details come from the pull request's target branch.

One Change-Id can name a change on several branches: Gerrit cherry-picks
keep it, and a change re-pushed to a new branch reuses it. A bare
Change-Id then fails to address one change (Gerrit answers "Multiple
changes found"), and a search limited to one result returns whichever
branch's change was updated last (#502).

``FakeGerrit`` follows the server's rules, as observed on Gerrit 3.7:
a bare Change-Id resolves only when unique, ``project~branch~Change-Id``
always resolves, and searches list the most recently updated first.
"""

from __future__ import annotations

import shlex
import urllib.parse
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from github2gerrit.core import GerritInfo
from github2gerrit.core import Orchestrator
from github2gerrit.core import PreparedChange
from github2gerrit.core import RepoNames
from github2gerrit.gerrit_query import GerritChange
from github2gerrit.gerrit_rest import GerritRestError
from github2gerrit.models import GitHubContext


PROJECT = "example/project"
CHANGE_ID = "I" + "a" * 40
GERRIT = GerritInfo(host="gerrit.example.org", port=29418, project=PROJECT)
_PENDING = pytest.mark.xfail(
    strict=True, reason="lookups ignore the target branch (#502)"
)


def _change(number: int, branch: str, status: str) -> dict[str, Any]:
    sha = f"{number:040d}"
    return {
        "change_id": CHANGE_ID,
        "_number": number,
        "project": PROJECT,
        "branch": branch,
        "status": status,
        "subject": "Bump foo from 1.0 to 1.1",
        "current_revision": sha,
        "revisions": {
            sha: {
                "_number": 2,
                "commit": {"message": "Bump foo from 1.0 to 1.1\n"},
            }
        },
    }


class FakeGerrit:
    """A Gerrit REST client over a fixed list of changes, newest first."""

    is_authenticated = True

    def __init__(self, *changes: dict[str, Any]) -> None:
        self.changes = list(changes)
        self.puts: list[str] = []

    def get(self, path: str) -> Any:
        parts = urllib.parse.urlsplit(path)
        if parts.path.rstrip("/") == "/changes":
            return self._search(urllib.parse.parse_qs(parts.query))
        return self._resolve(parts.path.split("/")[2])

    def put(self, path: str, data: Any | None = None) -> Any:
        self._resolve(path.split("/")[2])
        self.puts.append(path)
        return {"ok": True}

    def _search(self, params: dict[str, list[str]]) -> list[dict[str, Any]]:
        found = list(self.changes)
        limit = int(params.get("n", ["100"])[0])
        for token in shlex.split(params["q"][0]):
            key, _, value = token.partition(":")
            if key == "limit":
                limit = min(limit, int(value))
            elif key == "is" and value == "open":
                found = [c for c in found if c["status"] == "NEW"]
            elif key in ("change", "project", "branch"):
                field = "change_id" if key == "change" else key
                found = [c for c in found if c[field] == value]
            elif not value and token.startswith("I"):
                found = [c for c in found if c["change_id"] == token]
        return found[:limit]

    def _resolve(self, ident: str) -> dict[str, Any]:
        parts = [urllib.parse.unquote(p) for p in ident.split("~")]
        if len(parts) == 3:
            matches = [
                c
                for c in self.changes
                if [c["project"], c["branch"], c["change_id"]] == parts
            ]
        else:
            matches = [c for c in self.changes if c["change_id"] == parts[0]]
        if len(matches) > 1:
            msg = f"Multiple changes found for {ident}"
            raise GerritRestError(msg, status=404)
        if not matches:
            msg = f"Not found: {ident}"
            raise GerritRestError(msg, status=404)
        return matches[0]


def _ctx(base_ref: str) -> GitHubContext:
    return GitHubContext(
        event_name="pull_request_target",
        event_action="synchronize",
        event_path=None,
        repository=PROJECT,
        repository_owner="example",
        server_url="https://github.com",
        run_id="1",
        sha="0" * 40,
        base_ref=base_ref,
        head_ref="feature",
        pr_number=39,
    )


@pytest.fixture(autouse=True)
def _environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GERRIT_BRANCH", raising=False)
    monkeypatch.delenv("GITHUB_BASE_REF", raising=False)
    monkeypatch.setattr(
        "github2gerrit.gerrit_urls._discover_base_path_for_host",
        lambda _host, timeout=5.0: "",
    )


def _serve(monkeypatch: pytest.MonkeyPatch, gerrit: FakeGerrit) -> None:
    monkeypatch.setattr(
        "github2gerrit.gerrit_rest.build_client_for_host",
        lambda _host, **_kwargs: gerrit,
    )


@_PENDING
@pytest.mark.parametrize(
    ("target", "elsewhere"),
    [
        ("master", _change(200, "stable/vanadium", "MERGED")),
        ("stable/vanadium", _change(200, "master", "ABANDONED")),
    ],
    ids=["merged-cherry-pick", "abandoned-misroute"],
)
def test_final_state_is_read_from_the_target_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    elsewhere: dict[str, Any],
) -> None:
    """A closed change on another branch must not close an open PR."""
    _serve(monkeypatch, FakeGerrit(elsewhere, _change(100, target, "NEW")))
    orch = Orchestrator(workspace=tmp_path)
    with patch.object(orch, "_reconcile_pr_for_final_changes") as close:
        result = orch._reconcile_final_state_changes(
            gh=_ctx(target), gerrit=GERRIT, change_ids=[CHANGE_ID]
        )
    assert result is None
    close.assert_not_called()


@_PENDING
def test_push_results_report_the_target_branch_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(
        monkeypatch,
        FakeGerrit(
            _change(100, "master", "NEW"),
            _change(200, "stable/vanadium", "NEW"),
        ),
    )
    orch = Orchestrator(workspace=tmp_path)
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
            "_resolve_reviewers",
        )
    }
    with patch.multiple(orch, **stubs):
        result = orch._push_and_finalize(
            inputs=MagicMock(),
            gh=_ctx("stable/vanadium"),
            gerrit=GERRIT,
            repo_names=RepoNames(project_gerrit=PROJECT, project_github="p"),
            prep=PreparedChange(change_ids=[CHANGE_ID], commit_shas=[]),
            operation_mode="update",
        )
    assert result.change_numbers == ["200"]
    assert result.change_urls == [
        "https://gerrit.example.org/c/example/project/+/200"
    ]


@_PENDING
def test_patchset_verification_finds_the_target_branch_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _serve(
        monkeypatch,
        FakeGerrit(
            _change(100, "master", "NEW"),
            _change(200, "stable/vanadium", "NEW"),
        ),
    )
    orch = Orchestrator(workspace=tmp_path)
    with patch.object(orch, "_sync_gerrit_change_metadata"):
        orch._verify_and_sync_after_push(
            gh=_ctx("stable/vanadium"),
            gerrit=GERRIT,
            prep=PreparedChange(change_ids=[CHANGE_ID], commit_shas=[]),
            is_update_operation=True,
            is_edit_operation=False,
        )
    (record,) = orch._verification_results
    assert record["verified"] is True
    assert record["change_number"] == 200


@_PENDING
def test_metadata_sync_updates_the_target_branch_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gerrit = FakeGerrit(
        _change(100, "master", "NEW"),
        _change(200, "stable/vanadium", "NEW"),
    )
    _serve(monkeypatch, gerrit)
    for name in ("build_client", "get_repo_from_env", "get_pull"):
        monkeypatch.setattr(f"github2gerrit.core.{name}", MagicMock())
    monkeypatch.setattr(
        "github2gerrit.core.get_pr_title_body",
        lambda _pr: ("Bump foo from 1.0 to 1.2", "Body"),
    )
    orch = Orchestrator(workspace=tmp_path)
    orch._sync_gerrit_change_metadata(
        gh=_ctx("stable/vanadium"), gerrit=GERRIT, change_ids=[CHANGE_ID]
    )
    triplet = "~".join(
        urllib.parse.quote(part, safe="")
        for part in (PROJECT, "stable/vanadium", CHANGE_ID)
    )
    assert gerrit.puts == [f"/changes/{triplet}/message"]


@_PENDING
def test_topic_lookup_lists_a_shared_change_id_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def twin(number: int) -> GerritChange:
        return GerritChange(
            change_id=CHANGE_ID,
            number=str(number),
            subject="Bump foo from 1.0 to 1.1",
            status="NEW",
            current_revision="",
            files=[],
            commit_message="",
        )

    monkeypatch.setattr(
        "github2gerrit.gerrit_query.query_changes_by_topic",
        lambda *_args, **_kwargs: [twin(100), twin(200)],
    )
    monkeypatch.setattr(
        "github2gerrit.gerrit_rest.build_client_for_host", MagicMock()
    )
    orch = Orchestrator(workspace=tmp_path)
    found = orch._find_change_by_topic(_ctx("stable/vanadium"), GERRIT)
    assert found == [CHANGE_ID]
