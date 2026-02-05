# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""Tests for shallow clone detection and unshallowing functionality."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from github2gerrit.core import Orchestrator
from github2gerrit.gitutils import CommandError
from github2gerrit.gitutils import CommandResult


# Default deepen depth used in graduated deepening
DEEPEN_DEPTH = 100


class TestIsShallowClone:
    """Tests for _is_shallow_clone method."""

    def test_shallow_file_exists(self, tmp_path: Path) -> None:
        """Detect shallow clone when .git/shallow file exists."""
        # Create .git/shallow file
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        shallow_file = git_dir / "shallow"
        shallow_file.touch()

        orch = Orchestrator(workspace=tmp_path)
        assert orch._is_shallow_clone() is True

    def test_shallow_file_not_exists(self, tmp_path: Path) -> None:
        """Not shallow when .git/shallow file doesn't exist."""
        # Create .git directory but no shallow file
        git_dir = tmp_path / ".git"
        git_dir.mkdir()

        # Test with mocked git command
        def fake_run_cmd(cmd: list[str], **kwargs: Any) -> CommandResult:
            if "is-shallow-repository" in cmd:
                return CommandResult(returncode=0, stdout="false\n", stderr="")
            return CommandResult(returncode=0, stdout="", stderr="")

        with patch("github2gerrit.core.run_cmd", side_effect=fake_run_cmd):
            orch = Orchestrator(workspace=tmp_path)
            assert orch._is_shallow_clone() is False

    def test_git_command_fallback(self, tmp_path: Path) -> None:
        """Use git command as fallback when .git/shallow doesn't exist."""
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        # No shallow file - will use git command fallback

        orch = Orchestrator(workspace=tmp_path)

        # Mock the git command to return true
        with patch("github2gerrit.core.run_cmd") as mock_run:
            mock_run.return_value = CommandResult(
                returncode=0, stdout="true\n", stderr=""
            )
            result = orch._is_shallow_clone()

        assert result is True
        # Verify git rev-parse --is-shallow-repository was called
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert "--is-shallow-repository" in call_args


class TestDeepenRepository:
    """Tests for _deepen_repository method."""

    def test_deepen_success(self, tmp_path: Path) -> None:
        """Successfully deepen a shallow repository."""
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "shallow").touch()

        call_log: list[list[str]] = []

        def fake_run_cmd(cmd: list[str], **kwargs: Any) -> CommandResult:
            call_log.append(cmd)
            return CommandResult(returncode=0, stdout="", stderr="")

        with patch("github2gerrit.core.run_cmd", side_effect=fake_run_cmd):
            orch = Orchestrator(workspace=tmp_path)
            result = orch._deepen_repository(depth=100)

        assert result is True
        # Verify deepen command was called
        deepen_calls = [c for c in call_log if "--deepen=" in str(c)]
        assert len(deepen_calls) == 1
        assert f"--deepen={DEEPEN_DEPTH}" in deepen_calls[0]

    def test_deepen_not_needed(self, tmp_path: Path) -> None:
        """Return True when repository is not shallow."""
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        # No shallow file

        def fake_run_cmd(cmd: list[str], **kwargs: Any) -> CommandResult:
            if "is-shallow-repository" in cmd:
                return CommandResult(returncode=0, stdout="false\n", stderr="")
            return CommandResult(returncode=0, stdout="", stderr="")

        with patch("github2gerrit.core.run_cmd", side_effect=fake_run_cmd):
            orch = Orchestrator(workspace=tmp_path)
            result = orch._deepen_repository()

        assert result is True

    def test_deepen_failure(self, tmp_path: Path) -> None:
        """Return False when deepen command fails."""
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "shallow").touch()

        def fake_run_cmd(cmd: list[str], **kwargs: Any) -> CommandResult:
            if "--deepen=" in str(cmd):
                raise CommandError("git fetch --deepen failed", returncode=1)
            return CommandResult(returncode=0, stdout="", stderr="")

        with patch("github2gerrit.core.run_cmd", side_effect=fake_run_cmd):
            orch = Orchestrator(workspace=tmp_path)
            result = orch._deepen_repository()

        assert result is False


