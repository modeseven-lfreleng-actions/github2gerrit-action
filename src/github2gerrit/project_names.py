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
   caller has one — from ``.gitreview`` or an explicit input — and
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
import threading
from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import product
from typing import Any
from typing import Literal

from .utils import setting_bool
from .utils import setting_str


__all__ = [
    "ProjectSource",
    "RepoNames",
    "candidate_gerrit_projects",
    "find_gerrit_project",
    "gerrit_project_lister",
    "gerrit_to_github",
    "github_repo_name",
    "github_to_gerrit_guess",
    "opted_in_gerrit_project_lister",
    "resolve_repo_names",
]

log = logging.getLogger("github2gerrit.project_names")

RESOLVE_VIA_GERRIT_ENV = "G2G_RESOLVE_PROJECT_VIA_GERRIT"
"""Opt-in for asking Gerrit which reading of a GitHub name exists."""

_MAX_HYPHENS_TO_ENUMERATE = 10
"""Beyond this many hyphens the candidate set is too large to be useful.

Ten hyphens is 1024 candidates. Real mirror names carry two or three.
A name past the limit is almost certainly not a flattened path at all,
so :func:`candidate_gerrit_projects` returns only the name itself.
"""


ProjectSource = Literal[
    "explicit", "gitreview", "gerrit", "fallback", "name", "guess"
]
"""Where a :class:`RepoNames` got its Gerrit project, in order of trust.

``explicit``
    An operator named it for this run.
``gitreview``
    The repository's ``.gitreview`` names it.
``gerrit``
    Exactly one reading of the GitHub name exists on the server.
``fallback``
    A lower-trust source such as a per-organization configuration file
    named it; somebody wrote it down, nothing confirmed it.
``name``
    The GitHub name has no hyphens and so only one reading.
``guess``
    The GitHub name has hyphens and every one was read as a separator.

The first three are *confirmed*: something other than the name itself
says so.  See :attr:`RepoNames.confirmed` and :attr:`RepoNames.guessed`.
"""

_CONFIRMED_SOURCES: frozenset[str] = frozenset(
    {"explicit", "gitreview", "gerrit"}
)


@dataclass(frozen=True)
class RepoNames:
    """A repository's name on each side of the mirror.

    Attributes:
        project_gerrit: Gerrit project path, e.g. ``releng/builder``.
        project_github: GitHub repository name without its owner, e.g.
            ``releng-builder``.
        source: Where *project_gerrit* came from; see
            :data:`ProjectSource`.  Defaults to ``"explicit"`` because a
            caller constructing one by hand is stating the project.
    """

    project_gerrit: str
    project_github: str
    source: ProjectSource = "explicit"

    @property
    def confirmed(self) -> bool:
        """``True`` when something beyond the GitHub name named the project.

        Only a confirmed project should displace one an operator wrote
        in a configuration file.
        """
        return self.source in _CONFIRMED_SOURCES

    @property
    def guessed(self) -> bool:
        """``True`` when hyphens were read as separators with nothing to
        confirm which of them are.

        Callers about to push to the project, rather than merely query
        it, refuse a guess.  A name without hyphens has one reading and
        is not one.
        """
        return self.source == "guess"


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
        more than one matches.  Two matches is genuinely ambiguous —
        both ``aai/aai-common`` and ``aai/aai/common`` could exist —
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


def _clean_project(project: str) -> str:
    """Normalise a project name as written in inputs or ``.gitreview``."""
    return project.strip().removesuffix(".git").strip("/")


