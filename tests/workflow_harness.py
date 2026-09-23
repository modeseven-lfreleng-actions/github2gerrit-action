# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Helpers for testing the reusable workflow as GitHub would run it.

Shared by the tests of the reusable workflow's fan-out (#422). Not a
test module: pytest collects only ``test_*.py``.

* :func:`evaluate` and :func:`render` interpret the subset of GitHub
  expressions the workflow's conditions and concurrency groups use, so
  a test can say what a condition or a group *evaluates to* rather than
  what it looks like.
* :func:`run_enumeration` executes the enumerate job's script straight
  out of the workflow against a stubbed GraphQL endpoint.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
from collections.abc import Callable
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml


WORKFLOW = (
    Path(__file__).parent.parent
    / ".github"
    / "workflows"
    / "github2gerrit.yaml"
)
REPO = "opendaylight/mdsal"


def load_jobs() -> dict[str, Any]:
    """Return the reusable workflow's jobs."""
    return dict(yaml.safe_load(WORKFLOW.read_text())["jobs"])


# ---------------------------------------------------------------------
# A small evaluator for the subset of GitHub expressions the workflow's
# conditions and concurrency groups use. Comparing the expressions as
# strings could not tell whether two groups actually render the same.
# ---------------------------------------------------------------------

_TOKEN = re.compile(
    r"\s*(?:(?P<op>\|\||&&|==|!=|!|\(|\)|,)"
    r"|'(?P<str>(?:[^']|'')*)'"
    r"|(?P<num>\d+)"
    r"|(?P<name>[A-Za-z_][\w-]*(?:\.[\w-]+)*))"
)
_TEMPLATE = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)


def truthy(value: Any) -> bool:
    if isinstance(value, float) and math.isnan(value):
        return False
    return value not in (None, False, 0, "")


def _as_number(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip() or 0)
    except ValueError:
        return math.nan


def _equal(left: Any, right: Any) -> bool:
    # GitHub compares strings case-insensitively and coerces mixed types
    # to numbers.
    if isinstance(left, str) and isinstance(right, str):
        return left.lower() == right.lower()
    if type(left) is type(right):
        return bool(left == right)
    return _as_number(left) == _as_number(right)


def _contains(haystack: Any, needle: Any) -> bool:
    if isinstance(haystack, list):
        return any(_equal(item, needle) for item in haystack)
    return str(needle or "").lower() in str(haystack or "").lower()


def _format(template: str, *args: Any) -> str:
    return re.sub(
        r"\{(\d+)\}", lambda m: render_value(args[int(m.group(1))]), template
    )


_FUNCTIONS: dict[str, Callable[..., Any]] = {
    "contains": _contains,
    "format": _format,
    "fromJSON": json.loads,
}


class _Expression:
    """Recursive descent over ``||``, ``&&``, ``==``/``!=``, ``!``."""

    def __init__(self, text: str, context: dict[str, Any]) -> None:
        self.tokens = [m for m in _TOKEN.finditer(text) if m.group().strip()]
        assert "".join(m.group() for m in self.tokens).strip() == text.strip()
        self.pos = 0
        self.context = context

    def evaluate(self) -> Any:
        value = self._or()
        assert self.pos == len(self.tokens), "trailing tokens"
        return value

    def _peek(self, op: str) -> bool:
        return (
            self.pos < len(self.tokens)
            and self.tokens[self.pos].group("op") == op
        )

    def _take(self, op: str) -> None:
        assert self._peek(op), f"expected {op!r}"
        self.pos += 1

    def _or(self) -> Any:
        value = self._and()
        while self._peek("||"):
            self._take("||")
            right = self._and()
            value = value if truthy(value) else right
        return value

    def _and(self) -> Any:
        value = self._compare()
        while self._peek("&&"):
            self._take("&&")
            right = self._compare()
            value = right if truthy(value) else value
        return value

    def _compare(self) -> Any:
        value = self._unary()
        while self._peek("==") or self._peek("!="):
            negate = self._peek("!=")
            self.pos += 1
            right = self._unary()
            value = _equal(value, right) != negate
        return value

    def _unary(self) -> Any:
        if self._peek("!"):
            self._take("!")
            return not truthy(self._unary())
        return self._primary()

    def _primary(self) -> Any:
        if self._peek("("):
            self._take("(")
            value = self._or()
            self._take(")")
            return value
        token = self.tokens[self.pos]
        self.pos += 1
        if token.group("str") is not None:
            return token.group("str").replace("''", "'")
        if token.group("num") is not None:
            return int(token.group("num"))
        name = token.group("name")
        if self._peek("("):
            return _FUNCTIONS[name](*self._arguments())
        literals = {"true": True, "false": False, "null": None}
        return literals.get(name, self.context.get(name))

    def _arguments(self) -> list[Any]:
        self._take("(")
        args = [self._or()]
        while self._peek(","):
            self._take(",")
            args.append(self._or())
        self._take(")")
        return args


