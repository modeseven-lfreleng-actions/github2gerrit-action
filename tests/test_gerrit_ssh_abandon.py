# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""
Tests for SSH-based Gerrit abandon (gerrit_ssh module) and the SSH-first
routing in gerrit_pr_closer._abandon_gerrit_change.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch

from github2gerrit import gerrit_ssh
from github2gerrit.gitutils import CommandError
from github2gerrit.gitutils import CommandResult


def _ok(stdout: str = "") -> CommandResult:
    return CommandResult(returncode=0, stdout=stdout, stderr="")


_QUERY_JSON = (
    '{"project":"netconf","number":123344,'
    '"currentPatchSet":{"number":2}}\n'
    '{"type":"stats","rowCount":1}\n'
)


class TestAbandonChangeViaSsh:
    """Tests for gerrit_ssh.abandon_change_via_ssh."""

    def test_returns_false_when_prerequisites_missing(self):
        assert (
            gerrit_ssh.abandon_change_via_ssh(
                host="",
                change_number="1",
                message="m",
                user="u",
                ssh_privkey="key",
            )
            is False
        )
        assert (
            gerrit_ssh.abandon_change_via_ssh(
                host="h",
                change_number="1",
                message="m",
                user="",
                ssh_privkey="key",
            )
            is False
        )
        assert (
            gerrit_ssh.abandon_change_via_ssh(
                host="h",
                change_number="1",
                message="m",
                user="u",
                ssh_privkey="",
            )
            is False
        )

    def test_mkdtemp_failure_returns_false(self):
        # Honor the "never raises" contract: a temp-dir failure must result
        # in a clean False so the caller can fall back to REST.
        with patch.object(
            gerrit_ssh.tempfile,
            "mkdtemp",
            side_effect=OSError("disk full"),
        ):
            result = gerrit_ssh.abandon_change_via_ssh(
                host="git.example.org",
                change_number="123344",
                message="m",
                user="u",
                ssh_privkey="key",
            )
        assert result is False

    def test_successful_abandon_queries_then_reviews(self):
        calls: list[list[str]] = []
        known_hosts_seen: list[str] = []

        def fake_run_cmd(cmd, **kwargs):
            calls.append(list(cmd))
            # Capture the known_hosts file content referenced in the argv.
            for tok in cmd:
                if isinstance(tok, str) and tok.startswith(
                    "UserKnownHostsFile="
                ):
                    p = tok.split("=", 1)[1]
                    with contextlib.suppress(OSError):
                        known_hosts_seen.append(
                            Path(p).read_text(encoding="utf-8")
                        )
            remote = cmd[-1]
            if remote.startswith("gerrit query"):
                return _ok(_QUERY_JSON)
            return _ok("")

        with patch.object(gerrit_ssh, "run_cmd", side_effect=fake_run_cmd):
            result = gerrit_ssh.abandon_change_via_ssh(
                host="git.example.org",
                change_number="123344",
                message="PR closed",
                user="gh2gerrit",
                ssh_privkey="PRIVKEY",
                known_hosts="git.example.org ssh-ed25519 AAAA",
                port=29418,
            )

        assert result is True
        # Two SSH invocations: query then review.
        assert len(calls) == 2
        query_remote = calls[0][-1]
        review_remote = calls[1][-1]
        assert query_remote.startswith("gerrit query")
        assert "change:123344" in query_remote
        assert review_remote.startswith("gerrit review --abandon")
        assert " -m " in review_remote
        # The resolved patch-set must be used as <change>,<patchset>.
        assert "123344,2" in review_remote
        # Connection target is user@host on the given port.
        assert "gh2gerrit@git.example.org" in calls[1]
        assert "29418" in calls[1]
        # known_hosts must be augmented with a bracketed [host]:port entry
        # so verification works on the non-default Gerrit SSH port.
        assert known_hosts_seen
        assert "[git.example.org]:29418" in known_hosts_seen[0]

    def test_no_known_hosts_uses_throwaway_file(self):
        calls: list[list[str]] = []

        def fake_run_cmd(cmd, **kwargs):
            calls.append(list(cmd))
            if cmd[-1].startswith("gerrit query"):
                return _ok(_QUERY_JSON)
            return _ok("")

        with patch.object(gerrit_ssh, "run_cmd", side_effect=fake_run_cmd):
            result = gerrit_ssh.abandon_change_via_ssh(
                host="git.example.org",
                change_number="123344",
                message="m",
                user="u",
                ssh_privkey="k",
                known_hosts=None,
            )

        assert result is True
        # Without known_hosts we must not mutate the default known_hosts.
        flat = " ".join(calls[0])
        assert "UserKnownHostsFile=/dev/null" in flat
        assert "StrictHostKeyChecking=accept-new" in flat

    def test_returns_false_when_patchset_unresolved(self):
        def fake_run_cmd(cmd, **kwargs):
            remote = cmd[-1]
            if remote.startswith("gerrit query"):
                return _ok('{"type":"stats","rowCount":0}\n')
            raise AssertionError("review should not run without a patch-set")

        with patch.object(gerrit_ssh, "run_cmd", side_effect=fake_run_cmd):
            result = gerrit_ssh.abandon_change_via_ssh(
                host="git.example.org",
                change_number="999",
                message="m",
                user="u",
                ssh_privkey="k",
            )
        assert result is False

    def test_returns_false_when_review_fails(self):
        def fake_run_cmd(cmd, **kwargs):
            remote = cmd[-1]
            if remote.startswith("gerrit query"):
                return _ok(_QUERY_JSON)
            raise CommandError("abandon failed", returncode=1)

        with patch.object(gerrit_ssh, "run_cmd", side_effect=fake_run_cmd):
            result = gerrit_ssh.abandon_change_via_ssh(
                host="git.example.org",
                change_number="123344",
                message="m",
                user="u",
                ssh_privkey="k",
                known_hosts="kh",
            )
        assert result is False

    def test_returns_false_when_query_fails(self):
        def fake_run_cmd(cmd, **kwargs):
            raise CommandError("query failed", returncode=255)

        with patch.object(gerrit_ssh, "run_cmd", side_effect=fake_run_cmd):
            result = gerrit_ssh.abandon_change_via_ssh(
                host="git.example.org",
                change_number="123344",
                message="m",
                user="u",
                ssh_privkey="k",
            )
        assert result is False