def resolve_repo_names(
    repository: str,
    *,
    gitreview_project: str | None = None,
    explicit_project: str | None = None,
    list_projects: Callable[[str], Iterable[str]] | None = None,
    fallback_project: str | None = None,
) -> RepoNames:
    """Settle both names for a repository, from the best source available.

    Sources, in order:

    1. *explicit_project* — an operator said so.
    2. *gitreview_project* — the repository says so.
    3. *list_projects* — Gerrit says so, if a lister was supplied and
       exactly one candidate exists there.
    4. *fallback_project* — a lower-trust source said so: typically
       a value an earlier derivation pass exported, which may itself
       have come from a per-organization configuration file.  Better
       than a guess, since somebody once wrote it down, but nothing
       here confirms it for this repository.
    5. :func:`github_to_gerrit_guess`, logged as a guess.

    The GitHub name is flattened from the Gerrit path whenever an
    authoritative project is available, as it always has been: the
    topic that identifies a pull request on Gerrit is built from it,
    and push and query must agree on that rule. When the tool is
    falling back or guessing, the GitHub name is taken from
    *repository*, since it is then the one fact actually in hand.

    Args:
        repository: ``owner/repo`` or bare ``repo``.  May be empty when
            an authoritative project is supplied.
        gitreview_project: Project from ``.gitreview``, with or without
            ``.git``.
        explicit_project: Project an operator configured for this run.
        list_projects: See :func:`find_gerrit_project`.
        fallback_project: Project from a derived or per-organization
            source, consulted only when nothing authoritative answers.

    Raises:
        ValueError: If nothing at all identifies the repository.
    """
    github_name = github_repo_name(repository) if repository.strip() else ""

    authoritative: tuple[tuple[ProjectSource, str | None], ...] = (
        ("explicit", explicit_project),
        ("gitreview", gitreview_project),
    )
    for source, project in authoritative:
        if project and project.strip():
            gerrit = _clean_project(project)
            log.debug("Gerrit project %r taken from %s", gerrit, source)
            return RepoNames(
                project_gerrit=gerrit,
                project_github=gerrit_to_github(gerrit),
                source=source,
            )

    if not github_name:
        msg = "cannot resolve repository names: no repository and no project"
        raise ValueError(msg)

    # A hyphen-free name has exactly one reading, so the server can
    # only confirm what the name already says. That confirmation is
    # worth a round trip when it would supersede a lower-trust
    # fallback, and not otherwise.
    has_fallback = bool(fallback_project and fallback_project.strip())
    if list_projects is not None and ("-" in github_name or has_fallback):
        found = find_gerrit_project(github_name, list_projects)
        if found:
            return RepoNames(
                project_gerrit=found,
                project_github=github_name,
                source="gerrit",
            )

    if fallback_project and has_fallback:
        gerrit = _clean_project(fallback_project)
        log.debug(
            "Gerrit project %r taken from derived configuration; nothing "
            "authoritative names one for %r",
            gerrit,
            github_name,
        )
        return RepoNames(
            project_gerrit=gerrit, project_github=github_name, source="fallback"
        )

    guess = github_to_gerrit_guess(github_name)
    if guess == github_name:
        # No hyphen, so exactly one reading. Nothing confirms that the
        # mirror carries the same name as its Gerrit project, but that
        # is the premise of every mapping in this module, not a guess
        # particular to this name.
        log.debug(
            "No .gitreview available; GitHub name %r has one reading and "
            "is taken as the Gerrit project",
            github_name,
        )
        return RepoNames(
            project_gerrit=guess, project_github=github_name, source="name"
        )
    log.info(
        "No .gitreview available; guessing Gerrit project %r from GitHub "
        "name %r by reading every hyphen as a separator. Add a .gitreview "
        "to the repository if this is wrong.",
        guess,
        github_name,
    )
    return RepoNames(
        project_gerrit=guess, project_github=github_name, source="guess"
    )


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


