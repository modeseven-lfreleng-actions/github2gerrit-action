# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation
"""
Tests holding the mypy pre-commit hook's pins to ``uv.lock``.

The ``mirrors-mypy`` hook runs in an isolated environment built from
PyPI, not from ``uv.lock``, so nothing in pre-commit itself ties the two
together.  Left to resolve freely the hook reports on a dependency set
no developer is running, and the results diverge from a local ``uv run
mypy`` for reasons the diff never shows.

These tests close that gap from both sides: the hook must pin every
dependency exactly, and each pinned version must be the one the
lockfile records.  Changing either file alone fails here.
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
PRE_COMMIT_CONFIG = REPO_ROOT / ".pre-commit-config.yaml"
UV_LOCK = REPO_ROOT / "uv.lock"

MYPY_MIRROR_URL = "https://github.com/pre-commit/mirrors-mypy"
MYPY_HOOK_ID = "mypy"

# The paths the import scan walks.  Deliberately expressed as prefixes
# rather than as a copy of the hook's regex: a copy is one more literal
# free to drift from the hook, which is the failure this file exists to
# prevent.  A test instead selects the tracked Python files two ways --
# by these prefixes and by the hook's own pattern -- and requires the
# two selections to be the same set, so neither can widen without the
# other.
_SCANNED_DIRECTORIES = ("src", "scripts", "tests")
_SCANNED_FILES = ("sitecustomize.py",)

# An exact pin and nothing else: no ranges, no markers, no extras.
EXACT_PIN = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[A-Za-z0-9._!+-]+)$"
)


def _canonical(name: str) -> str:
    """Return the PEP 503 canonical form of a package name."""
    return re.sub(r"[-_.]+", "-", name).lower()


# Guard failures below use `assert` or `raise`, not pytest.fail. All
# three end the test, but only the first two say so unconditionally to a
# type checker: pytest.fail is NoReturn according to pytest's own
# annotations, and the basedpyright pre-commit hook runs in an
# environment without pytest installed, where the call looks ordinary
# and every isinstance guard before it stops narrowing. That difference
# is invisible locally, where the project virtualenv supplies pytest.
def _load_hook_field(field: str) -> object:
    """Return one field of the mirrors-mypy hook."""
    document = yaml.safe_load(PRE_COMMIT_CONFIG.read_text(encoding="utf-8"))
    repos = document.get("repos") if isinstance(document, dict) else None
    assert isinstance(repos, list), (
        f"{PRE_COMMIT_CONFIG.name} declares no 'repos' list"
    )

    for repo in repos:
        if not isinstance(repo, dict) or repo.get("repo") != MYPY_MIRROR_URL:
            continue
        hooks = repo.get("hooks")
        if not isinstance(hooks, list):
            continue
        for hook in hooks:
            if not isinstance(hook, dict):
                continue
            if hook.get("id") != MYPY_HOOK_ID:
                continue
            if field not in hook:
                raise AssertionError(
                    f"the {MYPY_HOOK_ID} hook in {PRE_COMMIT_CONFIG.name} "
                    f"declares no {field}"
                )
            return hook[field]

    raise AssertionError(
        f"{PRE_COMMIT_CONFIG.name} has no {MYPY_HOOK_ID} hook under "
        f"{MYPY_MIRROR_URL}"
    )


def _load_hook_dependencies() -> list[str]:
    """Return ``additional_dependencies`` of the mirrors-mypy hook."""
    dependencies = _load_hook_field("additional_dependencies")
    assert isinstance(dependencies, list), (
        f"the {MYPY_HOOK_ID} hook in {PRE_COMMIT_CONFIG.name} "
        "declares no additional_dependencies list"
    )
    return [str(entry) for entry in dependencies]


def _load_locked_versions() -> dict[str, set[str]]:
    """Return every version ``uv.lock`` records, keyed by package."""
    with UV_LOCK.open("rb") as handle:
        document = tomllib.load(handle)
    packages = document.get("package")
    assert isinstance(packages, list), (
        f"{UV_LOCK.name} declares no [[package]] entries"
    )

    locked: dict[str, set[str]] = {}
    for package in packages:
        if not isinstance(package, dict):
            continue
        name = package.get("name")
        version = package.get("version")
        # A resolution may fork a package across markers, and the local
        # project itself is listed without a version.
        if isinstance(name, str) and isinstance(version, str):
            locked.setdefault(_canonical(name), set()).add(version)
    return locked


@pytest.fixture(scope="module")
def hook_dependencies() -> list[str]:
    """Provide the mypy hook's declared dependencies."""
    return _load_hook_dependencies()


