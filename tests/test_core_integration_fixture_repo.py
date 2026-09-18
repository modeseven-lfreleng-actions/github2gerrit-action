# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

from github2gerrit.config import DERIVED_KEYS_ENV
from github2gerrit.core import GerritInfo
from github2gerrit.core import Orchestrator
from github2gerrit.core import OrchestratorError
from github2gerrit.core import RepoNames
from github2gerrit.models import GitHubContext
from github2gerrit.models import Inputs


sys.path.append(str(Path(__file__).parent))
from fixtures.make_repo import init_repo
from fixtures.make_repo import write_gitreview


def _minimal_inputs(*, dry_run: bool = False) -> Inputs:
    return Inputs(
        submit_single_commits=False,
        use_pr_as_commit=False,
        fetch_depth=10,
        gerrit_known_hosts="example.org ssh-rsa AAAAB3Nza...",
        gerrit_ssh_privkey_g2g="-----BEGIN KEY-----\nabc\n-----END KEY-----",
        gerrit_ssh_user_g2g="gerrit-bot",
        gerrit_ssh_user_g2g_email="gerrit-bot@example.org",
        github_token="ghp_test_token_123",  # noqa: S106
        organization="example",
        reviewers_email="",
        preserve_github_prs=False,
        dry_run=dry_run,
        normalise_commit=True,
        gerrit_server="gerrit.example.org",
        gerrit_server_port=29418,
        gerrit_project="example/project",
        issue_id="",
        issue_id_lookup_json="",
        commit_rules_json="",
        allow_duplicates=False,
        ci_testing=False,
    )


def _gh_ctx(
    *,
    repository: str = "owner/repo-name",
    owner: str = "owner",
    pr_number: int | None = 7,
) -> GitHubContext:
    return GitHubContext(
        event_name="pull_request_target",
        event_action="opened",
        event_path=None,
        repository=repository,
        repository_owner=owner,
        server_url="https://github.com",
        run_id="1",
        sha="deadbeef",
        base_ref="master",
        head_ref="feature/test",
        pr_number=pr_number,
    )