def opted_in_gerrit_project_lister(
    host: str | None,
    *,
    config: Mapping[str, str] | None = None,
) -> Callable[[str], list[str]] | None:
    """Build a *list_projects* callable for *host*, if the run allows one.

    Every place that resolves a project without ``.gitreview`` goes
    through here, so they all apply the same conditions: the operator
    opted in with :data:`RESOLVE_VIA_GERRIT_ENV`, the run is not one
    that promised to make no Gerrit calls (``G2G_NO_GERRIT`` or
    ``G2G_DRYRUN_DISABLE_NETWORK``), and there is a host to ask.
    Parameter derivation runs before ``G2G_NO_GERRIT`` has been
    translated into the network flag, which is why both are checked.

    Args:
        host: Gerrit host to ask.
        config: The loaded per-organization configuration, when the
            caller runs before it has been exported to the environment.
            Every setting read here — the opt-in, both network guards,
            the HTTP base path and the HTTP credentials the client
            needs — is read through :func:`utils.setting_str`, so a
            value in the file counts and a non-blank environment value
            still wins.  ``None`` reads the environment alone.

    Returns ``None`` whenever any condition fails, and the caller falls
    back to guessing as it would have without the option.  A client
    that cannot be built is treated the same way, since the lookup is
    an improvement on the guess rather than a requirement.
    """
    if not setting_bool(RESOLVE_VIA_GERRIT_ENV, config):
        return None
    if setting_bool("G2G_NO_GERRIT", config) or setting_bool(
        "G2G_DRYRUN_DISABLE_NETWORK", config
    ):
        log.debug(
            "%s set but Gerrit network access is disabled for this run; "
            "falling back to the name guess",
            RESOLVE_VIA_GERRIT_ENV,
        )
        return None
    host = (host or "").strip()
    if not host:
        log.debug(
            "%s set but no Gerrit host to ask; falling back to the name guess",
            RESOLVE_VIA_GERRIT_ENV,
        )
        return None
    try:
        from .gerrit_rest import build_client_for_host

        client = build_client_for_host(
            host,
            base_path=setting_str("GERRIT_HTTP_BASE_PATH", config) or None,
            http_user=setting_str("GERRIT_HTTP_USER", config) or None,
            http_password=setting_str("GERRIT_HTTP_PASSWORD", config) or None,
        )
        return _memoised_by_host(host, gerrit_project_lister(client))
    except Exception as exc:
        log.debug("Could not build a Gerrit client for %s: %s", host, exc)
        return None


_LISTINGS: dict[tuple[str, str], list[str]] = {}
"""Project listings already fetched this process, by (host, prefix).

Bulk mode builds one orchestrator per pull request, and every one of
them would otherwise ask the same server the same question. A listing
is a function of the host and the prefix alone, and the answer does not
change within a run, so the memo is keyed on exactly those two things.
"""

_LISTINGS_LOCK = threading.Lock()
"""Guards :data:`_LISTINGS` and :data:`_IN_FLIGHT`. Never held during a
fetch."""

_IN_FLIGHT: dict[tuple[str, str], threading.Lock] = {}
"""One lock per key, so callers for the same key share a single fetch
while unrelated keys proceed concurrently."""


def _memoised_by_host(
    host: str, fetch: Callable[[str], list[str]]
) -> Callable[[str], list[str]]:
    """Wrap *fetch* so each (host, prefix) is fetched at most once.

    Concurrent callers for the same key wait on that key's lock and
    then read the answer the first of them stored; callers for other
    keys are not held up.  A failed fetch is not recorded, so a
    transient error on one pull request does not condemn the rest of
    the run to the guess; the next caller for that key tries again.
    """

    def _list(prefix: str) -> list[str]:
        key = (host, prefix)
        with _LISTINGS_LOCK:
            cached = _LISTINGS.get(key)
            if cached is not None:
                return list(cached)
            key_lock = _IN_FLIGHT.setdefault(key, threading.Lock())
        with key_lock:
            with _LISTINGS_LOCK:
                cached = _LISTINGS.get(key)
            if cached is not None:
                return list(cached)
            result = list(fetch(prefix))
            with _LISTINGS_LOCK:
                _LISTINGS[key] = result
            return list(result)

    return _list
