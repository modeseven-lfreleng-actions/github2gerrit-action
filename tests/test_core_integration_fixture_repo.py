# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from github2gerrit.core import GerritInfo
from github2gerrit.core import Orchestrator
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
    assert names == RepoNames("aai/aai-common", "aai-aai-common")


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

    def _never(host: str, **_: object) -> None:
        raise AssertionError("Gerrit must not be consulted")

    monkeypatch.setattr(
        "github2gerrit.gerrit_rest.build_client_for_host", _never
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
    info = orch._resolve_gerrit_info(gitreview, _minimal_inputs(), names)
    # Should return the gitreview values directly
    assert info.host == "gerrit.example.net"
    assert info.port == 29420
    assert info.project == "apps/service"


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
