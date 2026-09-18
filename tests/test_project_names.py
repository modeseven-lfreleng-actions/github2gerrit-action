# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Mapping between GitHub repository names and Gerrit project paths.

The interesting cases are the ones where the obvious rule is wrong.
``aai-aai-common`` is ``aai/aai-common`` on gerrit.onap.org, and the
close handler in #441 queried ``multicloud-openstack`` for a change that
lived under ``multicloud/openstack``. Both are real.
"""

from __future__ import annotations

import logging

import pytest

from github2gerrit.project_names import RepoNames
from github2gerrit.project_names import candidate_gerrit_projects
from github2gerrit.project_names import find_gerrit_project
from github2gerrit.project_names import gerrit_project_lister
from github2gerrit.project_names import gerrit_to_github
from github2gerrit.project_names import github_repo_name
from github2gerrit.project_names import github_to_gerrit_guess
from github2gerrit.project_names import resolve_repo_names


class TestGerritToGithub:
    """The exact direction."""

    @pytest.mark.parametrize(
        ("project", "expected"),
        [
            ("releng/builder", "releng-builder"),
            ("multicloud/openstack", "multicloud-openstack"),
            ("aai/aai-common", "aai-aai-common"),
            ("integration", "integration"),
            ("a/b/c/d", "a-b-c-d"),
        ],
    )
    def test_slashes_become_hyphens(self, project: str, expected: str) -> None:
        assert gerrit_to_github(project) == expected

    def test_git_suffix_is_dropped(self) -> None:
        # .gitreview files commonly carry it; GitHub names never do.
        assert gerrit_to_github("releng/builder.git") == "releng-builder"

    def test_surrounding_noise_is_ignored(self) -> None:
        assert gerrit_to_github("  /releng/builder/  ") == "releng-builder"


class TestGithubRepoName:
    @pytest.mark.parametrize(
        ("repository", "expected"),
        [
            ("onap/multicloud-openstack", "multicloud-openstack"),
            ("multicloud-openstack", "multicloud-openstack"),
            ("  onap/aai-common/  ", "aai-common"),
        ],
    )
    def test_owner_is_stripped(self, repository: str, expected: str) -> None:
        assert github_repo_name(repository) == expected


class TestGithubToGerritGuess:
    """The heuristic, and where it breaks."""

    def test_reads_every_hyphen_as_a_separator(self) -> None:
        assert github_to_gerrit_guess("multicloud-openstack") == (
            "multicloud/openstack"
        )

    def test_accepts_an_owner_prefix(self) -> None:
        assert github_to_gerrit_guess("onap/multicloud-openstack") == (
            "multicloud/openstack"
        )

    def test_is_wrong_for_hyphenated_segments(self) -> None:
        # Recorded deliberately. The real project is aai/aai-common,
        # and the guess cannot know that. This is why callers should
        # prefer .gitreview and why the function is named a guess.
        assert github_to_gerrit_guess("aai-aai-common") == "aai/aai/common"

    def test_round_trips_with_the_exact_direction_only_one_way(self) -> None:
        # Gerrit -> GitHub -> guess is only stable when every hyphen
        # in the original was a separator.
        assert github_to_gerrit_guess(gerrit_to_github("a/b/c")) == "a/b/c"
        assert github_to_gerrit_guess(gerrit_to_github("aai/aai-common")) != (
            "aai/aai-common"
        )


class TestCandidateGerritProjects:
    def test_enumerates_every_reading(self) -> None:
        assert candidate_gerrit_projects("aai-aai-common") == [
            "aai-aai-common",
            "aai-aai/common",
            "aai/aai-common",
            "aai/aai/common",
        ]

    def test_bare_name_is_always_present(self) -> None:
        for name in ("integration", "a-b", "a-b-c"):
            assert name in candidate_gerrit_projects(name)

    def test_no_hyphens_yields_only_the_name(self) -> None:
        assert candidate_gerrit_projects("integration") == ["integration"]

    def test_owner_is_stripped_first(self) -> None:
        assert candidate_gerrit_projects("onap/a-b") == ["a-b", "a/b"]

    def test_fewest_separators_first(self) -> None:
        candidates = candidate_gerrit_projects("a-b-c")
        assert candidates[0] == "a-b-c"
        assert candidates[-1] == "a/b/c"

    def test_absurd_hyphen_counts_are_not_enumerated(self) -> None:
        # 2**30 candidates would be neither useful nor kind.
        name = "-".join(["x"] * 31)
        assert candidate_gerrit_projects(name) == [name]


class TestFindGerritProject:
    """Disambiguation against what the server actually has."""

    @staticmethod
    def _server(*projects: str):
        def _list(prefix: str) -> list[str]:
            return [p for p in projects if p.startswith(prefix)]

        return _list

    def test_picks_the_one_that_exists(self) -> None:
        server = self._server("aai/aai-common", "aai/babel", "aai/resources")
        assert find_gerrit_project("aai-aai-common", server) == "aai/aai-common"

    def test_the_guess_would_have_been_wrong(self) -> None:
        # The case the guess gets wrong is exactly the one this resolves.
        server = self._server("aai/aai-common")
        assert github_to_gerrit_guess("aai-aai-common") == "aai/aai/common"
        assert find_gerrit_project("aai-aai-common", server) == "aai/aai-common"

    def test_flat_project_resolves_to_itself(self) -> None:
        server = self._server("integration", "integration/csit")
        assert find_gerrit_project("integration", server) == "integration"

    def test_nothing_known_yields_none(self) -> None:
        assert find_gerrit_project("aai-aai-common", self._server()) is None

    def test_two_matches_are_refused(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Choosing between them would be no better than the guess this
        # function exists to avoid.
        server = self._server("aai/aai-common", "aai/aai/common")
        with caplog.at_level(
            logging.WARNING, logger="github2gerrit.project_names"
        ):
            assert find_gerrit_project("aai-aai-common", server) is None
        assert "several Gerrit projects" in caplog.text

    def test_server_git_suffixes_are_tolerated(self) -> None:
        server = self._server("aai/aai-common.git")
        assert find_gerrit_project("aai-aai-common", server) == "aai/aai-common"

    def test_a_failing_lister_yields_none(self) -> None:
        def _boom(prefix: str) -> list[str]:
            raise OSError("gerrit unreachable")

        assert find_gerrit_project("aai-aai-common", _boom) is None

    def test_queries_by_the_first_segment(self) -> None:
        seen: list[str] = []

        def _list(prefix: str) -> list[str]:
            seen.append(prefix)
            return []

        find_gerrit_project("onap/multicloud-openstack", _list)
        assert seen == ["multicloud"]


class TestResolveRepoNames:
    """The priority chain callers should use."""

    def test_explicit_project_outranks_everything(self) -> None:
        names = resolve_repo_names(
            "onap/multicloud-openstack",
            gitreview_project="wrong/project",
            explicit_project="operator/said/so",
        )
        assert names == RepoNames("operator/said/so", "operator-said-so")

    def test_gitreview_is_authoritative_over_the_guess(self) -> None:
        names = resolve_repo_names(
            "onap/aai-aai-common", gitreview_project="aai/aai-common.git"
        )
        assert names == RepoNames("aai/aai-common", "aai-aai-common")

    def test_github_name_is_flattened_from_an_authoritative_project(
        self,
    ) -> None:
        # Deliberate, and load-bearing: the Gerrit topic that identifies
        # a pull request is built from project_github, and push and
        # query must agree on it. Taking the caller's repository name
        # here would let the two diverge when the mirror is renamed.
        names = resolve_repo_names(
            "org/service-repo", gitreview_project="releng/builder"
        )
        assert names.project_github == "releng-builder"

    def test_github_name_is_flattened_when_repository_is_absent(self) -> None:
        names = resolve_repo_names("", gitreview_project="releng/builder")
        assert names == RepoNames("releng/builder", "releng-builder")

    def test_gerrit_lookup_beats_the_guess(self) -> None:
        def _list(prefix: str) -> list[str]:
            return ["aai/aai-common"]

        names = resolve_repo_names("onap/aai-aai-common", list_projects=_list)
        assert names.project_gerrit == "aai/aai-common"

    def test_falls_back_to_the_guess_and_says_so(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(
            logging.INFO, logger="github2gerrit.project_names"
        ):
            names = resolve_repo_names("acme/my-repo-name")
        assert names == RepoNames("my/repo/name", "my-repo-name")
        assert "guessing" in caplog.text

    def test_nothing_to_go_on_is_an_error(self) -> None:
        with pytest.raises(ValueError, match="no repository and no project"):
            resolve_repo_names("")

    def test_blank_authoritative_values_are_ignored(self) -> None:
        names = resolve_repo_names(
            "acme/thing", gitreview_project="  ", explicit_project=""
        )
        assert names.project_gerrit == "thing"


class TestGerritProjectLister:
    def test_wraps_the_projects_endpoint(self) -> None:
        class _Client:
            def __init__(self) -> None:
                self.paths: list[str] = []

            def get(self, path: str) -> dict[str, dict[str, str]]:
                self.paths.append(path)
                return {"aai/aai-common": {}, "aai/babel": {}}

        client = _Client()
        lister = gerrit_project_lister(client)
        assert sorted(lister("aai")) == ["aai/aai-common", "aai/babel"]
        assert client.paths == ["/projects/?p=aai"]

    def test_prefix_is_url_encoded(self) -> None:
        class _Client:
            def __init__(self) -> None:
                self.paths: list[str] = []

            def get(self, path: str) -> dict[str, object]:
                self.paths.append(path)
                return {}

        client = _Client()
        gerrit_project_lister(client)("weird name/with slash")
        assert client.paths == ["/projects/?p=weird%20name%2Fwith%20slash"]

    def test_non_mapping_response_yields_nothing(self) -> None:
        class _Client:
            def get(self, path: str) -> list[object]:
                return []

        assert gerrit_project_lister(_Client())("aai") == []