@pytest.fixture(scope="module")
def locked_versions() -> dict[str, set[str]]:
    """Provide the versions recorded in the lockfile."""
    return _load_locked_versions()


class TestPrecommitMypyPins:
    """Check the mypy hook environment against the lockfile."""

    def test_hook_declares_dependencies(self, hook_dependencies):
        """The hook lists dependencies, so the checks below have input."""
        assert hook_dependencies, (
            f"the {MYPY_HOOK_ID} hook in {PRE_COMMIT_CONFIG.name} lists no "
            "additional_dependencies; without them mypy runs against an "
            "environment missing the imports it is meant to resolve"
        )

    def test_every_dependency_is_pinned_exactly(self, hook_dependencies):
        """Every entry pins one version, so resolution cannot drift."""
        loose = [
            entry
            for entry in hook_dependencies
            if EXACT_PIN.fullmatch(entry) is None
        ]
        assert not loose, (
            "the {hook} hook in {config} must pin every dependency "
            "exactly, because its environment resolves from PyPI rather "
            "than {lock}; a floor such as '>=' still installs whatever is "
            "newest when the environment is built. Not exact pins: "
            "{loose}".format(
                hook=MYPY_HOOK_ID,
                config=PRE_COMMIT_CONFIG.name,
                lock=UV_LOCK.name,
                loose=", ".join(loose),
            )
        )

    def test_pins_match_uv_lock(self, hook_dependencies, locked_versions):
        """Each pinned version is the one the lockfile resolves to."""
        problems: list[str] = []
        for entry in hook_dependencies:
            match = EXACT_PIN.fullmatch(entry)
            if match is None:
                problems.append(f"{entry}: not an exact '==' pin")
                continue
            name = _canonical(match.group("name"))
            pinned = match.group("version")
            recorded = locked_versions.get(name)
            if recorded is None:
                problems.append(
                    f"{name}: pinned at {pinned}, absent from {UV_LOCK.name}"
                )
            elif pinned not in recorded:
                problems.append(
                    f"{name}: {PRE_COMMIT_CONFIG.name} pins {pinned}, "
                    f"{UV_LOCK.name} records {', '.join(sorted(recorded))}"
                )

        assert not problems, (
            "the {hook} hook pins versions {lock} does not resolve to, so "
            "the hook checks code against dependencies nobody runs. Update "
            "{config} and {lock} together:\n  {problems}".format(
                hook=MYPY_HOOK_ID,
                config=PRE_COMMIT_CONFIG.name,
                lock=UV_LOCK.name,
                problems="\n  ".join(problems),
            )
        )


# Top-level import names that do not match their distribution name, and
# the distribution the hook must therefore list for them.
_IMPORT_TO_DISTRIBUTION = {
    "yaml": "types-pyyaml",
    "_pytest": "pytest",
}

# Imported by src/ but deliberately absent from the hook. pygerrit2 is an
# optional runtime dependency and pyproject.toml already gives it
# ignore_missing_imports, so mypy never reads its types and its version
# cannot change the verdict.
_DELIBERATELY_ABSENT = {"pygerrit2"}