def test_read_gitreview_parses_file(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo", default_branch="main")
    # Write a .gitreview with non-default port and project with .git suffix
    p = write_gitreview(
        repo,
        host="gerrit.acme.org",
        port=29419,
        project="acme/widgets",
    )
    assert p.exists()
    orch = Orchestrator(workspace=repo.path)
    info = orch._read_gitreview(repo.path / ".gitreview")
    assert info is not None
    assert info.host == "gerrit.acme.org"
    assert info.port == 29419
    # The .git suffix should be removed by the reader
    assert info.project == "acme/widgets"


def test_readers_keep_a_host_only_gitreview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file naming a host but no project reaches the resolvers intact.

    Discarding it would drop the push target and let the project be
    confirmed on that host by the lookup, then pushed under a host from
    the inputs. A file naming no host is still invalid.
    """
    repo = init_repo(tmp_path / "repo", default_branch="main")
    (repo.path / ".gitreview").write_text(
        "[gerrit]\nhost=gerrit.acme.org\nport=29418\n", encoding="utf-8"
    )
    orch = Orchestrator(workspace=repo.path)

    local = orch._read_gitreview(repo.path / ".gitreview")
    assert local is not None
    assert (local.host, local.project) == ("gerrit.acme.org", "")

    (repo.path / ".gitreview").write_text("[gerrit]\nport=29418\n")
    with pytest.raises(OrchestratorError, match="missing host"):
        orch._read_gitreview(repo.path / ".gitreview")

    # The remote reader keeps it too.
    monkeypatch.setattr(
        "github2gerrit.core.fetch_gitreview",
        lambda **_: GerritInfo(host="gerrit.acme.org", port=29418, project=""),
    )
    remote = orch._fetch_remote_gitreview(None, untrusted_tree=False)
    assert remote is not None
    assert (remote.host, remote.project) == ("gerrit.acme.org", "")


def test_derive_repo_names_from_gitreview(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo2", default_branch="main")
    write_gitreview(
        repo,
        host="gerrit.example.org",
        port=29418,
        project="releng/builder",
    )
    orch = Orchestrator(workspace=repo.path)
    gitreview = orch._read_gitreview(repo.path / ".gitreview")
    assert gitreview is not None
    names = orch._derive_repo_names(gitreview, _gh_ctx())
    assert names.project_gerrit == "releng/builder"
    # GitHub project name should be Gerrit path with '/' replaced by '-'
    assert names.project_github == "releng-builder"


def test_derive_repo_names_from_context_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("G2G_RESOLVE_PROJECT_VIA_GERRIT", raising=False)
    repo = init_repo(tmp_path / "repo3", default_branch="main")
    orch = Orchestrator(workspace=repo.path)
    # No .gitreview present; derive from GitHub repository owner/name
    gh = _gh_ctx(repository="acme/my-repo-name", owner="acme")
    names = orch._derive_repo_names(None, gh)
    # Fallback guesses '-' as '/' for the Gerrit path
    assert names.project_gerrit == "my/repo/name"
    assert names.project_github == "my-repo-name"


def test_derive_repo_names_asks_gerrit_when_opted_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no .gitreview, the guess is wrong for aai-aai-common.

    Opting in lets the tool ask Gerrit which reading exists, and the
    lister is only built when there is a host to ask.
    """
    monkeypatch.setenv("G2G_RESOLVE_PROJECT_VIA_GERRIT", "true")
    monkeypatch.setenv("GERRIT_SERVER", "gerrit.onap.org")

    class _Client:
        def get(self, path: str) -> dict[str, dict[str, str]]:
            assert path == "/projects/?p=aai"
            return {"aai/aai-common": {}, "aai/babel": {}}

    monkeypatch.setattr(
        "github2gerrit.gerrit_rest.build_client_for_host",
        lambda host, **_: _Client(),
    )
    orch = Orchestrator(workspace=init_repo(tmp_path / "r").path)
    names = orch._derive_repo_names(
        None, _gh_ctx(repository="onap/aai-aai-common", owner="onap")
    )
    assert names == RepoNames("aai/aai-common", "aai-aai-common", "gerrit")


def test_derive_repo_names_gerrit_lookup_needs_a_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Opted in but nothing to ask: fall back to the guess rather than
    # fail, since the guess is what every caller got before.
    monkeypatch.setenv("G2G_RESOLVE_PROJECT_VIA_GERRIT", "true")
    monkeypatch.delenv("GERRIT_SERVER", raising=False)
    orch = Orchestrator(workspace=init_repo(tmp_path / "r").path)
    names = orch._derive_repo_names(
        None, _gh_ctx(repository="onap/aai-aai-common", owner="onap")
    )
    assert names.project_gerrit == "aai/aai/common"


def test_derive_repo_names_gitreview_makes_gerrit_lookup_moot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An authoritative answer means no network call, even when opted in.
    monkeypatch.setenv("G2G_RESOLVE_PROJECT_VIA_GERRIT", "true")
    monkeypatch.setenv("GERRIT_SERVER", "gerrit.onap.org")

    asked: list[str] = []

    class _Client:
        def get(self, path: str) -> dict[str, object]:
            return {"aai/aai-common": {}}

    def _build(host: str, **_: object) -> _Client:
        asked.append(host)
        return _Client()

    monkeypatch.setattr(
        "github2gerrit.gerrit_rest.build_client_for_host", _build
    )
    repo = init_repo(tmp_path / "r", default_branch="main")
    write_gitreview(
        repo, host="gerrit.onap.org", port=29418, project="aai/aai-common"
    )
    orch = Orchestrator(workspace=repo.path)
    gitreview = orch._read_gitreview(repo.path / ".gitreview")
    assert gitreview is not None
    names = orch._derive_repo_names(gitreview, _gh_ctx())
    assert names.project_gerrit == "aai/aai-common"
    assert asked == []


def test_derive_repo_names_gerrit_lookup_honours_no_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # G2G_NO_GERRIT is documented as making no Gerrit calls at all, and
    # the CLI spells it G2G_DRYRUN_DISABLE_NETWORK before the
    # orchestrator runs. The lookup is a Gerrit call.
    monkeypatch.setenv("G2G_RESOLVE_PROJECT_VIA_GERRIT", "true")
    monkeypatch.setenv("GERRIT_SERVER", "gerrit.onap.org")
    monkeypatch.setenv("G2G_DRYRUN_DISABLE_NETWORK", "true")

    asked: list[str] = []

    class _Client:
        def get(self, path: str) -> dict[str, object]:
            return {"aai/aai-common": {}}

    def _build(host: str, **_: object) -> _Client:
        asked.append(host)
        return _Client()

    monkeypatch.setattr(
        "github2gerrit.gerrit_rest.build_client_for_host", _build
    )
    orch = Orchestrator(workspace=init_repo(tmp_path / "r").path)
    names = orch._derive_repo_names(
        None, _gh_ctx(repository="onap/aai-aai-common", owner="onap")
    )
    assert names.project_gerrit == "aai/aai/common"
    assert names.guessed is True
    assert asked == []


def _inputs_with_project(
    project: str, *, gerrit_server: str | None = None, dry_run: bool = False
) -> Inputs:
    base = _minimal_inputs(dry_run=dry_run)
    return replace(
        base,
        gerrit_project=project,
        gerrit_server=gerrit_server or base.gerrit_server,
    )


def test_explicit_project_input_outranks_gitreview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Operator intent wins, for both names and the push target.

    Resolved once: the topic is built from the same project the change
    is pushed to. Host and port still come from .gitreview.
    """
    # The project is explicit; the stand-in server is a derived value,
    # so the file's host and port still apply.
    monkeypatch.setenv(DERIVED_KEYS_ENV, "GERRIT_SERVER,GERRIT_SERVER_PORT")
    monkeypatch.setenv("G2G_DRYRUN_DISABLE_NETWORK", "true")
    repo = init_repo(tmp_path / "r", default_branch="main")
    write_gitreview(
        repo, host="gerrit.example.net", port=29420, project="apps/service"
    )
    orch = Orchestrator(workspace=repo.path)
    gitreview = orch._read_gitreview(repo.path / ".gitreview")
    inputs = _inputs_with_project("operator/said-so")

    names = orch._derive_repo_names(gitreview, _gh_ctx(), inputs)
    assert names == RepoNames(
        "operator/said-so", "operator-said-so", "explicit"
    )

    info = orch._resolve_gerrit_info(gitreview, inputs, names)
    assert (info.host, info.port, info.project) == (
        "gerrit.example.net",
        29420,
        "operator/said-so",
    )


def test_derived_project_input_does_not_outrank_gitreview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A value derivation exported through GERRIT_PROJECT is no input."""
    monkeypatch.setenv(
        DERIVED_KEYS_ENV, "GERRIT_PROJECT,GERRIT_SERVER,GERRIT_SERVER_PORT"
    )
    monkeypatch.setenv("G2G_DRYRUN_DISABLE_NETWORK", "true")
    repo = init_repo(tmp_path / "r", default_branch="main")
    write_gitreview(
        repo, host="gerrit.example.net", port=29420, project="apps/service"
    )
    orch = Orchestrator(workspace=repo.path)
    gitreview = orch._read_gitreview(repo.path / ".gitreview")
    inputs = _inputs_with_project("stale/guess")

    names = orch._derive_repo_names(gitreview, _gh_ctx(), inputs)
    assert names.project_gerrit == "apps/service"
    info = orch._resolve_gerrit_info(gitreview, inputs, names)
    assert info == gitreview


def test_gerrit_lookup_result_reaches_the_push_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Topic and push target come from the one resolution.

    Before, the lookup could settle aai/aai-common for the topic while
    the push took the earlier aai/aai/common guess from the inputs.
    """
    monkeypatch.setenv(DERIVED_KEYS_ENV, "GERRIT_PROJECT")
    monkeypatch.setenv("G2G_RESOLVE_PROJECT_VIA_GERRIT", "true")
    monkeypatch.setenv("GERRIT_SERVER", "gerrit.onap.org")
    monkeypatch.delenv("G2G_NO_GERRIT", raising=False)
    monkeypatch.delenv("G2G_DRYRUN_DISABLE_NETWORK", raising=False)

    class _Client:
        def get(self, path: str) -> dict[str, dict[str, str]]:
            return {"aai/aai-common": {}}

    monkeypatch.setattr(
        "github2gerrit.gerrit_rest.build_client_for_host",
        lambda host, **_: _Client(),
    )
    orch = Orchestrator(workspace=init_repo(tmp_path / "r").path)
    gh = _gh_ctx(repository="onap/aai-aai-common", owner="onap")
    inputs = _inputs_with_project(
        "aai/aai/common", gerrit_server="gerrit.onap.org"
    )

    names = orch._derive_repo_names(None, gh, inputs)
    assert names.project_gerrit == "aai/aai-common"

    monkeypatch.setenv("G2G_DRYRUN_DISABLE_NETWORK", "true")
    info = orch._resolve_gerrit_info(None, inputs, names)
    assert info.project == "aai/aai-common"


def test_derived_project_input_is_a_fallback_above_the_guess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A per-organization configuration value arrives derived. Nothing
    # confirms it, but somebody wrote it down, which beats reading the
    # hyphens blind.
    monkeypatch.setenv(DERIVED_KEYS_ENV, "GERRIT_PROJECT")
    monkeypatch.delenv("G2G_RESOLVE_PROJECT_VIA_GERRIT", raising=False)
    monkeypatch.setenv("G2G_DRYRUN_DISABLE_NETWORK", "true")
    orch = Orchestrator(workspace=init_repo(tmp_path / "r").path)
    gh = _gh_ctx(repository="acme/ci-management", owner="acme")
    inputs = _inputs_with_project("ci-management")

    names = orch._derive_repo_names(None, gh, inputs)
    assert names == RepoNames("ci-management", "ci-management", "fallback")
    assert orch._resolve_gerrit_info(None, inputs, names).project == (
        "ci-management"
    )


def test_guess_alone_is_not_pushed_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No .gitreview, no input, no fallback: the pre-existing rule that
    # a real run needs GERRIT_PROJECT still holds, and a dry run may
    # still proceed on the guess. A direct URL names the GitHub side
    # only and earns no exception.
    monkeypatch.delenv("G2G_RESOLVE_PROJECT_VIA_GERRIT", raising=False)
    monkeypatch.setenv("G2G_DRYRUN_DISABLE_NETWORK", "true")
    orch = Orchestrator(workspace=init_repo(tmp_path / "r").path)
    gh = _gh_ctx(repository="acme/my-repo-name", owner="acme")
    inputs = _inputs_with_project("")

    names = orch._derive_repo_names(None, gh, inputs)
    assert names.guessed is True
    for target_url in (None, "https://github.com/acme/my-repo-name/pull/1"):
        if target_url is None:
            monkeypatch.delenv("G2G_TARGET_URL", raising=False)
        else:
            monkeypatch.setenv("G2G_TARGET_URL", target_url)
        with pytest.raises(
            OrchestratorError, match="missing GERRIT_PROJECT"
        ) as exc:
            orch._resolve_gerrit_info(None, inputs, names)
        assert "'my-repo-name' is ambiguous ('my/repo/name'?)" in str(exc.value)

    dry = _inputs_with_project("", dry_run=True)
    assert orch._resolve_gerrit_info(None, dry, names).project == "my/repo/name"


def test_derivations_own_guess_is_not_laundered_into_a_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A guess exported through GERRIT_PROJECT stays a guess here.

    Early derivation exports its guess so cleanup queries have a
    project to scope by, and records it as such. Handing that value
    back as a confirmed fallback would let a real run push to an
    ambiguous path the push-time guard exists to refuse.
    """
    from github2gerrit.config import GUESSED_KEYS_ENV

    monkeypatch.setenv(DERIVED_KEYS_ENV, "GERRIT_PROJECT")
    monkeypatch.setenv(GUESSED_KEYS_ENV, "GERRIT_PROJECT")
    monkeypatch.delenv("G2G_RESOLVE_PROJECT_VIA_GERRIT", raising=False)
    monkeypatch.delenv("G2G_TARGET_URL", raising=False)
    monkeypatch.setenv("G2G_DRYRUN_DISABLE_NETWORK", "true")
    orch = Orchestrator(workspace=init_repo(tmp_path / "r").path)
    gh = _gh_ctx(repository="onap/aai-aai-common", owner="onap")
    inputs = _inputs_with_project("aai/aai/common")

    names = orch._derive_repo_names(None, gh, inputs)
    assert names.guessed is True
    with pytest.raises(OrchestratorError, match="missing GERRIT_PROJECT"):
        orch._resolve_gerrit_info(None, inputs, names)


def test_hyphen_free_name_is_not_a_guess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One reading only, so nothing to be ambiguous about; a run without
    # .gitreview on such a repository pushes to the same-named project
    # as it always has.
    monkeypatch.delenv("G2G_RESOLVE_PROJECT_VIA_GERRIT", raising=False)
    monkeypatch.setenv("G2G_DRYRUN_DISABLE_NETWORK", "true")
    orch = Orchestrator(workspace=init_repo(tmp_path / "r").path)
    gh = _gh_ctx(repository="lfit/sandbox", owner="lfit")
    inputs = _inputs_with_project("")

    names = orch._derive_repo_names(None, gh, inputs)
    assert names == RepoNames("sandbox", "sandbox", "name")
    assert orch._resolve_gerrit_info(None, inputs, names).project == "sandbox"


def test_host_only_gitreview_does_not_settle_the_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A .gitreview with connection details only leaves the question open.

    It must not switch off the server lookup, and it must not carry a
    guess past the push-time guard by being the object the connection
    info is built from. The lookup asks the file's host, since that is
    where the push would go.
    """
    monkeypatch.setenv("G2G_RESOLVE_PROJECT_VIA_GERRIT", "true")
    monkeypatch.delenv("GERRIT_SERVER", raising=False)
    monkeypatch.delenv("G2G_NO_GERRIT", raising=False)
    monkeypatch.delenv("G2G_DRYRUN_DISABLE_NETWORK", raising=False)
    # The stand-in inputs server is a derived value; the file's host
    # is the push target.
    monkeypatch.setenv(DERIVED_KEYS_ENV, "GERRIT_SERVER")
    asked: list[str] = []

    class _Client:
        def __init__(self, known: dict[str, object]) -> None:
            self.known = known

        def get(self, path: str) -> dict[str, object]:
            return self.known

    known: dict[str, object] = {}

    def _build(host: str, **_: object) -> _Client:
        asked.append(host)
        return _Client(known)

    monkeypatch.setattr(
        "github2gerrit.gerrit_rest.build_client_for_host", _build
    )
    orch = Orchestrator(workspace=init_repo(tmp_path / "r").path)
    gh = _gh_ctx(repository="onap/aai-aai-common", owner="onap")
    inputs = _inputs_with_project("")
    host_only = GerritInfo(host="gerrit.onap.org", port=29418, project="")

    # Server knows nothing: still a guess, and the host-only file does
    # not let it through.
    names = orch._derive_repo_names(host_only, gh, inputs)
    assert asked == ["gerrit.onap.org"]
    assert names.guessed is True
    monkeypatch.setenv("G2G_DRYRUN_DISABLE_NETWORK", "true")
    with pytest.raises(OrchestratorError, match="missing GERRIT_PROJECT"):
        orch._resolve_gerrit_info(host_only, inputs, names)
    dry = _inputs_with_project("", dry_run=True)
    info = orch._resolve_gerrit_info(host_only, dry, names)
    assert (info.host, info.project) == ("gerrit.onap.org", "aai/aai/common")

    # Server confirms one reading: the file's host, the server's project.
    # (Forget the empty listing the memo kept from the first phase.)
    from github2gerrit import project_names

    project_names._LISTINGS.clear()
    monkeypatch.delenv("G2G_DRYRUN_DISABLE_NETWORK", raising=False)
    known["aai/aai-common"] = {}
    names = orch._derive_repo_names(host_only, gh, inputs)
    assert names == RepoNames("aai/aai-common", "aai-aai-common", "gerrit")
    monkeypatch.setenv("G2G_DRYRUN_DISABLE_NETWORK", "true")
    info = orch._resolve_gerrit_info(host_only, inputs, names)
    assert (info.host, info.project) == ("gerrit.onap.org", "aai/aai-common")


def test_lookup_asks_the_host_the_push_will_go_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Confirmation on one server is worthless for a push to another.

    The lookup host follows the same order _resolve_gerrit_info uses
    for the push: the .gitreview host over an environment GERRIT_SERVER,
    and, with no file, the inputs' server even when nothing exported it
    to the environment.
    """
    monkeypatch.setenv("G2G_RESOLVE_PROJECT_VIA_GERRIT", "true")
    monkeypatch.delenv("G2G_NO_GERRIT", raising=False)
    monkeypatch.delenv("G2G_DRYRUN_DISABLE_NETWORK", raising=False)
    asked: list[str] = []

    class _Client:
        def get(self, path: str) -> dict[str, object]:
            return {}

    def _build(host: str, **_: object) -> _Client:
        asked.append(host)
        return _Client()

    monkeypatch.setattr(
        "github2gerrit.gerrit_rest.build_client_for_host", _build
    )
    orch = Orchestrator(workspace=init_repo(tmp_path / "r").path)
    gh = _gh_ctx(repository="onap/aai-aai-common", owner="onap")

    host_only = GerritInfo(host="gerrit.onap.org", port=29418, project="")

    # An explicit server outranks the file's host, at the push and so
    # here.
    monkeypatch.delenv(DERIVED_KEYS_ENV, raising=False)
    explicit = _inputs_with_project("", gerrit_server="gerrit.explicit.example")
    orch._derive_repo_names(host_only, gh, explicit)
    assert asked == ["gerrit.explicit.example"]

    # A derived server does not; the file's host is the push target,
    # whatever the environment says.
    asked.clear()
    monkeypatch.setenv(DERIVED_KEYS_ENV, "GERRIT_SERVER")
    monkeypatch.setenv("GERRIT_SERVER", "gerrit.elsewhere.example")
    orch._derive_repo_names(host_only, gh, _inputs_with_project(""))
    assert asked == ["gerrit.onap.org"]

    # No file: the inputs' server, even when nothing exported it.
    asked.clear()
    monkeypatch.delenv("GERRIT_SERVER", raising=False)
    inputs = _inputs_with_project(
        "", gerrit_server="gerrit.from-inputs.example"
    )
    orch._derive_repo_names(None, gh, inputs)
    assert asked == ["gerrit.from-inputs.example"]


def test_resolve_gerrit_info_prefers_gitreview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path / "repo4", default_branch="main")
    write_gitreview(
        repo,
        host="gerrit.example.net",
        port=29420,
        project="apps/service",
    )
    orch = Orchestrator(workspace=repo.path)
    gitreview = orch._read_gitreview(repo.path / ".gitreview")
    assert gitreview is not None
    gh = _gh_ctx(repository="org/service-repo", owner="org")
    names = orch._derive_repo_names(gitreview, gh)
    # Skip DNS validation — fake hostname is not resolvable
    monkeypatch.setenv("G2G_DRYRUN_DISABLE_NETWORK", "true")
    # The stand-in inputs server is a derived value.
    monkeypatch.setenv(DERIVED_KEYS_ENV, "GERRIT_SERVER,GERRIT_SERVER_PORT")
    info = orch._resolve_gerrit_info(gitreview, _minimal_inputs(), names)
    # Should return the gitreview values directly
    assert info.host == "gerrit.example.net"
    assert info.port == 29420
    assert info.project == "apps/service"


