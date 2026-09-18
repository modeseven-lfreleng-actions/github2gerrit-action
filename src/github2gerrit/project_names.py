# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Mapping between GitHub repository names and Gerrit project paths.

Gerrit projects are paths, ``releng/builder``; GitHub repository names
are flat, ``releng-builder``.  The Linux Foundation mirrors flatten a
path by replacing every ``/`` with ``-``.  This module is the single
place that mapping lives, in both directions, so every part of the tool
agrees on what a name means.

One direction is exact and the other is not
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Gerrit to GitHub is deterministic: ``/`` becomes ``-`` and nothing else
changes.  GitHub to Gerrit is **ambiguous**, because a hyphen is both
the flattened separator and an ordinary character inside a segment.
``aai-aai-common`` is ``aai/aai-common`` on gerrit.onap.org, not
``aai/aai/common``, and no function of the name alone can tell.

So the reverse direction offers three things, in order of trust:

1. :func:`resolve_repo_names` takes an authoritative project when the
   caller has one \u2014 from ``.gitreview`` or an explicit input \u2014 and
   uses it.
2. :func:`find_gerrit_project` asks Gerrit which of the
   :func:`candidate_gerrit_projects` actually exists.
3. :func:`github_to_gerrit_guess` reads every hyphen as a separator.
   It is named a guess because that is what it is.

Callers that only have a GitHub name and want a Gerrit project should
be reading ``.gitreview``; the guess exists so the tool degrades rather
than stops when that file is missing, and its output should be logged
as such wherever it is used.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import product
from typing import Any


__all__ = [
    "RepoNames",
    "candidate_gerrit_projects",
    "find_gerrit_project",
    "gerrit_project_lister",
    "gerrit_to_github",
    "github_repo_name",
    "github_to_gerrit_guess",
    "resolve_repo_names",
]

log = logging.getLogger("github2gerrit.project_names")

_MAX_HYPHENS_TO_ENUMERATE = 10
"""Beyond this many hyphens the candidate set is too large to be useful.

Ten hyphens is 1024 candidates. Real mirror names carry two or three.
A name past the limit is almost certainly not a flattened path at all,
so :func:`candidate_gerrit_projects` returns only the name itself.
"""


@dataclass(frozen=True)
class RepoNames:
    """A repository's name on each side of the mirror.

    Attributes:
        project_gerrit: Gerrit project path, e.g. ``releng/builder``.
        project_github: GitHub repository name without its owner, e.g.
            ``releng-builder``.
    """

    project_gerrit: str
    project_github: str


def gerrit_to_github(project: str) -> str:
    """Flatten a Gerrit project path to its GitHub repository name.

    The exact direction: ``releng/builder`` becomes ``releng-builder``.
    Any ``.git`` suffix is dropped first, since ``.gitreview`` files
    commonly carry one and a GitHub name never does.
    """
    name = project.strip()
    name = name.removesuffix(".git")
    return name.strip("/").replace("/", "-")


def github_repo_name(repository: str) -> str:
    """Return the repository name from ``owner/repo``.

    A bare name is returned unchanged, so callers may pass either form.
    """
    name = repository.strip().strip("/")
    if "/" in name:
        return name.rsplit("/", 1)[1]
    return name


def github_to_gerrit_guess(github_name: str) -> str:
    """Read every hyphen in a GitHub name as a path separator.

    ``my-repo-name`` becomes ``my/repo/name``.  This is a **guess**: it
    is right for ``multicloud-openstack`` and wrong for
    ``aai-aai-common``, and nothing in the name distinguishes the two.
    Prefer :func:`resolve_repo_names` with a ``.gitreview`` project, or
    :func:`find_gerrit_project` when Gerrit can be asked.

    Owner prefixes are stripped, so ``owner/repo-name`` is accepted.
    """
    return github_repo_name(github_name).replace("-", "/")


def candidate_gerrit_projects(github_name: str) -> list[str]:
    """Every Gerrit path a flattened GitHub name could have come from.

    Each hyphen may be a separator or a literal, so a name with *n*
    hyphens has ``2**n`` candidates.  Returned with the fewest
    separators first and the bare name always present, so a caller
    that finds nothing better can fall back to it.

    Past :data:`_MAX_HYPHENS_TO_ENUMERATE` hyphens only the bare name is
    returned; such a name is not plausibly a flattened path.
    """
    name = github_repo_name(github_name)
    parts = name.split("-")
    if len(parts) - 1 > _MAX_HYPHENS_TO_ENUMERATE:
        return [name]

    candidates: set[str] = set()
    for separators in product(("-", "/"), repeat=len(parts) - 1):
        built = parts[0]
        for sep, part in zip(separators, parts[1:], strict=True):
            built += sep + part
        candidates.add(built)

    return sorted(candidates, key=lambda c: (c.count("/"), c))