# Pins the import scan cannot derive, because nothing under the checked
# paths imports them, and which must therefore be required by name.
# Without this the completeness test happily passes after either is
# deleted, which is how the checker's own pin could vanish unnoticed.
_REQUIRED_REGARDLESS = {
    # The checker. Deleting this pin returns mypy's version to the
    # mirror's rev alone, which nothing ties to uv.lock.
    "mypy",
    # Stubs for requests, which arrives through responses rather than
    # being imported directly. Verified not load-bearing today -- mypy
    # passes with it uninstalled, in the hook environment as well as
    # locally -- and kept so that a future direct import of requests
    # does not silently find the stubs already gone.
    "types-requests",
}


def _tracked_python_files() -> list[str]:
    """Return every tracked ``.py`` path, as pre-commit would see them."""
    result = subprocess.run(
        ["git", "ls-files", "*.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def _scan_selects(path: str) -> bool:
    """Whether the import scan walks *path*."""
    return path.split("/")[0] in _SCANNED_DIRECTORIES or path in _SCANNED_FILES


def _third_party_imports() -> set[str]:
    """Return distributions imported by the paths the hook checks."""
    import ast
    import sys

    first_party = {"github2gerrit", "tests", "fixtures", "conftest"}
    names: set[str] = set()
    for relative in _tracked_python_files():
        if not _scan_selects(relative):
            continue
        tree = ast.parse((REPO_ROOT / relative).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                roots = [(node.module or "").split(".")[0]]
            else:
                continue
            for root in roots:
                if not root or root in sys.stdlib_module_names:
                    continue
                if root in first_party:
                    continue
                names.add(_IMPORT_TO_DISTRIBUTION.get(root, root))
    return {_canonical(n) for n in names}


def test_import_scan_covers_the_same_paths_as_the_hook() -> None:
    """The scan walks exactly the files the hook type-checks.

    The completeness test below is only as good as the paths it looks
    at, so this pins the two together by *outcome* rather than by
    comparing the hook's pattern against a copy of it.  Both sides
    select from the same list of tracked Python files -- one by the
    prefixes the scan uses, the other by the hook's own regex -- and
    must agree.  Widening either alone leaves a file selected by one
    and not the other, and this fails naming it.
    """
    pattern = re.compile(str(_load_hook_field("files")))
    tracked = _tracked_python_files()
    assert tracked, "git ls-files found no Python files; the scan is broken"

    scanned = {path for path in tracked if _scan_selects(path)}
    checked = {path for path in tracked if pattern.search(path)}

    assert scanned == checked, (
        f"the import scan and the {MYPY_HOOK_ID} hook disagree about which "
        "files are in scope, so the completeness test below no longer "
        "covers everything the hook checks.\n"
        f"  checked by the hook, not scanned: {sorted(checked - scanned)}\n"
        f"  scanned, not checked by the hook: {sorted(scanned - checked)}"
    )


def test_hook_covers_every_imported_third_party_package(
    hook_dependencies: list[str],
) -> None:
    """The hook lists everything mypy can actually read types from.

    Exact pins on the direct entries do not freeze the resolved closure:
    pip still chooses transitive versions when the environment is built.
    That only matters for a package whose types mypy reads, which means
    one the checked files import -- a transitive package nobody imports
    cannot change the verdict, so pinning the whole closure would
    duplicate the lockfile to no effect.

    This asserts the narrower property that does matter: every
    third-party package imported under a path the hook checks is pinned
    here, so none of them is left to arrive at whatever version a
    dependency happens to pull in. ``rich`` was exactly that case --
    imported by src/, absent from this list, and supplied transitively
    by typer.
    """
    pinned = {
        _canonical(m.group("name"))
        for entry in hook_dependencies
        if (m := EXACT_PIN.fullmatch(entry)) is not None
    }
    imported = _third_party_imports()

    assert imported, "found no third-party imports; the scan is broken"

    required = (imported | _REQUIRED_REGARDLESS) - _DELIBERATELY_ABSENT
    missing = sorted(required - pinned)
    assert not missing, (
        f"required by the {MYPY_HOOK_ID} hook but not pinned there, so "
        f"mypy reads whatever version a transitive dependency supplies "
        f"or the mirror's rev decides: {', '.join(missing)}"
    )