def test_ci_testing_does_not_read_gitreview_at_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mode ignores the file, so the file is not read.

    Reading and then discarding it would let a malformed local copy
    fail the run and a missing one trigger remote fetches, both in a
    mode documented to consult neither.
    """
    monkeypatch.setenv("G2G_DRYRUN_DISABLE_NETWORK", "true")
    monkeypatch.delenv("G2G_RESOLVE_PROJECT_VIA_GERRIT", raising=False)
    repo = init_repo(tmp_path / "r", default_branch="main")
    (repo.path / ".gitreview").write_text("not an ini file at all\n")
    orch = Orchestrator(workspace=repo.path)

    reads: list[Path] = []

    def _read(path: Path, gh: GitHubContext | None = None) -> GerritInfo:
        reads.append(path)
        raise AssertionError(".gitreview must not be read under CI_TESTING")

    monkeypatch.setattr(orch, "_read_gitreview", _read)
    inputs = replace(
        _inputs_with_project("explicit/project"),
        ci_testing=True,
        gerrit_server="gerrit.ci.example",
    )

    gerrit, names = orch._resolve_pipeline_targets(inputs, _gh_ctx())
    assert reads == []
    assert (gerrit.host, gerrit.project) == (
        "gerrit.ci.example",
        "explicit/project",
    )
    assert names.project_gerrit == "explicit/project"


def test_port_follows_its_own_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit port outranks the file's; an unset one yields to it.

    The action and the CLI pass no port when the caller gave none, so
    ``Inputs.gerrit_server_port == 0`` means unset and a set value is
    distinguishable from a default. 29418 applies only when nothing
    names a port.
    """
    monkeypatch.delenv(DERIVED_KEYS_ENV, raising=False)
    monkeypatch.setenv("G2G_DRYRUN_DISABLE_NETWORK", "true")
    repo = init_repo(tmp_path / "r", default_branch="main")
    write_gitreview(
        repo, host="gerrit.example.net", port=29420, project="apps/service"
    )
    orch = Orchestrator(workspace=repo.path)
    gitreview = orch._read_gitreview(repo.path / ".gitreview")
    names = orch._derive_repo_names(gitreview, _gh_ctx())

    # Unset port, derived server: the file's port.
    monkeypatch.setenv(DERIVED_KEYS_ENV, "GERRIT_SERVER")
    unset = replace(_minimal_inputs(), gerrit_server_port=0)
    assert orch._resolve_gerrit_info(gitreview, unset, names).port == 29420

    # Explicit port, derived server: the file's host, the operator's port.
    explicit_port = replace(_minimal_inputs(), gerrit_server_port=2222)
    info = orch._resolve_gerrit_info(gitreview, explicit_port, names)
    assert (info.host, info.port) == ("gerrit.example.net", 2222)

    # Explicit server, unset port, file present: the file's port still
    # applies, since the port has its own precedence.
    monkeypatch.delenv(DERIVED_KEYS_ENV, raising=False)
    explicit_host = replace(
        _minimal_inputs(),
        gerrit_server="gerrit.explicit.example",
        gerrit_server_port=0,
    )
    info = orch._resolve_gerrit_info(gitreview, explicit_host, names)
    assert (info.host, info.port) == ("gerrit.explicit.example", 29420)

    # Nothing names a port: the Gerrit default.
    info = orch._resolve_gerrit_info(None, explicit_host, names)
    assert info.port == 29418