class TestUnshallowRepository:
    """Tests for _unshallow_repository method."""

    def test_unshallow_success(self, tmp_path: Path) -> None:
        """Successfully unshallow a shallow repository."""
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "shallow").touch()

        call_log: list[list[str]] = []

        def fake_run_cmd(cmd: list[str], **kwargs: Any) -> CommandResult:
            call_log.append(cmd)
            if cmd[:3] == ["git", "fetch", "--unshallow"]:
                return CommandResult(returncode=0, stdout="", stderr="")
            if "is-shallow-repository" in cmd:
                return CommandResult(returncode=0, stdout="true\n", stderr="")
            return CommandResult(returncode=0, stdout="", stderr="")

        with patch("github2gerrit.core.run_cmd", side_effect=fake_run_cmd):
            orch = Orchestrator(workspace=tmp_path)
            result = orch._unshallow_repository()

        assert result is True
        # Verify unshallow command was called
        unshallow_calls = [c for c in call_log if "--unshallow" in c]
        assert len(unshallow_calls) == 1

    def test_unshallow_not_needed(self, tmp_path: Path) -> None:
        """Return True when repository is not shallow."""
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        # No shallow file

        def fake_run_cmd(cmd: list[str], **kwargs: Any) -> CommandResult:
            if "is-shallow-repository" in cmd:
                return CommandResult(returncode=0, stdout="false\n", stderr="")
            return CommandResult(returncode=0, stdout="", stderr="")

        with patch("github2gerrit.core.run_cmd", side_effect=fake_run_cmd):
            orch = Orchestrator(workspace=tmp_path)
            result = orch._unshallow_repository()

        assert result is True

    def test_unshallow_failure(self, tmp_path: Path) -> None:
        """Return False when unshallow command fails."""
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "shallow").touch()

        def fake_run_cmd(cmd: list[str], **kwargs: Any) -> CommandResult:
            if cmd[:3] == ["git", "fetch", "--unshallow"]:
                raise CommandError("git fetch --unshallow failed", returncode=1)
            if "is-shallow-repository" in cmd:
                return CommandResult(returncode=0, stdout="true\n", stderr="")
            return CommandResult(returncode=0, stdout="", stderr="")

        with patch("github2gerrit.core.run_cmd", side_effect=fake_run_cmd):
            orch = Orchestrator(workspace=tmp_path)
            result = orch._unshallow_repository()

        assert result is False