def evaluate(expression: str, context: dict[str, Any]) -> Any:
    match = _TEMPLATE.fullmatch(expression.strip())
    inner = match.group(1) if match else expression
    return _Expression(inner, {"github.repository": REPO, **context}).evaluate()


def render_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def render(template: str, context: dict[str, Any]) -> str:
    return _TEMPLATE.sub(
        lambda m: render_value(evaluate(m.group(1), context)), template
    )


# ---------------------------------------------------------------------
# The enumeration script, executed out of the workflow
# ---------------------------------------------------------------------

_CURL_STUB = r"""#!/usr/bin/env bash
# Stands in for the GraphQL endpoint. Cursors here are page numbers;
# the real ones are opaque, which the script must not rely on either way.
python3 -c 'import json, sys; print(json.dumps(sys.argv[1:]))' "$@" \
  >> "${CURL_LOG}"
args=("$@")
data=''
for ((i = 0; i < ${#args[@]}; i++)); do
  if [[ "${args[i]}" == "--data" ]]; then data="${args[i + 1]}"; fi
done
if [[ -n "${CURL_FAIL:-}" ]]; then
  echo '{"message": "Bad credentials"}'
  exit 22
fi
if [[ -n "${GRAPHQL_ERRORS:-}" ]]; then
  echo '{"data": null, "errors": [{"message": "Resource not accessible"}]}'
  exit 0
fi
page=$(( $(jq -r '.variables.after // "0"' <<< "${data}") + 1 ))
pages=$(find "${CURL_PAGES}" -name '*.json' | wc -l)
if (( page < pages )); then next=true; else next=false; fi
jq -nc --slurpfile nodes "${CURL_PAGES}/${page}.json" \
  --argjson next "${next}" --arg cursor "${page}" \
  '{data: {repository: {pullRequests: {nodes: $nodes[0],
    pageInfo: {hasNextPage: $next, endCursor: $cursor}}}}}'
"""


Page = Sequence[int | dict[str, Any]]
"""A page of pull requests: bare numbers, or GraphQL nodes in full."""


def run_enumeration(
    jobs: dict[str, Any],
    tmp_path: Path,
    pages: Sequence[Page],
    **env: str,
) -> tuple[subprocess.CompletedProcess[str], str, list[list[str]]]:
    """Run the enumerate job's script against the stubbed endpoint.

    Returns the completed process, what it wrote to ``GITHUB_OUTPUT``,
    and the arguments of every request it made.
    """
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    curl = stub_dir / "curl"
    curl.write_text(_CURL_STUB)
    curl.chmod(0o755)
    page_dir = tmp_path / "pages"
    page_dir.mkdir()
    for index, nodes in enumerate(pages, start=1):
        (page_dir / f"{index}.json").write_text(
            json.dumps(
                [n if isinstance(n, dict) else {"number": n} for n in nodes]
            )
        )
    output = tmp_path / "github_output"
    output.touch()
    log = tmp_path / "curl.log"
    log.touch()
    result = subprocess.run(
        ["bash", "-c", str(jobs["enumerate"]["steps"][0]["run"])],
        capture_output=True,
        text=True,
        check=False,
        # A cursor that never advances must fail the test, not hang it
        timeout=60,
        env={
            **os.environ,
            "PATH": f"{stub_dir}{os.pathsep}{os.environ['PATH']}",
            "GITHUB_TOKEN": "t0ken",
            "GITHUB_GRAPHQL_URL": "https://api.github.example/graphql",
            "GITHUB_REPOSITORY": REPO,
            "GITHUB_OUTPUT": str(output),
            "G2G_DISABLED": "",
            "CURL_PAGES": str(page_dir),
            "CURL_LOG": str(log),
            **env,
        },
    )
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    return result, output.read_text(), calls


def matrix_of(output: str) -> str:
    """Return the matrix the script wrote to ``GITHUB_OUTPUT``."""
    return next(
        line.split("=", 1)[1]
        for line in output.splitlines()
        if line.startswith("matrix=")
    )


def request_of(call: list[str]) -> dict[str, Any]:
    """Return the GraphQL request body of one stubbed call."""
    return dict(json.loads(call[call.index("--data") + 1]))
