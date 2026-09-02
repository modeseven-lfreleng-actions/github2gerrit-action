# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import typer
from pytest import mark
from typer.testing import CliRunner

from github2gerrit.cli import _extract_pr_number
from github2gerrit.cli import _mask_secret
from github2gerrit.cli import _read_github_context
from github2gerrit.cli import _resolve_bool_override
from github2gerrit.cli import app


parametrize = mark.parametrize


# -----------------------------
# Tests for _mask_secret
# -----------------------------


def test_mask_secret_empty_returns_empty() -> None:
    assert _mask_secret("") == ""


def test_mask_secret_shorter_than_keep_masks_all() -> None:
    # default keep=4; len(secret)=3 -> mask all 3 characters
    assert _mask_secret("abc") == "***"


def test_mask_secret_equal_to_keep_masks_all() -> None:
    # When len(value) <= keep, the implementation masks the whole string
    assert _mask_secret("abcd", keep=4) == "****"


def test_mask_secret_longer_than_keep_keeps_prefix_and_masks_rest() -> None:
    # For longer values, first 'keep' chars are kept, rest are masked
    assert _mask_secret("abcdefgh") == "abcd****"
    assert _mask_secret("abcdefgh", keep=2) == "ab******"


# -----------------------------
# Tests for _extract_pr_number
# -----------------------------


@parametrize(
    "evt, expected",
    [
        ({"pull_request": {"number": 17}}, 17),
        ({"pull_request": {"number": 0}}, 0),
        ({"issue": {"number": 42}}, 42),
        ({"number": 5}, 5),
        # Non-integer values should be ignored and produce None
        ({"pull_request": {"number": "x"}}, None),
        ({"issue": {"number": "y"}}, None),
        ({"number": "z"}, None),
        ({}, None),
    ],
)
def test_extract_pr_number(
    evt: dict[str, object], expected: int | None
) -> None:
    assert _extract_pr_number(evt) == expected


# -----------------------------
# Tests for _read_github_context
# -----------------------------


def test_read_github_context_reads_event_and_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Prepare a fake GitHub event payload with action and pull_request.number
    event = {
        "action": "opened",
        "pull_request": {"number": 33},
    }
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(event), encoding="utf-8")

    # Set environment variables consumed by _read_github_context
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/repo")
    monkeypatch.setenv("GITHUB_REPOSITORY_OWNER", "example")
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.enterprise.local")
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")
    monkeypatch.setenv("GITHUB_SHA", "deadbeef")
    monkeypatch.setenv("GITHUB_BASE_REF", "main")
    monkeypatch.setenv("GITHUB_HEAD_REF", "feature-branch")
    # Ensure PR_NUMBER fallback is not used for this test
    monkeypatch.delenv("PR_NUMBER", raising=False)

    ctx = _read_github_context()

    assert ctx.event_name == "pull_request"
    assert ctx.event_action == "opened"
    assert ctx.event_path == event_path
    assert ctx.repository == "example/repo"
    assert ctx.repository_owner == "example"
    assert ctx.server_url == "https://github.enterprise.local"
    assert ctx.run_id == "12345"
    assert ctx.sha == "deadbeef"
    assert ctx.base_ref == "main"
    assert ctx.head_ref == "feature-branch"
    assert ctx.pr_number == 33


def test_read_github_context_falls_back_to_PR_NUMBER_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Event without PR info
    event = {"action": "synchronize"}
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(event), encoding="utf-8")

    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("PR_NUMBER", "6")  # fallback
    # Keep other env minimal
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_REPOSITORY_OWNER", "owner")
    monkeypatch.delenv("GITHUB_SERVER_URL", raising=False)  # use default

    ctx = _read_github_context()

    # Action should be captured from event payload
    assert ctx.event_action == "synchronize"
    # PR number should fall back to env var
    assert ctx.pr_number == 6
    # Default server URL should be used when not set
    assert ctx.server_url == "https://github.com"


def test_read_github_context_handles_missing_event_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Point to a non-existent path and minimal env
    monkeypatch.setenv("GITHUB_EVENT_NAME", "")
    monkeypatch.setenv("GITHUB_EVENT_PATH", "/nonexistent/path/to/event.json")
    monkeypatch.delenv("PR_NUMBER", raising=False)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.delenv("GITHUB_REPOSITORY_OWNER", raising=False)
    monkeypatch.delenv("GITHUB_SERVER_URL", raising=False)
    monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    monkeypatch.delenv("GITHUB_BASE_REF", raising=False)
    monkeypatch.delenv("GITHUB_HEAD_REF", raising=False)

    ctx = _read_github_context()

    # With no event and no env, values should be empty/defaults
    assert ctx.event_name == ""
    assert ctx.event_action == ""
    assert (
        ctx.event_path is not None
    )  # Path is created from the string env var even if it doesn't exist
    assert str(ctx.event_path) == "/nonexistent/path/to/event.json"
    assert ctx.repository == ""
    assert ctx.repository_owner == ""
    assert ctx.server_url == "https://github.com"
    assert ctx.run_id == ""
    assert ctx.sha == ""
    assert ctx.base_ref == ""
    assert ctx.head_ref == ""
    assert ctx.pr_number is None