class TestAbandonRouting:
    """Tests for SSH-first routing in _abandon_gerrit_change."""

    def test_prefers_ssh_and_skips_rest(self, monkeypatch):
        from github2gerrit import gerrit_pr_closer

        monkeypatch.setenv("GERRIT_SSH_PRIVKEY_G2G", "PRIVKEY")
        monkeypatch.setenv("GERRIT_SSH_USER_G2G", "gh2gerrit")
        monkeypatch.setenv("GERRIT_SERVER", "git.example.org")
        monkeypatch.setenv("GERRIT_KNOWN_HOSTS", "kh")
        monkeypatch.setenv("GERRIT_SERVER_PORT", "29418")

        client = MagicMock()
        with patch(
            "github2gerrit.gerrit_ssh.abandon_change_via_ssh",
            return_value=True,
        ) as mock_ssh:
            gerrit_pr_closer._abandon_gerrit_change(client, "123344", "msg")

        mock_ssh.assert_called_once()
        client.post.assert_not_called()

    def test_falls_back_to_rest_when_ssh_unavailable(self, monkeypatch):
        from github2gerrit import gerrit_pr_closer

        monkeypatch.delenv("GERRIT_SSH_PRIVKEY_G2G", raising=False)
        monkeypatch.delenv("GERRIT_SSH_USER_G2G", raising=False)

        client = MagicMock()
        client.host = "git.example.org"
        gerrit_pr_closer._abandon_gerrit_change(client, "123344", "msg")
        client.post.assert_called_once()
        args, _kwargs = client.post.call_args
        assert "/changes/123344/abandon" in args[0]

    def test_falls_back_to_rest_when_ssh_fails(self, monkeypatch):
        from github2gerrit import gerrit_pr_closer

        monkeypatch.setenv("GERRIT_SSH_PRIVKEY_G2G", "PRIVKEY")
        monkeypatch.setenv("GERRIT_SSH_USER_G2G", "gh2gerrit")
        monkeypatch.setenv("GERRIT_SERVER", "git.example.org")

        client = MagicMock()
        with patch(
            "github2gerrit.gerrit_ssh.abandon_change_via_ssh",
            return_value=False,
        ):
            gerrit_pr_closer._abandon_gerrit_change(client, "123344", "msg")
        client.post.assert_called_once()
