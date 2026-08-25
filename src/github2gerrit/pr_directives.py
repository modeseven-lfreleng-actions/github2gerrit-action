# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""The entry point for acting on ``@github2gerrit`` PR directives.

This module composes the three steps that must always happen together:

1. fetch the pull request's comments (:mod:`github2gerrit.github_api`),
2. discard those whose author is not trusted
   (:mod:`github2gerrit.trust`), and
3. parse the survivors for commands
   (:mod:`github2gerrit.pr_commands`).

**Consumers of the command registry must come through here.** Calling
:func:`github2gerrit.pr_commands.parse_commands` directly with comments
straight from the API skips step 2, which on a public mirror lets any
GitHub user direct the tool. That was the defect fixed in issue #382,
and a second command added later would reintroduce it just as easily as
the first one did.

The split exists so that no single module holds both API access and
directive grammar: ``github_api`` fetches and partitions by trust
without knowing what a command looks like, ``pr_commands`` parses text
without reaching the network, and this module joins them.
"""

from __future__ import annotations

import logging
from typing import Any

from .github_api import get_trusted_comment_bodies
from .pr_commands import MENTION_PREFIX
from .pr_commands import CommandMatch
from .pr_commands import CommandParseResult
from .pr_commands import contains_directive
from .pr_commands import parse_commands
from .trust import describe_trust_policy


__all__ = [
    "DirectiveScan",
    "find_pr_command",
    "scan_pr_directives",
]

log = logging.getLogger("github2gerrit.pr_directives")


class DirectiveScan:
    """Result of scanning a pull request for directives.

    Attributes:
        result: Commands parsed from trusted comments.
        ignored: ``"login (ASSOCIATION)"`` for each untrusted comment
            that attempted to issue a directive.
    """

    __slots__ = ("ignored", "result")

    def __init__(
        self,
        result: CommandParseResult,
        ignored: list[str],
    ) -> None:
        self.result = result
        self.ignored = ignored


def scan_pr_directives(pr: Any) -> DirectiveScan:
    """Fetch, authorise and parse a pull request's directives.

    Refused directives are logged here rather than left to each caller,
    so a maintainer whose command is declined always learns why.

    Args:
        pr: Pull request object.

    Returns:
        A :class:`DirectiveScan`.
    """
    bodies, ignored = get_trusted_comment_bodies(
        pr, directive_detector=contains_directive
    )

    if ignored:
        log.warning(
            "🚫 Ignoring %s directive(s) from untrusted comment "
            "author(s): %s. Trusted associations: %s",
            MENTION_PREFIX,
            ", ".join(ignored),
            describe_trust_policy(),
        )

    return DirectiveScan(parse_commands(bodies), ignored)


def find_pr_command(pr: Any, command_name: str) -> CommandMatch | None:
    """Return a trusted occurrence of *command_name*, or ``None``.

    The authorised counterpart of
    :func:`github2gerrit.pr_commands.find_command`.

    Args:
        pr: Pull request object.
        command_name: Canonical command name to look for.

    Returns:
        The :class:`~github2gerrit.pr_commands.CommandMatch` when a
        trusted author issued the command, otherwise ``None``.
    """
    target = command_name.lower().strip()
    for match in scan_pr_directives(pr).result.matches:
        if match.command_name == target:
            return match
    return None
