# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Who is trusted to direct this tool.

GitHub reports an ``author_association`` on every comment, review and
pull request, describing the author's standing in the repository.  This
module turns that into a single trust decision, so every place that acts
on user input agrees on who may be obeyed.

Why ``author_association`` rather than collaborator permission
──────────────────────────────────────────────────────────────
The repositories this tool serves are Gerrit mirrors.  Their GitHub
collaborator lists hold infrastructure staff and bots, not the people
who review the code — on ``opendaylight/mdsal`` every collaborator is
LF releng, while project committers appear only as organisation
``MEMBER``\\ s.  Requiring ``write`` or ``admin`` there would trust the
wrong people and exclude the right ones.

``author_association`` also arrives on the payloads already being
fetched, so it costs no extra API calls and no extra token scope.

Its limits are real and worth stating: ``MEMBER`` means organisation
member, which is not the same as write access to a given repository.
Callers that need a stronger guarantee should combine this with a
signal GitHub enforces structurally — for example, that a pull request
author cannot approve their own pull request.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable


__all__ = [
    "DEFAULT_TRUSTED_ASSOCIATIONS",
    "TRUSTED_ASSOCIATIONS_ENV",
    "describe_trust_policy",
    "is_trusted_association",
    "trusted_associations",
]

log = logging.getLogger("github2gerrit.trust")

TRUSTED_ASSOCIATIONS_ENV = "G2G_TRUSTED_ASSOCIATIONS"
"""Environment variable overriding the trusted set (comma-separated)."""

DEFAULT_TRUSTED_ASSOCIATIONS: frozenset[str] = frozenset(
    {
        "OWNER",
        "MEMBER",
        "COLLABORATOR",
    }
)
"""Associations trusted to direct the tool by default.

``OWNER`` and ``COLLABORATOR`` carry repository standing; ``MEMBER``
carries organisation standing, which is the only signal available on a
Gerrit mirror.

``CONTRIBUTOR`` is deliberately excluded: it means only that the author
has had a pull request merged at some point, which any outside
contributor can achieve and which conveys no authority.
"""


def trusted_associations() -> frozenset[str]:
    """Return the trusted association set, honouring the environment.

    ``G2G_TRUSTED_ASSOCIATIONS`` accepts a comma-separated list, for
    example ``OWNER,MEMBER``.  Values are upper-cased and stripped.  An
    unset, empty or entirely blank value keeps the default.
    """
    raw = os.getenv(TRUSTED_ASSOCIATIONS_ENV, "").strip()
    if not raw:
        return DEFAULT_TRUSTED_ASSOCIATIONS

    parsed = {part.strip().upper() for part in raw.split(",") if part.strip()}
    if not parsed:
        return DEFAULT_TRUSTED_ASSOCIATIONS

    log.debug(
        "Trusted associations overridden via %s: %s",
        TRUSTED_ASSOCIATIONS_ENV,
        sorted(parsed),
    )
    return frozenset(parsed)


def is_trusted_association(
    association: str | None,
    *,
    allowed: Iterable[str] | None = None,
) -> bool:
    """Report whether *association* is trusted to direct the tool.

    Args:
        association: A GitHub ``author_association`` value.  ``None``,
            empty, or an unrecognised value is untrusted.
        allowed: Override the trusted set.  Defaults to
            :func:`trusted_associations`.

    Returns:
        ``True`` only for an explicitly trusted association.  Every
        other input — including a missing or malformed one — is
        untrusted, so an absent signal never grants authority.
    """
    if not association:
        return False

    permitted = (
        frozenset(a.strip().upper() for a in allowed)
        if allowed is not None
        else trusted_associations()
    )
    return association.strip().upper() in permitted


def describe_trust_policy() -> str:
    """Return a human-readable summary of the trusted set, for logs."""
    return ", ".join(sorted(trusted_associations()))
