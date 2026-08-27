# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""Regression tests for process-wide cache isolation between tests.

``gerrit_urls._BASE_PATH_CACHE`` memoises expensive lookups in
module-level state that outlives an individual test, and
``tests/conftest.py`` clears it around every test via the
``reset_gerrit_base_path_cache`` autouse fixture.

``ssh_config_parser`` needs no such fixture.  It keeps no process-wide
state at all: credential derivation reads ``G2G_RESPECT_USER_SSH``, the
SSH config and the git identity on every call.  The pairs below hold
that to account: each polluter populates whatever state exists with a
value the victim must not see, and each victim asserts the answer it
would get from a clean process.  Tests within a module run in
definition order, so the pairing is deterministic.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from github2gerrit import gerrit_urls as urls_mod
from github2gerrit import ssh_config_parser as ssh_mod
from github2gerrit.gerrit_urls import create_gerrit_url_builder


# The fixture host shared by roughly twenty test modules, and therefore
# the one most likely to carry a leaked base path.
_SHARED_HOST = "gerrit.example.org"


class _FakeResp:
    def __init__(self, code: int, headers: dict[str, str]) -> None:
        self.status = code
        self.headers = headers

    def getcode(self) -> int:
        return self.status


class _FakeOpener:
    def __init__(
        self, decide: Callable[[str], _FakeResp | BaseException]
    ) -> None:
        self._decide = decide
        self.addheaders: list[tuple[str, str]] = []

    def open(self, url: str, timeout: float | None = None) -> _FakeResp:
        result = self._decide(url)
        if isinstance(result, BaseException):
            raise result
        return result


def _install_opener(
    monkeypatch: pytest.MonkeyPatch,
    decide: Callable[[str], _FakeResp | BaseException],
) -> None:
    opener = _FakeOpener(decide)

    def _build(*_args: Any, **_kwargs: Any) -> _FakeOpener:
        return opener

    monkeypatch.setattr(
        "github2gerrit.gerrit_urls.urllib.request.build_opener",
        _build,
        raising=True,
    )


def test_base_path_cache_polluter_records_r_for_shared_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Populate the base-path cache with 'r' for the shared host.

    This mirrors what the base-path discovery tests do: a 302 whose
    Location carries a ``/r/`` prefix teaches the module that the host
    serves Gerrit under that base path, for the rest of the process.
    """
    monkeypatch.delenv("GERRIT_HTTP_BASE_PATH", raising=False)

    def decide(url: str) -> _FakeResp | BaseException:
        if url.endswith("/dashboard/self"):
            return _FakeResp(302, {"Location": "/r/dashboard/self"})
        return _FakeResp(404, {})

    _install_opener(monkeypatch, decide)

    builder = create_gerrit_url_builder(_SHARED_HOST)

    assert builder.base_path == "r"
    assert urls_mod._BASE_PATH_CACHE[_SHARED_HOST] == "r"


def test_base_path_cache_does_not_leak_into_later_test(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later, independent build for the same host sees no base path.

    Discovery here cannot reach the server, so the honest answer is an
    empty base path.  Without the autouse reset the builder short
    circuits on the leaked ``'r'`` from the previous test and never
    probes at all, yielding ``/r/`` URLs.
    """
    monkeypatch.delenv("GERRIT_HTTP_BASE_PATH", raising=False)

    probed: list[str] = []

    def decide(url: str) -> _FakeResp | BaseException:
        probed.append(url)
        return OSError("unreachable")

    _install_opener(monkeypatch, decide)

    builder = create_gerrit_url_builder(_SHARED_HOST)

    assert probed, "discovery short-circuited on a leaked cache entry"
    assert builder.base_path == ""
    assert (
        builder.change_url("releng/builder", 12345)
        == "https://gerrit.example.org/c/releng/builder/+/12345"
    )


def test_credential_cache_polluter_records_ssh_derived_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Derive credentials from a user-SSH-respecting environment."""
    monkeypatch.setenv("G2G_RESPECT_USER_SSH", "true")
    monkeypatch.setattr(
        ssh_mod,
        "get_ssh_user_for_gerrit",
        lambda _host, _port=29418: "sshuser",
        raising=True,
    )
    monkeypatch.setattr(
        ssh_mod,
        "get_git_user_email",
        lambda: "dev@example.org",
        raising=True,
    )

    user, email = ssh_mod.derive_gerrit_credentials(_SHARED_HOST, "leakorg")

    assert user == "sshuser"
    assert email == "dev@example.org"
    assert ssh_mod._get_respect_user_ssh_setting() is True


def test_respect_user_ssh_setting_does_not_leak_into_later_test(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The setting reflects this test's environment, not the last one.

    ``_get_respect_user_ssh_setting`` takes no arguments, so any memo
    over it would have exactly one slot that every test in the suite
    collides on, over an environment variable tests monkeypatch freely.
    It reads the environment on every call instead, so the answer here
    is this test's own -- with no fixture clearing anything in between.
    """
    monkeypatch.delenv("G2G_RESPECT_USER_SSH", raising=False)

    assert ssh_mod._get_respect_user_ssh_setting() is False


def test_git_user_email_cache_does_not_leak_into_later_test(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The git email reflects this test's git identity.

    ``_read_git_user_email`` is the same shape as
    ``_get_respect_user_ssh_setting``: zero arguments, so a memo would
    give the whole suite a single shared slot.  What it reads is the
    output of ``git config user.email``, which ``isolate_git_environment``
    and individual tests both take pains to control, so it is read on
    every call and the polluter above cannot reach this test.
    """
    monkeypatch.setattr(
        ssh_mod,
        "get_git_user_email",
        lambda: "victim@example.org",
        raising=True,
    )

    assert ssh_mod._read_git_user_email() == "victim@example.org"


def test_credential_cache_does_not_leak_into_later_test(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same host and organisation derive fallback credentials.

    The polluter above derived personal credentials for this very host,
    organisation and port.  Were the derivation memoised on those three
    arguments its answer would be replayed here, even though it was
    computed from an environment this test does not share.  Deriving
    afresh, this test sees its own empty environment and falls back to
    the organisation service account -- no cache clearing required.
    """
    monkeypatch.delenv("G2G_RESPECT_USER_SSH", raising=False)

    user, email = ssh_mod.derive_gerrit_credentials(_SHARED_HOST, "leakorg")

    assert user == "leakorg.gh2gerrit"
    assert email == "releng+leakorg-gh2gerrit@linuxfoundation.org"