class TestCheckoutWithUnshallowFallback:
    """Tests for _checkout_with_unshallow_fallback method."""

    def test_checkout_success_first_attempt(self, tmp_path: Path) -> None:
        """Checkout succeeds on first attempt without unshallow."""
        git_dir = tmp_path / ".git"
        git_dir.mkdir()

        call_log: list[list[str]] = []

        def fake_run_cmd(cmd: list[str], **kwargs: Any) -> CommandResult:
            call_log.append(cmd)
            return CommandResult(returncode=0, stdout="", stderr="")

        with patch("github2gerrit.core.run_cmd", side_effect=fake_run_cmd):
            orch = Orchestrator(workspace=tmp_path)
            orch._checkout_with_unshallow_fallback(
                branch_name="test_branch",
                start_point="abc123",
                create_branch=True,
            )

        # Only checkout command should be called
        checkout_calls = [c for c in call_log if c[:2] == ["git", "checkout"]]
        assert len(checkout_calls) == 1
        assert checkout_calls[0] == [
            "git",
            "checkout",
            "-b",
            "test_branch",
            "abc123",
        ]

    def test_checkout_fails_then_deepen_succeeds(self, tmp_path: Path) -> None:
        """Checkout fails due to missing SHA, deepen fixes it."""
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "shallow").touch()

        checkout_attempts = [0]

        def fake_run_cmd(cmd: list[str], **kwargs: Any) -> CommandResult:
            if cmd[:2] == ["git", "checkout"]:
                checkout_attempts[0] += 1
                if checkout_attempts[0] == 1:
                    # First attempt fails with "not a commit" error
                    raise CommandError(
                        "fatal: 'abc123' is not a commit and a branch "
                        "'test_branch' cannot be created from it",
                        returncode=128,
                    )
                # Second attempt (after deepen) succeeds
                return CommandResult(returncode=0, stdout="", stderr="")
            if "--deepen=" in str(cmd):
                return CommandResult(returncode=0, stdout="", stderr="")
            if "is-shallow-repository" in cmd:
                return CommandResult(returncode=0, stdout="true\n", stderr="")
            return CommandResult(returncode=0, stdout="", stderr="")

        with patch("github2gerrit.core.run_cmd", side_effect=fake_run_cmd):
            orch = Orchestrator(workspace=tmp_path)
            # Should not raise
            orch._checkout_with_unshallow_fallback(
                branch_name="test_branch",
                start_point="abc123",
                create_branch=True,
            )

        # Should have attempted checkout twice (before and after deepen)
        assert checkout_attempts[0] == 2

    def test_checkout_fails_deepen_insufficient_unshallow_succeeds(
        self, tmp_path: Path
    ) -> None:
        """Checkout fails, deepen insufficient, full unshallow fixes it."""
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "shallow").touch()

        checkout_attempts = [0]

        def fake_run_cmd(cmd: list[str], **kwargs: Any) -> CommandResult:
            if cmd[:2] == ["git", "checkout"]:
                checkout_attempts[0] += 1
                if checkout_attempts[0] <= 2:
                    # First and second attempts fail
                    raise CommandError(
                        "fatal: 'abc123' is not a commit",
                        returncode=128,
                    )
                # Third attempt (after full unshallow) succeeds
                return CommandResult(returncode=0, stdout="", stderr="")
            if "--deepen=" in str(cmd):
                # Deepen succeeds but doesn't help
                return CommandResult(returncode=0, stdout="", stderr="")
            if cmd[:3] == ["git", "fetch", "--unshallow"]:
                return CommandResult(returncode=0, stdout="", stderr="")
            if "is-shallow-repository" in cmd:
                return CommandResult(returncode=0, stdout="true\n", stderr="")
            return CommandResult(returncode=0, stdout="", stderr="")

        with patch("github2gerrit.core.run_cmd", side_effect=fake_run_cmd):
            orch = Orchestrator(workspace=tmp_path)
            orch._checkout_with_unshallow_fallback(
                branch_name="test_branch",
                start_point="abc123",
                create_branch=True,
            )

        # Should have attempted checkout 3 times:
        # 1. Initial (fail), 2. After deepen (fail), 3. After unshallow (success)
        assert checkout_attempts[0] == 3

    def test_checkout_fails_non_shallow_error_raises(
        self, tmp_path: Path
    ) -> None:
        """Checkout fails with non-shallow error, raises immediately."""
        git_dir = tmp_path / ".git"
        git_dir.mkdir()

        def fake_run_cmd(cmd: list[str], **kwargs: Any) -> CommandResult:
            if cmd[:2] == ["git", "checkout"]:
                # Fail with a different error (not related to missing commit)
                raise CommandError(
                    "fatal: A branch named 'test_branch' already exists",
                    returncode=128,
                )
            return CommandResult(returncode=0, stdout="", stderr="")

        with patch("github2gerrit.core.run_cmd", side_effect=fake_run_cmd):
            orch = Orchestrator(workspace=tmp_path)
            with pytest.raises(CommandError) as exc_info:
                orch._checkout_with_unshallow_fallback(
                    branch_name="test_branch",
                    start_point="abc123",
                    create_branch=True,
                )

        assert "already exists" in str(exc_info.value)

    def test_checkout_fails_all_recovery_fails_raises(
        self, tmp_path: Path
    ) -> None:
        """Checkout fails, deepen and unshallow both fail, raises original error."""
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "shallow").touch()

        def fake_run_cmd(cmd: list[str], **kwargs: Any) -> CommandResult:
            if cmd[:2] == ["git", "checkout"]:
                raise CommandError(
                    "fatal: 'abc123' is not a commit",
                    returncode=128,
                )
            if "--deepen=" in str(cmd):
                raise CommandError("deepen failed", returncode=1)
            if cmd[:3] == ["git", "fetch", "--unshallow"]:
                raise CommandError("unshallow failed", returncode=1)
            if "is-shallow-repository" in cmd:
                return CommandResult(returncode=0, stdout="true\n", stderr="")
            return CommandResult(returncode=0, stdout="", stderr="")

        with patch("github2gerrit.core.run_cmd", side_effect=fake_run_cmd):
            orch = Orchestrator(workspace=tmp_path)
            with pytest.raises(CommandError) as exc_info:
                orch._checkout_with_unshallow_fallback(
                    branch_name="test_branch",
                    start_point="abc123",
                    create_branch=True,
                )

        assert "not a commit" in str(exc_info.value)

    @pytest.mark.parametrize(
        "error_message",
        [
            "fatal: 'abc123' is not a commit",
            "cannot be created from it",
            "fatal: bad revision 'abc123'",
            "unknown revision or path not in the working tree",
            "invalid reference: abc123",
        ],
    )
    def test_recognizes_missing_commit_errors(
        self, tmp_path: Path, error_message: str
    ) -> None:
        """All variations of missing commit errors trigger unshallow."""
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "shallow").touch()

        checkout_attempts = [0]

        def fake_run_cmd(cmd: list[str], **kwargs: Any) -> CommandResult:
            if cmd[:2] == ["git", "checkout"]:
                checkout_attempts[0] += 1
                if checkout_attempts[0] == 1:
                    raise CommandError(error_message, returncode=128)
                # Second attempt after deepen succeeds
                return CommandResult(returncode=0, stdout="", stderr="")
            if "--deepen=" in str(cmd):
                return CommandResult(returncode=0, stdout="", stderr="")
            if "is-shallow-repository" in cmd:
                return CommandResult(returncode=0, stdout="true\n", stderr="")
            return CommandResult(returncode=0, stdout="", stderr="")

        with patch("github2gerrit.core.run_cmd", side_effect=fake_run_cmd):
            orch = Orchestrator(workspace=tmp_path)
            orch._checkout_with_unshallow_fallback(
                branch_name="test_branch",
                start_point="abc123",
                create_branch=True,
            )

        # Should have attempted checkout twice (before and after deepen)
        assert checkout_attempts[0] == 2