def test_explicit_server_input_outranks_gitreview_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Operator intent wins for the host as it does for the project.

    The exported pair the close handler queries is then the pair the
    pipeline pushes to, which it was not while an explicit server lost
    to the file at the push but survived the export.
    """
    monkeypatch.delenv(DERIVED_KEYS_ENV, raising=False)
    monkeypatch.setenv("G2G_DRYRUN_DISABLE_NETWORK", "true")
    repo = init_repo(tmp_path / "r", default_branch="main")
    write_gitreview(
        repo, host="gerrit.example.net", port=29420, project="apps/service"
    )
    orch = Orchestrator(workspace=repo.path)
    gitreview = orch._read_gitreview(repo.path / ".gitreview")
    inputs = _inputs_with_project("", gerrit_server="gerrit.explicit.example")

    names = orch._derive_repo_names(gitreview, _gh_ctx(), inputs)
    info = orch._resolve_gerrit_info(gitreview, inputs, names)
    assert (info.host, info.port, info.project) == (
        "gerrit.explicit.example",
        29418,
        "apps/service",
    )


def test_resolve_gerrit_info_dry_run_uses_derived_project_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_repo(tmp_path / "repo5", default_branch="main")
    orch = Orchestrator(workspace=repo.path)
    gh = _gh_ctx(repository="team/reusable-action", owner="team")
    names = orch._derive_repo_names(None, gh)
    # Provide inputs with missing project but dry-run True to allow derivation
    inputs = _minimal_inputs(dry_run=True)
    inputs = Inputs(
        submit_single_commits=inputs.submit_single_commits,
        use_pr_as_commit=inputs.use_pr_as_commit,
        fetch_depth=inputs.fetch_depth,
        gerrit_known_hosts=inputs.gerrit_known_hosts,
        gerrit_ssh_privkey_g2g=inputs.gerrit_ssh_privkey_g2g,
        gerrit_ssh_user_g2g=inputs.gerrit_ssh_user_g2g,
        gerrit_ssh_user_g2g_email=inputs.gerrit_ssh_user_g2g_email,
        github_token=inputs.github_token,
        organization=inputs.organization,
        reviewers_email=inputs.reviewers_email,
        preserve_github_prs=inputs.preserve_github_prs,
        dry_run=True,
        normalise_commit=inputs.normalise_commit,
        gerrit_server=inputs.gerrit_server,
        gerrit_server_port=inputs.gerrit_server_port,
        gerrit_project="",  # Missing, should derive from repo name
        issue_id=inputs.issue_id,
        issue_id_lookup_json=inputs.issue_id_lookup_json,
        commit_rules_json="",
        allow_duplicates=inputs.allow_duplicates,
        ci_testing=inputs.ci_testing,
    )
    # Skip DNS validation — fake hostname is not resolvable
    monkeypatch.setenv("G2G_DRYRUN_DISABLE_NETWORK", "true")
    info = orch._resolve_gerrit_info(None, inputs, names)
    assert info.host == "gerrit.example.org"
    assert info.port == 29418
    # Project should be derived from RepoNames.project_gerrit
    assert info.project == names.project_gerrit


def test_dry_run_preflight_network_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Initialize a repo but do not require any network or remote
    repo = init_repo(tmp_path / "repo6", default_branch="main")
    orch = Orchestrator(workspace=repo.path)
    gh = _gh_ctx(repository="acme/system", owner="acme", pr_number=33)
    names = RepoNames(project_gerrit="acme/system", project_github="system")
    # Disable network within preflight to avoid DNS/HTTP/SSH probes
    monkeypatch.setenv("G2G_DRYRUN_DISABLE_NETWORK", "true")
    # Keep config path benign and prevent close/comment behavior from looking at
    # network
    monkeypatch.setenv("PRESERVE_GITHUB_PRS", "true")
    inputs = _minimal_inputs(dry_run=True)
    gerrit = GerritInfo(
        host="gerrit.acme.org", port=29418, project="acme/system"
    )
    # Should complete without raising exceptions
    orch._dry_run_preflight(gerrit=gerrit, inputs=inputs, gh=gh, repo=names)
