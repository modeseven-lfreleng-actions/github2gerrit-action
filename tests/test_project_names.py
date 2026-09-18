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
from github2gerrit.project_names import opted_in_gerrit_project_lister
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
        assert names == RepoNames(
            "operator/said/so", "operator-said-so", "explicit"
        )

    def test_gitreview_is_authoritative_over_the_guess(self) -> None:
        names = resolve_repo_names(
            "onap/aai-aai-common", gitreview_project="aai/aai-common.git"
        )
        assert names == RepoNames(
            "aai/aai-common", "aai-aai-common", "gitreview"
        )

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
        assert names == RepoNames(
            "releng/builder", "releng-builder", "gitreview"
        )

    def test_gerrit_lookup_beats_the_guess(self) -> None:
        def _list(prefix: str) -> list[str]:
            return ["aai/aai-common"]

        names = resolve_repo_names("onap/aai-aai-common", list_projects=_list)
        assert names.project_gerrit == "aai/aai-common"
        assert names.source == "gerrit"
        assert names.confirmed is True

    def test_fallback_beats_the_guess_but_not_the_lookup(self) -> None:
        # A derived value somebody once wrote down ranks above reading
        # the hyphens blind, and below anything that confirms the name.
        def _list(prefix: str) -> list[str]:
            return ["aai/aai-common"]

        names = resolve_repo_names(
            "onap/aai-aai-common",
            list_projects=_list,
            fallback_project="legacy/value",
        )
        assert names.project_gerrit == "aai/aai-common"

        def _nothing(prefix: str) -> list[str]:
            return []

        names = resolve_repo_names(
            "onap/aai-aai-common",
            list_projects=_nothing,
            fallback_project="legacy/value.git",
        )
        assert names == RepoNames("legacy/value", "aai-aai-common", "fallback")
        assert names.confirmed is False
        assert names.guessed is False

    def test_gitreview_beats_the_fallback(self) -> None:
        names = resolve_repo_names(
            "onap/aai-aai-common",
            gitreview_project="aai/aai-common",
            fallback_project="legacy/value",
        )
        assert names.project_gerrit == "aai/aai-common"

    def test_falls_back_to_the_guess_and_says_so(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(
            logging.INFO, logger="github2gerrit.project_names"
        ):
            names = resolve_repo_names("acme/my-repo-name")
        assert names == RepoNames("my/repo/name", "my-repo-name", "guess")
        assert "guessing" in caplog.text

    def test_blank_fallback_is_a_guess(self) -> None:
        names = resolve_repo_names("acme/thing-two", fallback_project="  ")
        assert names.guessed is True

    def test_hyphen_free_name_has_one_reading(self) -> None:
        # Nothing to guess between: the only candidate is the name.
        names = resolve_repo_names("lfit/sandbox")
        assert names == RepoNames("sandbox", "sandbox", "name")
        assert names.guessed is False
        assert names.confirmed is False

    def test_hyphen_free_name_is_not_looked_up_without_a_fallback(
        self,
    ) -> None:
        # The server could only confirm the one reading; not worth an
        # authenticated round trip unless there is a fallback it could
        # supersede.
        asked: list[str] = []

        def _list(prefix: str) -> list[str]:
            asked.append(prefix)
            return ["sandbox"]

        names = resolve_repo_names("lfit/sandbox", list_projects=_list)
        assert names == RepoNames("sandbox", "sandbox", "name")
        assert asked == []

        names = resolve_repo_names(
            "lfit/sandbox", list_projects=_list, fallback_project="legacy"
        )
        assert names == RepoNames("sandbox", "sandbox", "gerrit")
        assert asked == ["sandbox"]

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


class TestOptedInGerritProjectLister:
    """The one set of conditions every caller applies before asking."""

    @pytest.fixture
    def client_factory(self, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        built: list[str] = []

        class _Client:
            def get(self, path: str) -> dict[str, object]:
                return {}

        def _build(host: str, **_: object) -> _Client:
            built.append(host)
            return _Client()

        monkeypatch.setattr(
            "github2gerrit.gerrit_rest.build_client_for_host", _build
        )
        monkeypatch.delenv("G2G_NO_GERRIT", raising=False)
        monkeypatch.delenv("G2G_DRYRUN_DISABLE_NETWORK", raising=False)
        return built

    def test_off_by_default(
        self, client_factory: list[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("G2G_RESOLVE_PROJECT_VIA_GERRIT", raising=False)
        assert opted_in_gerrit_project_lister("gerrit.onap.org") is None
        assert client_factory == []

    def test_opted_in_builds_a_client_for_the_host(
        self, client_factory: list[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("G2G_RESOLVE_PROJECT_VIA_GERRIT", "true")
        lister = opted_in_gerrit_project_lister(" gerrit.onap.org ")
        assert lister is not None
        assert client_factory == ["gerrit.onap.org"]

    @pytest.mark.parametrize(
        "guard", ["G2G_NO_GERRIT", "G2G_DRYRUN_DISABLE_NETWORK"]
    )
    def test_network_guards_win_over_the_opt_in(
        self,
        guard: str,
        client_factory: list[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # G2G_NO_GERRIT promises no Gerrit calls at all. Parameter
        # derivation runs before the CLI has translated it into the
        # network flag, so both spellings have to be honoured here.
        monkeypatch.setenv("G2G_RESOLVE_PROJECT_VIA_GERRIT", "true")
        monkeypatch.setenv(guard, "true")
        assert opted_in_gerrit_project_lister("gerrit.onap.org") is None
        assert client_factory == []

    @pytest.mark.parametrize("host", [None, "", "   "])
    def test_no_host_means_no_lister(
        self,
        host: str | None,
        client_factory: list[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("G2G_RESOLVE_PROJECT_VIA_GERRIT", "true")
        assert opted_in_gerrit_project_lister(host) is None
        assert client_factory == []

    def test_unbuildable_client_degrades_to_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("G2G_RESOLVE_PROJECT_VIA_GERRIT", "true")
        monkeypatch.delenv("G2G_NO_GERRIT", raising=False)
        monkeypatch.delenv("G2G_DRYRUN_DISABLE_NETWORK", raising=False)

        def _boom(host: str, **_: object) -> None:
            raise OSError("no netrc")

        monkeypatch.setattr(
            "github2gerrit.gerrit_rest.build_client_for_host", _boom
        )
        assert opted_in_gerrit_project_lister("gerrit.onap.org") is None

    def test_caller_may_supply_the_configuration(
        self, client_factory: list[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Parameter derivation reads its settings from the configuration
        # file as well, before it has been exported to the environment.
        monkeypatch.delenv("G2G_RESOLVE_PROJECT_VIA_GERRIT", raising=False)
        cfg = {"G2G_RESOLVE_PROJECT_VIA_GERRIT": "true"}
        assert (
            opted_in_gerrit_project_lister("gerrit.onap.org", config=cfg)
            is not None
        )
        # A non-blank environment value keeps precedence over the file.
        monkeypatch.setenv("G2G_RESOLVE_PROJECT_VIA_GERRIT", "false")
        assert (
            opted_in_gerrit_project_lister("gerrit.onap.org", config=cfg)
            is None
        )

    @pytest.mark.parametrize(
        "guard", ["G2G_NO_GERRIT", "G2G_DRYRUN_DISABLE_NETWORK"]
    )
    def test_network_guards_are_read_from_the_configuration_too(
        self,
        guard: str,
        client_factory: list[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A file that opts in and also promises no Gerrit calls must be
        # held to the promise before the file reaches the environment.
        monkeypatch.delenv("G2G_RESOLVE_PROJECT_VIA_GERRIT", raising=False)
        cfg = {"G2G_RESOLVE_PROJECT_VIA_GERRIT": "true", guard: "true"}
        assert (
            opted_in_gerrit_project_lister("gerrit.onap.org", config=cfg)
            is None
        )
        assert client_factory == []

    def test_http_credentials_reach_the_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A server that needs credentials for /projects/ gets them.

        Without this the lookup would run unauthenticated, fail, and
        fall back to the guess without saying why.
        """
        monkeypatch.setenv("G2G_RESOLVE_PROJECT_VIA_GERRIT", "true")
        monkeypatch.delenv("G2G_NO_GERRIT", raising=False)
        monkeypatch.delenv("G2G_DRYRUN_DISABLE_NETWORK", raising=False)
        monkeypatch.delenv("GERRIT_HTTP_USER", raising=False)
        monkeypatch.delenv("GERRIT_HTTP_BASE_PATH", raising=False)
        monkeypatch.setenv("GERRIT_HTTP_PASSWORD", "from-env")
        seen: dict[str, object] = {}

        class _Client:
            def get(self, path: str) -> dict[str, object]:
                return {}

        def _build(host: str, **kwargs: object) -> _Client:
            seen.update(kwargs)
            return _Client()

        monkeypatch.setattr(
            "github2gerrit.gerrit_rest.build_client_for_host", _build
        )
        cfg = {
            "GERRIT_HTTP_USER": "from-file",
            "GERRIT_HTTP_PASSWORD": "file-password",
            "GERRIT_HTTP_BASE_PATH": "r",
        }
        opted_in_gerrit_project_lister("gerrit.onap.org", config=cfg)
        assert seen == {
            "base_path": "r",
            "http_user": "from-file",
            "http_password": "from-env",
        }

        seen.clear()
        monkeypatch.delenv("GERRIT_HTTP_PASSWORD", raising=False)
        opted_in_gerrit_project_lister("gerrit.onap.org")
        assert seen == {
            "base_path": None,
            "http_user": None,
            "http_password": None,
        }

    def test_listings_are_fetched_once_per_host_and_prefix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Bulk mode builds one orchestrator per pull request.

        Each would otherwise ask the same server the same question.
        The answer is a function of host and prefix alone and does not
        change within a run, so it is remembered on exactly that key.
        """
        monkeypatch.setenv("G2G_RESOLVE_PROJECT_VIA_GERRIT", "true")
        monkeypatch.delenv("G2G_NO_GERRIT", raising=False)
        monkeypatch.delenv("G2G_DRYRUN_DISABLE_NETWORK", raising=False)
        fetched: list[tuple[str, str]] = []

        class _Client:
            def __init__(self, host: str) -> None:
                self.host = host

            def get(self, path: str) -> dict[str, object]:
                fetched.append((self.host, path))
                return {"aai/aai-common": {}}

        monkeypatch.setattr(
            "github2gerrit.gerrit_rest.build_client_for_host",
            lambda host, **_: _Client(host),
        )

        first = opted_in_gerrit_project_lister("gerrit.onap.org")
        second = opted_in_gerrit_project_lister("gerrit.onap.org")
        other = opted_in_gerrit_project_lister("gerrit.other.org")
        assert first and second and other

        assert first("aai") == ["aai/aai-common"]
        assert second("aai") == ["aai/aai-common"]
        assert first("sdc") == ["aai/aai-common"]
        assert other("aai") == ["aai/aai-common"]
        assert fetched == [
            ("gerrit.onap.org", "/projects/?p=aai"),
            ("gerrit.onap.org", "/projects/?p=sdc"),
            ("gerrit.other.org", "/projects/?p=aai"),
        ]

    def test_failed_fetch_is_not_remembered(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("G2G_RESOLVE_PROJECT_VIA_GERRIT", "true")
        monkeypatch.delenv("G2G_NO_GERRIT", raising=False)
        monkeypatch.delenv("G2G_DRYRUN_DISABLE_NETWORK", raising=False)
        calls = {"n": 0}

        class _Client:
            def get(self, path: str) -> dict[str, object]:
                calls["n"] += 1
                if calls["n"] == 1:
                    raise OSError("transient")
                return {"aai/aai-common": {}}

        monkeypatch.setattr(
            "github2gerrit.gerrit_rest.build_client_for_host",
            lambda host, **_: _Client(),
        )
        lister = opted_in_gerrit_project_lister("gerrit.onap.org")
        assert lister is not None
        with pytest.raises(OSError, match="transient"):
            lister("aai")
        assert lister("aai") == ["aai/aai-common"]
        assert calls["n"] == 2

    def test_concurrent_callers_share_one_fetch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same key, one request; other keys are not held up.

        Bulk workers all start at once and all miss the memo together.
        The fetch below blocks until every worker has arrived, so a
        check-then-fetch race would issue one request per worker.
        """
        import threading
        import time

        monkeypatch.setenv("G2G_RESOLVE_PROJECT_VIA_GERRIT", "true")
        monkeypatch.delenv("G2G_NO_GERRIT", raising=False)
        monkeypatch.delenv("G2G_DRYRUN_DISABLE_NETWORK", raising=False)
        workers = 6
        arrived = threading.Barrier(workers, timeout=10)
        fetches: list[str] = []
        fetch_lock = threading.Lock()

        class _Client:
            def get(self, path: str) -> dict[str, object]:
                with fetch_lock:
                    fetches.append(path)
                # Hold the fetch open long enough for every worker to
                # have checked the memo, so a check-then-fetch race
                # shows up as extra requests rather than passing by luck.
                time.sleep(0.1)
                return {"aai/aai-common": {}, "sdc/sdc-be": {}}

        monkeypatch.setattr(
            "github2gerrit.gerrit_rest.build_client_for_host",
            lambda host, **_: _Client(),
        )
        results: list[list[str]] = []
        results_lock = threading.Lock()

        def _worker(prefix: str) -> None:
            lister = opted_in_gerrit_project_lister("gerrit.onap.org")
            assert lister is not None
            arrived.wait()
            got = lister(prefix)
            with results_lock:
                results.append(got)

        threads = [
            threading.Thread(target=_worker, args=("aai" if i % 2 else "sdc",))
            for i in range(workers)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        assert not any(t.is_alive() for t in threads)

        assert len(results) == workers
        assert sorted(fetches) == ["/projects/?p=aai", "/projects/?p=sdc"]