def find_gerrit_project(
    github_name: str,
    list_projects: Callable[[str], Iterable[str]],
) -> str | None:
    """Ask Gerrit which candidate path really exists.

    Args:
        github_name: The GitHub repository name, with or without owner.
        list_projects: Returns the Gerrit projects whose name begins
            with a given prefix.  Typically wraps
            ``GET /projects/?p=<prefix>``.  Injected so this module
            stays free of network code and the resolution is testable.

    Returns:
        The one candidate Gerrit knows about, or ``None`` when none or
        more than one matches.  Two matches is genuinely ambiguous \u2014
        both ``aai/aai-common`` and ``aai/aai/common`` could exist \u2014
        and guessing between them would be no better than the guess
        this function exists to avoid.
    """
    name = github_repo_name(github_name)
    candidates = candidate_gerrit_projects(name)
    prefix = name.split("-", 1)[0]

    try:
        known = {p.strip().removesuffix(".git") for p in list_projects(prefix)}
    except Exception as exc:
        log.debug("Could not list Gerrit projects under %r: %s", prefix, exc)
        return None

    matches = [c for c in candidates if c in known]
    if len(matches) == 1:
        log.debug(
            "Resolved %r to Gerrit project %r from the server's project list",
            name,
            matches[0],
        )
        return matches[0]
    if matches:
        log.warning(
            "GitHub name %r matches several Gerrit projects (%s); refusing "
            "to choose between them",
            name,
            ", ".join(matches),
        )
    else:
        log.debug("No Gerrit project matches any reading of %r", name)
    return None


def resolve_repo_names(
    repository: str,
    *,
    gitreview_project: str | None = None,
    explicit_project: str | None = None,
    list_projects: Callable[[str], Iterable[str]] | None = None,
) -> RepoNames:
    """Settle both names for a repository, from the best source available.

    Sources, in order:

    1. *explicit_project* \u2014 an operator said so.
    2. *gitreview_project* \u2014 the repository says so.
    3. *list_projects* \u2014 Gerrit says so, if a lister was supplied and
       exactly one candidate exists there.
    4. :func:`github_to_gerrit_guess`, logged as a guess.

    The GitHub name is flattened from the Gerrit path whenever an
    authoritative project is available, as it always has been: the
    topic that identifies a pull request on Gerrit is built from it,
    and push and query must agree on that rule. Only when the tool is
    guessing the Gerrit path is the GitHub name taken from
    *repository*, since it is then the one fact actually in hand.

    Args:
        repository: ``owner/repo`` or bare ``repo``.  May be empty when
            an authoritative project is supplied.
        gitreview_project: Project from ``.gitreview``, with or without
            ``.git``.
        explicit_project: Project from an input or configuration.
        list_projects: See :func:`find_gerrit_project`.

    Raises:
        ValueError: If nothing at all identifies the repository.
    """
    github_name = github_repo_name(repository) if repository.strip() else ""

    for source, project in (
        ("explicit input", explicit_project),
        (".gitreview", gitreview_project),
    ):
        if project and project.strip():
            gerrit = project.strip().removesuffix(".git").strip("/")
            log.debug("Gerrit project %r taken from %s", gerrit, source)
            return RepoNames(
                project_gerrit=gerrit,
                project_github=gerrit_to_github(gerrit),
            )

    if not github_name:
        msg = "cannot resolve repository names: no repository and no project"
        raise ValueError(msg)

    if list_projects is not None:
        found = find_gerrit_project(github_name, list_projects)
        if found:
            return RepoNames(project_gerrit=found, project_github=github_name)

    guess = github_to_gerrit_guess(github_name)
    log.info(
        "No .gitreview available; guessing Gerrit project %r from GitHub "
        "name %r by reading every hyphen as a separator. Add a .gitreview "
        "to the repository if this is wrong.",
        guess,
        github_name,
    )
    return RepoNames(project_gerrit=guess, project_github=github_name)


def gerrit_project_lister(client: Any) -> Callable[[str], list[str]]:
    """Adapt a Gerrit REST client into a *list_projects* callable.

    Wraps ``GET /projects/?p=<prefix>``, which returns a mapping keyed
    by project name.  Kept here so callers do not each rediscover the
    endpoint's shape.
    """
    from urllib.parse import quote

    def _list(prefix: str) -> list[str]:
        data = client.get(f"/projects/?p={quote(prefix, safe='')}")
        if isinstance(data, dict):
            return [str(k) for k in data]
        return []

    return _list
