# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""Regression test for process-wide cache isolation between tests.

``gerrit_urls._BASE_PATH_CACHE`` memoises expensive lookups in
module-level state that outlives an individual test, and
``tests/conftest.py`` clears it around every test via the
``reset_gerrit_base_path_cache`` autouse fixture.

The two tests below are a deliberate pair: the polluter populates the
cache for the shared host, and the victim asserts a later, independent
test sees nothing of it.  What that proves is the fixture's effect, and
the fixture runs *between* tests, so the pair cannot be folded into a
single two-phase test -- with no test boundary between the phases the
fixture would never run, and the victim phase would read the polluted
cache.

The pairing therefore depends on the polluter running first.  Rather
than trust the file layout, the polluter records that it ran and the
victim asserts the record before anything else, so a reordering of this
file or a test-shuffling plugin makes the victim fail loudly instead of
passing without testing anything.

``ssh_config_parser`` once had pairs here too, on the grounds that it
keeps no process-wide state.  That property is covered without any
ordering dependency by ``TestDerivationReflectsItsInputs`` in
``tests/unit/test_ssh_config_parser.py``, whose tests change one input
and assert the derived credentials change with it, so the pairs here
were redundant and have gone.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from github2gerrit import gerrit_urls as urls_mod
from github2gerrit.gerrit_urls import create_gerrit_url_builder


# The fixture host shared by roughly twenty test modules, and therefore
# the one most likely to carry a leaked base path.
_SHARED_HOST = "gerrit.example.org"

# Set by the polluter once it has left an entry in the cache.  The
# victim's guard reads it; nothing else should.
_POLLUTER_RECORDED_BASE_PATH = False


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
    global _POLLUTER_RECORDED_BASE_PATH

    monkeypatch.delenv("GERRIT_HTTP_BASE_PATH", raising=False)

    def decide(url: str) -> _FakeResp | BaseException:
        if url.endswith("/dashboard/self"):
            return _FakeResp(302, {"Location": "/r/dashboard/self"})
        return _FakeResp(404, {})

    _install_opener(monkeypatch, decide)

    builder = create_gerrit_url_builder(_SHARED_HOST)

    assert builder.base_path == "r"
    assert urls_mod._BASE_PATH_CACHE[_SHARED_HOST] == "r"

    _POLLUTER_RECORDED_BASE_PATH = True


def test_base_path_cache_does_not_leak_into_later_test(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later, independent build for the same host sees no base path.

    Discovery here cannot reach the server, so the honest answer is an
    empty base path.  Without the autouse reset the builder short
    circuits on the leaked ``'r'`` from the previous test and never
    probes at all, yielding ``/r/`` URLs.
    """
    assert _POLLUTER_RECORDED_BASE_PATH, (
        "this test must run after "
        "test_base_path_cache_polluter_records_r_for_shared_host, which "
        "is what puts an entry in the cache for it to find gone.  Run "
        "first, alone, or with the pair reordered, its assertions below "
        "hold trivially and test nothing."
    )
    assert _SHARED_HOST not in urls_mod._BASE_PATH_CACHE, (
        "the reset_gerrit_base_path_cache autouse fixture in "
        "tests/conftest.py did not clear the entry the polluter left"
    )

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