class TestEnsureWorkspacePrepared:
    """Tests for _ensure_workspace_prepared - simple fetch only."""

    def test_shallow_clone_does_normal_fetch(self, tmp_path: Path) -> None:
        """Shallow clone does NOT proactively unshallow (performance)."""
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "shallow").touch()

        call_log: list[list[str]] = []

        def fake_run_cmd(cmd: list[str], **kwargs: Any) -> CommandResult:
            call_log.append(cmd)
            return CommandResult(returncode=0, stdout="", stderr="")

        with patch("github2gerrit.core.run_cmd", side_effect=fake_run_cmd):
            orch = Orchestrator(workspace=tmp_path)
            orch._ensure_workspace_prepared("master")

        # Should NOT have called fetch --unshallow (performance optimization)
        unshallow_calls = [c for c in call_log if "--unshallow" in c]
        assert len(unshallow_calls) == 0

        # Should have called normal fetch
        fetch_calls = [c for c in call_log if c[:2] == ["git", "fetch"]]
        assert len(fetch_calls) == 1
        assert fetch_calls[0] == ["git", "fetch", "origin", "master"]

    def test_non_shallow_clone_normal_fetch(self, tmp_path: Path) -> None:
        """Non-shallow clone uses normal fetch."""
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        # No shallow file

        call_log: list[list[str]] = []

        def fake_run_cmd(cmd: list[str], **kwargs: Any) -> CommandResult:
            call_log.append(cmd)
            return CommandResult(returncode=0, stdout="", stderr="")

        with patch("github2gerrit.core.run_cmd", side_effect=fake_run_cmd):
            orch = Orchestrator(workspace=tmp_path)
            orch._ensure_workspace_prepared("master")

        # Should have called normal fetch
        fetch_calls = [c for c in call_log if c[:2] == ["git", "fetch"]]
        assert len(fetch_calls) == 1
        assert fetch_calls[0] == ["git", "fetch", "origin", "master"]

    def test_workspace_prepared_flag_prevents_refetch(
        self, tmp_path: Path
    ) -> None:
        """Once prepared, subsequent calls don't refetch."""
        git_dir = tmp_path / ".git"
        git_dir.mkdir()

        call_log: list[list[str]] = []

        def fake_run_cmd(cmd: list[str], **kwargs: Any) -> CommandResult:
            call_log.append(cmd)
            return CommandResult(returncode=0, stdout="", stderr="")

        with patch("github2gerrit.core.run_cmd", side_effect=fake_run_cmd):
            orch = Orchestrator(workspace=tmp_path)
            orch._ensure_workspace_prepared("master")
            orch._ensure_workspace_prepared("master")  # Second call

        # Should only have fetched once
        fetch_calls = [c for c in call_log if c[:2] == ["git", "fetch"]]
        assert len(fetch_calls) == 1
