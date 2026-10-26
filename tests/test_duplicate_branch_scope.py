# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Duplicate detection compares a pull request with its own branch only.

Dependabot raises the same update against each target branch, and
duplicate detection compares subjects with version numbers normalised
away, so every branch's update looked like a duplicate of the first
while the Gerrit query spanned all branches (#502).
"""

from __future__ import annotations

import json
import re
from typing import Any
from unittest.mock import Mock
from unittest.mock import patch
from urllib.parse import parse_qs
from urllib.parse import urlsplit

import pytest
import responses

from github2gerrit.duplicate_detection import DuplicateChangeError
from github2gerrit.duplicate_detection import DuplicateDetector
from github2gerrit.gerrit_query import target_branch
from github2gerrit.models import GitHubContext


STABLE = "stable/vanadium"


def _ctx(base_ref: str = STABLE) -> GitHubContext:
    return GitHubContext(
        event_name="pull_request_target",
        event_action="opened",
        event_path=None,
        repository="opendaylight/mdsal",
        repository_owner="opendaylight",
        server_url="https://github.com",
        run_id="1",
        sha="0" * 40,
        base_ref=base_ref,
        head_ref="dependabot/maven/foo",
        pr_number=41,
    )


def _open_change(number: int, branch: str, subject: str) -> dict[str, Any]:
    return {
        "_number": number,
        "project": "mdsal",
        "branch": branch,
        "status": "NEW",
        "subject": subject,
    }


def _gerrit(
    changes: list[dict[str, Any]],
) -> Any:
    """Answer change queries, honouring a branch predicate like Gerrit."""

    def callback(request: Any) -> tuple[int, dict[str, str], str]:
        query = parse_qs(urlsplit(request.url).query)["q"][0]
        match = re.search(r'branch:"([^"]+)"', query)
        found = [
            c for c in changes if not match or c["branch"] == match.group(1)
        ]
        return 200, {}, ")]}'\n" + json.dumps(found)

    return callback


def _check(gh: GitHubContext, changes: list[dict[str, Any]]) -> None:
    target_pr = Mock()
    target_pr.number = gh.pr_number
    target_pr.title = "Bump foo from 1.2.3 to 1.2.4"
    target_pr.body = ""
    target_pr.get_files.return_value = []
    detector = DuplicateDetector(Mock())
    with (
        responses.RequestsMock() as mocked,
        patch.object(
            detector,
            "_resolve_gerrit_info_from_env_or_gitreview",
            return_value=("gerrit.example.org", "mdsal"),
        ),
        patch.dict("os.environ", {"GERRIT_HTTP_BASE_PATH": "r"}),
    ):
        mocked.add_callback(
            responses.GET,
            re.compile(r"https://gerrit\.example\.org/r/.*"),
            callback=_gerrit(changes),
            content_type="application/json",
        )
        detector.check_for_duplicates(target_pr, allow_duplicates=False, gh=gh)


@pytest.fixture(autouse=True)
def _no_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GERRIT_BRANCH", raising=False)


def test_same_update_on_another_branch_is_not_a_duplicate() -> None:
    _check(
        _ctx(STABLE),
        [_open_change(127192, "master", "Bump foo from 2.0.0 to 2.0.1")],
    )


def test_same_update_on_the_same_branch_is_a_duplicate() -> None:
    with pytest.raises(DuplicateChangeError):
        _check(
            _ctx(STABLE),
            [_open_change(127193, STABLE, "Bump foo from 1.2.0 to 1.2.1")],
        )


def test_gerrit_branch_override_picks_the_compared_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GERRIT_BRANCH", "release-1.2")
    _check(
        _ctx(STABLE),
        [_open_change(127193, STABLE, "Bump foo from 1.2.0 to 1.2.1")],
    )


class TestTargetBranch:
    def test_context_base_ref_comes_first(self) -> None:
        pr = Mock()
        pr.base.ref = "master"
        assert target_branch(STABLE, pr) == STABLE

    def test_falls_back_to_the_pull_requests_base(self) -> None:
        pr = Mock()
        pr.base.ref = "master"
        assert target_branch("", pr) == "master"

    def test_unknown_without_either(self) -> None:
        assert target_branch("", Mock(base=Mock(ref=None))) == ""
        assert target_branch("") == ""

    def test_gerrit_branch_overrides_both(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GERRIT_BRANCH", " release-1.2 ")
        assert target_branch(STABLE) == "release-1.2"
