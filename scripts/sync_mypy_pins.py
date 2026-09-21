#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Keep the mypy pre-commit hook's pins equal to what ``uv.lock`` records.

The ``mirrors-mypy`` hook runs in an isolated environment built from
PyPI, so the exact pins in its ``additional_dependencies`` are the only
thing tying it to the locked dependency set.  Nothing in pre-commit
updates them when the lockfile moves.  This script does: it reads the
version ``uv.lock`` records for each pinned package and rewrites the
pin in place, touching nothing else in the file so its comments
survive.

Run as a pre-commit hook it is deterministic and branch-local: both
files it compares come from the same checkout.  A pull request that
moves the lockfile fails the hook on that pull request, and ``prek run
sync-mypy-pins --all-files`` brings the pins back into step.  The
``--all-files`` matters: without it pre-commit selects staged files
only, and neither ``uv.lock`` nor the config is staged on a clean
checkout or right after ``uv lock --upgrade``, so the hook would not be
selected at all.  ``--check`` reports drift without writing, for
callers that want a verdict rather than a fix.

Only the standard library is used, so the hook runs anywhere pre-commit
does, including sandboxes without network access.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path


MYPY_MIRROR_URL = "https://github.com/pre-commit/mirrors-mypy"

_REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = _REPO_ROOT / ".pre-commit-config.yaml"
DEFAULT_LOCK = _REPO_ROOT / "uv.lock"

_PIN = re.compile(
    r"^(?P<indent>\s*-\s+)(?P<quote>[\"']?)"
    r"(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)=="
    r"(?P<version>[A-Za-z0-9._!+-]+)(?P=quote)(?P<rest>\s*(?:#.*)?)$"
)
"""An exact pin as a YAML list item, quoted or bare.

YAML decodes ``- "click==8.5.0"`` and ``- click==8.5.0`` to the same
string, so both are valid spellings and both must be recognised; a
quoted pin that fell outside the match would end the block early and
leave every later pin stale while the hook reported success.  The
quote, when present, is kept on rewrite.
"""


def canonical(name: str) -> str:
    """Return the PEP 503 canonical form of a package name."""
    return re.sub(r"[-_.]+", "-", name).lower()


def locked_versions(lock_path: Path) -> dict[str, set[str]]:
    """Return every version the lockfile records, keyed by package.

    A resolution may fork a package across environment markers, so a
    package can carry more than one version; the caller decides how to
    read that.
    """
    with lock_path.open("rb") as handle:
        document = tomllib.load(handle)
    packages = document.get("package")
    if not isinstance(packages, list):
        msg = f"{lock_path} declares no [[package]] entries"
        raise SystemExit(msg)
    locked: dict[str, set[str]] = {}
    for package in packages:
        if not isinstance(package, dict):
            continue
        name = package.get("name")
        version = package.get("version")
        if isinstance(name, str) and isinstance(version, str):
            locked.setdefault(canonical(name), set()).add(version)
    return locked


def _pin_block(lines: list[str]) -> range:
    """Return the line range of the mypy hook's ``additional_dependencies``.

    Located by text rather than by a YAML parser so the rewrite can
    leave every other byte of the file, comments included, as it was.
    Blank and comment-only lines inside the list belong to it; the
    block ends at the first line that is neither a pin nor one of
    those.
    """
    in_mirror = False
    start: int | None = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("- repo:"):
            in_mirror = stripped.endswith(MYPY_MIRROR_URL)
            continue
        if in_mirror and stripped == "additional_dependencies:":
            start = index + 1
            continue
        if start is not None:
            if not stripped or stripped.startswith("#"):
                continue
            if _PIN.match(line.rstrip("\r\n")):
                continue
            return range(start, index)
    if start is not None:
        return range(start, len(lines))
    msg = f"no additional_dependencies under {MYPY_MIRROR_URL} in the config"
    raise SystemExit(msg)


def sync(
    config_path: Path, lock_path: Path, *, check: bool
) -> tuple[list[str], bool]:
    """Rewrite the pins to match the lockfile.

    Returns the changes made (or, under *check*, the changes that would
    be made) as ``name: old -> new`` strings, and whether the file was
    written.  A pinned package the lockfile does not record, or records
    at several versions none of which is the current pin, is an error:
    the script has no basis for choosing, and a human must.
    """
    # newline="" on both sides: read_text's universal-newline translation
    # would turn a CRLF checkout into LF wholesale and produce a
    # whole-file diff, against the promise to change only the versions.
    with config_path.open(encoding="utf-8", newline="") as handle:
        text = handle.read()
    lines = text.splitlines(keepends=True)
    locked = locked_versions(lock_path)

    changes: list[str] = []
    problems: list[str] = []
    for index in _pin_block(lines):
        body = lines[index].rstrip("\r\n")
        newline = lines[index][len(body) :]
        match = _PIN.match(body)
        if match is None:
            continue
        name = match.group("name")
        pinned = match.group("version")
        recorded = locked.get(canonical(name))
        if not recorded:
            problems.append(f"{name}: pinned at {pinned}, absent from uv.lock")
            continue
        if pinned in recorded:
            continue
        if len(recorded) != 1:
            problems.append(
                f"{name}: uv.lock records {', '.join(sorted(recorded))}; "
                f"cannot choose"
            )
            continue
        (wanted,) = recorded
        changes.append(f"{name}: {pinned} -> {wanted}")
        quote = match.group("quote")
        lines[index] = (
            f"{match.group('indent')}{quote}{name}=={wanted}{quote}"
            f"{match.group('rest')}{newline}"
        )

    if problems:
        raise SystemExit(
            "cannot sync mypy hook pins with uv.lock:\n  "
            + "\n  ".join(problems)
        )

    if changes and not check:
        with config_path.open("w", encoding="utf-8", newline="") as handle:
            handle.write("".join(lines))
        return changes, True
    return changes, False


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(
        description="Keep the mypy pre-commit hook's pins equal to uv.lock."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="report drift and exit 1 without writing",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    args = parser.parse_args(argv)

    changes, written = sync(args.config, args.lock, check=args.check)
    if not changes:
        return 0
    verb = "updated" if written else "would update"
    print(f"{verb} {args.config.name} to match {args.lock.name}:")
    for change in changes:
        print(f"  {change}")
    # A hook that rewrote the file must fail so pre-commit reports the
    # modification; a --check run reports drift the same way.
    return 1


if __name__ == "__main__":
    sys.exit(main())