# ---------------------------------------
# Tests for _resolve_bool_override
# ---------------------------------------


def _run_override(
    argv: list[str],
    env_value: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> bool:
    """Invoke a one-option app and report what the override resolved to.

    A real Typer invocation is the only way to exercise this: the
    function asks Click which *source* supplied the parameter, and that
    is only populated by genuine argument parsing.
    """
    if env_value is None:
        monkeypatch.delenv("G2G_TEST_FLAG", raising=False)
    else:
        monkeypatch.setenv("G2G_TEST_FLAG", env_value)

    seen: dict[str, bool] = {}
    app = typer.Typer()

    @app.command()
    def _cmd(
        ctx: typer.Context,
        flag: bool = typer.Option(False, "--flag/--no-flag"),
    ) -> None:
        seen["value"] = _resolve_bool_override(
            ctx, "flag", "G2G_TEST_FLAG", flag
        )

    result = CliRunner().invoke(app, argv)
    assert result.exit_code == 0, result.output
    return seen["value"]


@parametrize(
    "argv,env_value,expected",
    [
        # An explicit flag outranks the environment, in both directions.
        (["--flag"], "false", True),
        (["--no-flag"], "true", False),
        # Without an explicit flag the environment decides. This is the
        # case that matters under GitHub Actions, which passes the
        # string "false"; Click treats any non-empty string as truthy,
        # so the value has to be parsed rather than coerced.
        ([], "false", False),
        ([], "true", True),
        # No flag and no environment variable leaves the default alone.
        ([], None, False),
    ],
)
def test_resolve_bool_override_precedence(
    argv: list[str],
    env_value: str | None,
    expected: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit CLI flag beats the environment variable behind it.

    Regression test. The check for an explicit flag compared a
    ``ParameterSource`` member against ``click.core.ParameterSource``.
    typer vendors its own copy of click from 0.26 onward, so from there
    the two enum classes differ, the comparison was never true, and
    every explicit flag silently lost to its environment variable.
    Below 0.26 typer uses the installed click and the comparison held,
    which is why the bug arrived with a typer upgrade rather than with
    a change to this file.
    """
    assert _run_override(argv, env_value, monkeypatch) is expected


# ---------------------------------------------------
# Guards for the typer/click split (issue #398)
# ---------------------------------------------------


def test_help_usage_line_is_stable() -> None:
    """The usage line is what we intend, and is asserted somewhere.

    A ``click.Group`` subclass used to be passed as the app's ``cls`` to
    force this line. It was never instantiated -- typer builds a
    ``TyperCommand`` for a single-command app -- so the line came from
    typer's default all along and the override was dead code. Nothing
    asserted on the output, so neither the intent nor the reality was
    ever checked.
    """
    result = CliRunner().invoke(app, ["--help"], prog_name="github2gerrit")

    assert result.exit_code == 0, result.output
    assert "Usage: github2gerrit [OPTIONS] [TARGET_URL]" in result.output


def test_typer_and_click_symbols_are_not_interchangeable() -> None:
    """Record that typer may vendor its own click, executably.

    From typer 0.26 onward ``typer._click`` is a complete private copy,
    so a symbol taken from ``click`` is a different object from typer's
    namesake and every identity, isinstance and except relationship
    between them silently fails to hold. Two live bugs came from that.

    Below typer 0.26 the two are the same object and the assertion
    would be false, so this only asserts under the vendored regime --
    detected by the presence of ``typer._click`` rather than by version
    arithmetic. Should typer stop vendoring, this stops asserting rather
    than failing spuriously.
    """
    if importlib.util.find_spec("typer._click") is None:
        pytest.skip("typer below 0.26 uses the installed click")

    import click.exceptions

    # Asserted as behaviour, not as identity or module paths. An
    # ``is not`` comparison gets folded by a type checker into a
    # non-overlapping identity check, and the module a symbol lives in
    # moves even between patch releases: typer.Exit was
    # typer._click.exceptions.Exit in 0.27.1 and typer.exceptions.Exit
    # in 0.27.2. What holds across both is the consequence -- an except
    # clause naming the wrong copy never fires, so the exception
    # escapes it in silence.
    def _raise_typer_exit() -> None:
        raise typer.Exit(3)

    caught_by_click = False
    try:
        _raise_typer_exit()
    except click.exceptions.Exit:
        caught_by_click = True
    except typer.Exit:
        pass
    assert not caught_by_click
