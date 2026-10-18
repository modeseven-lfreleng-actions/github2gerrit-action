# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""
Tests holding the mypy pre-commit hook to the locked environment.

The hook runs mypy through ``uv run --locked``, so mypy and every stub
it reads come from ``uv.lock`` and nothing else.  An isolated hook
environment with its own pins would be a second source of truth that
drifts each time Dependabot moves the lockfile (#493).

These tests check the hook's shape only.  They deliberately do not
compare anything against ``uv.lock``: a pull request is tested as
merged into ``main``, so such a comparison reads ``main``'s lockfile
and fails a branch for a dependency that moved elsewhere (#433).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
PRE_COMMIT_CONFIG = REPO_ROOT / ".pre-commit-config.yaml"

MYPY_HOOK_ID = "mypy"
MYPY_ENTRY = "uv run --locked mypy"
MYPY_MIRROR_URL = "https://github.com/pre-commit/mirrors-mypy"
PIN_SYNC_HOOK_ID = "sync-mypy-pins"

Hook = dict[str, object]


# Guard failures below use `assert` or `raise`, not pytest.fail: the
# basedpyright hook runs without pytest installed, where pytest.fail
# does not read as NoReturn and the isinstance guards stop narrowing.
def _load_config() -> dict[str, object]:
    """Return the parsed pre-commit configuration."""
    document = yaml.safe_load(PRE_COMMIT_CONFIG.read_text(encoding="utf-8"))
    assert isinstance(document, dict), (
        f"{PRE_COMMIT_CONFIG.name} is not a mapping"
    )
    return document


def _iter_hooks(config: dict[str, object]) -> list[tuple[str, Hook]]:
    """Return every hook in *config*, paired with its repository URL."""
    repos = config.get("repos")
    assert isinstance(repos, list), (
        f"{PRE_COMMIT_CONFIG.name} declares no 'repos' list"
    )
    found: list[tuple[str, Hook]] = []
    for repo in repos:
        if not isinstance(repo, dict):
            continue
        hooks = repo.get("hooks")
        if not isinstance(hooks, list):
            continue
        found.extend(
            (str(repo.get("repo")), hook)
            for hook in hooks
            if isinstance(hook, dict)
        )
    return found


@pytest.fixture(scope="module")
def config() -> dict[str, object]:
    """Provide the parsed pre-commit configuration."""
    return _load_config()


@pytest.fixture(scope="module")
def mypy_hook(config) -> tuple[str, Hook]:
    """Provide the single mypy hook and the repository declaring it."""
    matches = [
        (url, hook)
        for url, hook in _iter_hooks(config)
        if hook.get("id") == MYPY_HOOK_ID
    ]
    assert len(matches) == 1, (
        f"{PRE_COMMIT_CONFIG.name} must declare exactly one "
        f"'{MYPY_HOOK_ID}' hook, found {len(matches)}"
    )
    return matches[0]


class TestPrecommitMypyHook:
    """Check that mypy runs from the project's locked environment."""

    def test_hook_is_local_uv_run(self, mypy_hook):
        """The hook runs the locked mypy, not an isolated environment."""
        url, hook = mypy_hook
        assert url == "local", (
            f"the '{MYPY_HOOK_ID}' hook must be a local hook, not one "
            f"from {url}"
        )
        assert hook.get("language") == "system", (
            f"the '{MYPY_HOOK_ID}' hook must use language: system so it "
            "runs in the uv project environment"
        )
        assert hook.get("entry") == MYPY_ENTRY, (
            f"the '{MYPY_HOOK_ID}' hook entry must be '{MYPY_ENTRY}'"
        )

    def test_hook_carries_no_pins(self, config, mypy_hook):
        """No dependency list exists that could drift from uv.lock."""
        _, hook = mypy_hook
        assert "additional_dependencies" not in hook, (
            f"the '{MYPY_HOOK_ID}' hook must take its dependencies from "
            "uv.lock, not additional_dependencies"
        )
        mirrors = [
            url for url, _ in _iter_hooks(config) if url == MYPY_MIRROR_URL
        ]
        assert not mirrors, (
            f"{PRE_COMMIT_CONFIG.name} still references {MYPY_MIRROR_URL}"
        )

    def test_hook_checks_fixed_targets(self, mypy_hook):
        """Local runs and CI check the same set of paths."""
        _, hook = mypy_hook
        assert hook.get("pass_filenames") is False, (
            f"the '{MYPY_HOOK_ID}' hook must set pass_filenames: false"
        )
        assert hook.get("require_serial") is True, (
            f"the '{MYPY_HOOK_ID}' hook must set require_serial: true"
        )
        args = hook.get("args")
        assert isinstance(args, list), (
            f"the '{MYPY_HOOK_ID}' hook must name its targets in args"
        )
        targets = [str(arg) for arg in args if not str(arg).startswith("-")]
        assert targets, f"the '{MYPY_HOOK_ID}' hook names no targets"
        missing = [t for t in targets if not (REPO_ROOT / t).exists()]
        assert not missing, (
            f"the '{MYPY_HOOK_ID}' hook names targets that do not exist: "
            + ", ".join(missing)
        )

    def test_hook_is_skipped_on_precommit_ci(self, config):
        """pre-commit.ci runs offline and cannot build the uv env."""
        ci = config.get("ci")
        assert isinstance(ci, dict), (
            f"{PRE_COMMIT_CONFIG.name} declares no 'ci' section"
        )
        skip = ci.get("skip")
        assert isinstance(skip, list), (
            f"{PRE_COMMIT_CONFIG.name} declares no 'ci.skip' list"
        )
        assert MYPY_HOOK_ID in skip, (
            f"'{MYPY_HOOK_ID}' must be listed under ci.skip"
        )

    def test_no_pin_sync_hook(self, config):
        """The pin-sync hook has nothing left to synchronise."""
        ids = [hook.get("id") for _, hook in _iter_hooks(config)]
        assert PIN_SYNC_HOOK_ID not in ids, (
            f"{PRE_COMMIT_CONFIG.name} still declares '{PIN_SYNC_HOOK_ID}'"
        )
