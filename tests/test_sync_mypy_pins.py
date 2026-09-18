# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""``scripts/sync_mypy_pins.py`` keeps the mypy hook's pins at the locked
versions.

It replaced a test that compared the two files (#433): a pull request
is tested as merged into ``main``, so that test read ``main``'s
lockfile and failed the branch whenever a dependency moved elsewhere.
The script runs as a pre-commit hook against one checkout, rewrites the
pins in place, and leaves every other byte of the file alone.  No test
here compares the repository's own two files: that would be the same
unsound comparison in a new place.  The hook is the gate.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "sync_mypy_pins.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("sync_mypy_pins", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sync_mypy_pins = _load()


CONFIG = """\
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    hooks:
      - id: ruff
        additional_dependencies:
          - click==1.0.0
  - repo: https://github.com/pre-commit/mirrors-mypy
    rev: abc  # frozen: v2.3.1
    hooks:
      - id: mypy
        # A comment that must survive the rewrite.
        additional_dependencies:
          - mypy==2.3.1
          - click==8.4.2  # trailing comment
          - types-PyYAML==6.0.12.20260101
          - typer==0.27.1

  - repo: https://github.com/btford/write-good
    hooks:
      - id: write-good
"""

LOCK = """\
version = 1

[[package]]
name = "click"
version = "8.5.0"

[[package]]
name = "mypy"
version = "2.3.1"

[[package]]
name = "types-pyyaml"
version = "6.0.12.20260906"

[[package]]
name = "typer"
version = "0.27.2"

[[package]]
name = "github2gerrit"
source = { editable = "." }
"""


@pytest.fixture
def files(tmp_path: Path) -> tuple[Path, Path]:
    config = tmp_path / ".pre-commit-config.yaml"
    lock = tmp_path / "uv.lock"
    config.write_text(CONFIG, encoding="utf-8")
    lock.write_text(LOCK, encoding="utf-8")
    return config, lock


class TestSync:
    """The rewrite itself."""

    def test_rewrites_only_the_mypy_pins(
        self, files: tuple[Path, Path]
    ) -> None:
        config, lock = files
        changes, written = sync_mypy_pins.sync(config, lock, check=False)

        assert written is True
        assert changes == [
            "click: 8.4.2 -> 8.5.0",
            "types-PyYAML: 6.0.12.20260101 -> 6.0.12.20260906",
            "typer: 0.27.1 -> 0.27.2",
        ]
        text = config.read_text(encoding="utf-8")
        # Rewritten, with the trailing comment and the package's own
        # spelling kept.
        assert "          - click==8.5.0  # trailing comment\n" in text
        assert "          - types-PyYAML==6.0.12.20260906\n" in text
        assert "          - typer==0.27.2\n" in text
        # Untouched: the ruff hook's own click pin, the comments, and
        # everything after the block.
        assert "          - click==1.0.0\n" in text
        assert "# A comment that must survive the rewrite." in text
        assert text.endswith("      - id: write-good\n")
        # Idempotent.
        assert sync_mypy_pins.sync(config, lock, check=False) == ([], False)

    def test_check_reports_without_writing(
        self, files: tuple[Path, Path]
    ) -> None:
        config, lock = files
        before = config.read_text(encoding="utf-8")
        changes, written = sync_mypy_pins.sync(config, lock, check=True)
        assert len(changes) == 3
        assert written is False
        assert config.read_text(encoding="utf-8") == before

    def test_in_sync_is_a_no_op(self, files: tuple[Path, Path]) -> None:
        config, lock = files
        sync_mypy_pins.sync(config, lock, check=False)
        assert sync_mypy_pins.sync(config, lock, check=True) == ([], False)

    def test_package_missing_from_lock_is_an_error(
        self, files: tuple[Path, Path]
    ) -> None:
        # The script has no basis for choosing a version, so it stops
        # rather than guessing or silently leaving the pin stale.
        config, lock = files
        lock.write_text(
            LOCK.replace('name = "typer"\nversion = "0.27.2"\n', ""),
            encoding="utf-8",
        )
        with pytest.raises(
            SystemExit, match=r"typer: pinned at 0\.27\.1, absent"
        ):
            sync_mypy_pins.sync(config, lock, check=False)

    def test_forked_resolution_is_an_error(
        self, files: tuple[Path, Path]
    ) -> None:
        # uv may record one package at two versions under different
        # markers; if neither is the current pin a human must choose.
        config, lock = files
        lock.write_text(
            LOCK + '\n[[package]]\nname = "typer"\nversion = "0.28.0"\n',
            encoding="utf-8",
        )
        with pytest.raises(SystemExit, match=r"typer: uv\.lock records"):
            sync_mypy_pins.sync(config, lock, check=False)

    def test_forked_resolution_matching_the_pin_is_fine(
        self, files: tuple[Path, Path]
    ) -> None:
        config, lock = files
        lock.write_text(
            LOCK + '\n[[package]]\nname = "typer"\nversion = "0.27.1"\n',
            encoding="utf-8",
        )
        changes, _ = sync_mypy_pins.sync(config, lock, check=False)
        assert not any(c.startswith("typer:") for c in changes)

    def test_missing_hook_is_an_error(self, tmp_path: Path) -> None:
        config = tmp_path / "c.yaml"
        config.write_text("repos: []\n", encoding="utf-8")
        lock = tmp_path / "uv.lock"
        lock.write_text(LOCK, encoding="utf-8")
        with pytest.raises(SystemExit, match="no additional_dependencies"):
            sync_mypy_pins.sync(config, lock, check=False)

    def test_comments_and_blank_lines_inside_the_list_do_not_end_it(
        self, tmp_path: Path
    ) -> None:
        # A pin after a standalone comment or a blank line is still in
        # the list; skipping it would leave a stale pin while reporting
        # success.
        config = tmp_path / ".pre-commit-config.yaml"
        config.write_text(
            CONFIG.replace(
                "          - types-PyYAML==6.0.12.20260101\n",
                "          # stubs\n\n"
                "          - types-PyYAML==6.0.12.20260101\n",
            ),
            encoding="utf-8",
        )
        lock = tmp_path / "uv.lock"
        lock.write_text(LOCK, encoding="utf-8")

        changes, _ = sync_mypy_pins.sync(config, lock, check=False)
        assert "typer: 0.27.1 -> 0.27.2" in changes
        assert "types-PyYAML: 6.0.12.20260101 -> 6.0.12.20260906" in changes
        text = config.read_text(encoding="utf-8")
        assert (
            "          # stubs\n\n          - types-PyYAML==6.0.12.20260906\n"
            in text
        )
        # The next hook's list is still not part of it.
        assert "      - id: write-good\n" in text

    def test_quoted_pins_are_recognised_and_keep_their_quotes(
        self, tmp_path: Path
    ) -> None:
        # YAML decodes `- "click==8.4.2"` to the same string as the bare
        # form. A matcher that only knew the bare form would end the
        # block at the quoted line and leave it, and every pin after it,
        # stale while reporting success.
        config = tmp_path / ".pre-commit-config.yaml"
        config.write_text(
            CONFIG.replace(
                "          - click==8.4.2  # trailing comment\n",
                '          - "click==8.4.2"  # trailing comment\n',
            ).replace(
                "          - typer==0.27.1\n", "          - 'typer==0.27.1'\n"
            ),
            encoding="utf-8",
        )
        lock = tmp_path / "uv.lock"
        lock.write_text(LOCK, encoding="utf-8")

        changes, _ = sync_mypy_pins.sync(config, lock, check=False)

        assert changes == [
            "click: 8.4.2 -> 8.5.0",
            "types-PyYAML: 6.0.12.20260101 -> 6.0.12.20260906",
            "typer: 0.27.1 -> 0.27.2",
        ]
        text = config.read_text(encoding="utf-8")
        assert '          - "click==8.5.0"  # trailing comment\n' in text
        assert "          - 'typer==0.27.2'\n" in text

    def test_mismatched_quotes_are_not_a_pin(self, tmp_path: Path) -> None:
        config = tmp_path / ".pre-commit-config.yaml"
        config.write_text(
            CONFIG.replace(
                "          - typer==0.27.1\n", "          - \"typer==0.27.1'\n"
            ),
            encoding="utf-8",
        )
        lock = tmp_path / "uv.lock"
        lock.write_text(LOCK, encoding="utf-8")
        changes, _ = sync_mypy_pins.sync(config, lock, check=True)
        assert not any(c.startswith("typer:") for c in changes)

    def test_line_endings_are_preserved(self, tmp_path: Path) -> None:
        # read_text would translate CRLF to LF and rewrite the whole
        # file; only the version substrings may change.
        config = tmp_path / ".pre-commit-config.yaml"
        config.write_bytes(CONFIG.replace("\n", "\r\n").encode())
        lock = tmp_path / "uv.lock"
        lock.write_text(LOCK, encoding="utf-8")

        sync_mypy_pins.sync(config, lock, check=False)

        raw = config.read_bytes()
        assert b"\n" not in raw.replace(b"\r\n", b"")
        assert b"          - typer==0.27.2\r\n" in raw
        assert raw.count(b"\r\n") == CONFIG.count("\n")


class TestMain:
    """Exit codes as a pre-commit hook sees them."""

    def test_exits_nonzero_after_rewriting(
        self, files: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A hook that modified a file must fail so pre-commit reports it.
        config, lock = files
        code = sync_mypy_pins.main(
            ["--config", str(config), "--lock", str(lock)]
        )
        assert code == 1
        out = capsys.readouterr().out
        assert "updated" in out
        assert "typer: 0.27.1 -> 0.27.2" in out

    def test_exits_zero_when_in_sync(self, files: tuple[Path, Path]) -> None:
        config, lock = files
        sync_mypy_pins.main(["--config", str(config), "--lock", str(lock)])
        assert (
            sync_mypy_pins.main(["--config", str(config), "--lock", str(lock)])
            == 0
        )

    def test_check_exits_nonzero_on_drift(
        self, files: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
    ) -> None:
        config, lock = files
        code = sync_mypy_pins.main(
            ["--check", "--config", str(config), "--lock", str(lock)]
        )
        assert code == 1
        assert "would update" in capsys.readouterr().out
