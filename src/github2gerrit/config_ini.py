# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation
"""INI parsing for the github2gerrit configuration file.

The file format :mod:`github2gerrit.config` reads is INI with two
liberties configparser does not take on its own: values may reference
environment variables as ``${ENV:VAR}``, and a quoted value may span
several lines, which is how SSH private keys and known-hosts entries
are written inline.  This module turns such a file into a
:class:`configparser.RawConfigParser` and normalises the values it
yields.  It knows nothing about which keys mean what; that stays in
:mod:`github2gerrit.config`.
"""

from __future__ import annotations

import configparser
import logging
import os
import re
from pathlib import Path
from typing import Any
from typing import cast


log = logging.getLogger("github2gerrit.config")

_ENV_REF = re.compile(r"\$\{ENV:([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env_refs(value: str) -> str:
    """Expand ${ENV:VAR} references using current environment."""

    def repl(match: re.Match[str]) -> str:
        var = match.group(1)
        return os.getenv(var, "") or ""

    return _ENV_REF.sub(repl, value)


def _strip_quotes(value: str) -> str:
    v = value.strip()
    if len(v) >= 2 and ((v[0] == v[-1] == '"') or (v[0] == v[-1] == "'")):
        return v[1:-1]
    return v


def _normalize_bool_like(value: str) -> str | None:
    """Return 'true'/'false' for boolean-like values, else None."""
    s = value.strip().lower()
    if s in {"1", "true", "yes", "on"}:
        return "true"
    if s in {"0", "false", "no", "off"}:
        return "false"
    return None


def _coerce_value(raw: str) -> str:
    """Coerce a raw string to normalized representation."""
    expanded = _expand_env_refs(raw)
    unquoted = _strip_quotes(expanded)
    # Normalize escaped newline sequences into real newlines so that values
    # like SSH keys or known_hosts entries can be specified inline using
    # '\n' or '\r\n' in configuration files.
    normalized_newlines = (
        unquoted.replace("\\r\\n", "\n")
        .replace("\\n", "\n")
        .replace("\r\n", "\n")
    )

    # Additional sanitization for SSH private keys
    if (
        "-----BEGIN" in normalized_newlines
        and "PRIVATE KEY-----" in normalized_newlines
    ) or (
        "ssh-" in normalized_newlines.lower()
        and "key" in normalized_newlines.lower()
    ):
        # Clean up SSH key formatting: remove extra whitespace, normalize
        # line endings
        lines = normalized_newlines.split("\n")
        sanitized_lines = []
        for line in lines:
            cleaned = line.strip()
            if cleaned:
                # Remove any stray quotes that might have been embedded in the
                # key content
                cleaned = cleaned.replace('"', "").replace("'", "")
                sanitized_lines.append(cleaned)
        normalized_newlines = "\n".join(sanitized_lines)

    b = _normalize_bool_like(normalized_newlines)
    return b if b is not None else normalized_newlines


def _select_section(
    cp: configparser.RawConfigParser,
    org: str,
) -> str | None:
    """Find a section name case-insensitively."""
    target = org.strip().lower()
    for sec in cp.sections():
        if sec.strip().lower() == target:
            return sec
    return None


def _sanitize_ssh_key_content(content_lines: list[str]) -> str:
    """Clean the base64 content of an inline multi-line SSH key value."""
    sanitized_lines: list[str] = []
    for content_line in content_lines:
        cleaned = content_line.strip()
        # Preserve SSH key headers/footers but clean base64 content
        if cleaned.startswith("-----") or not cleaned:
            sanitized_lines.append(cleaned)
            continue
        # Remove embedded quotes and all whitespace from base64 content.
        # Base64 bodies contain no whitespace, so stripping any embedded
        # spaces/tabs (e.g. from wrapped copy-paste) repairs the content
        # rather than corrupting it. Headers/footers are preserved above.
        cleaned = cleaned.replace('"', "").replace("'", "")
        cleaned = "".join(cleaned.split())
        if cleaned:
            sanitized_lines.append(cleaned)
    return "\\n".join(sanitized_lines)


def _consume_multiline_quote(
    lines: list[str],
    start: int,
    left: str,
    out_lines: list[str],
) -> int:
    """Collapse a `key = "` ... `"` block into a single escaped line.

    Returns the index of the next unprocessed line.
    """
    i = start + 1
    block: list[str] = []
    # Collect until a line with only a closing quote (ignoring spaces)
    while i < len(lines) and lines[i].strip() != '"':
        block.append(lines[i])
        i += 1
    if i < len(lines) and lines[i].strip() == '"':
        joined = "\\n".join(block)
        out_lines.append(f'{left} "{joined}"')
        return i + 1
    # No closing quote found; keep the original opening line.
    log.debug(
        "Multi-line quote not properly closed for line: %s",
        lines[start][:50],
    )
    out_lines.append(lines[start])
    return i


def _consume_inline_quote(
    lines: list[str],
    start: int,
    left: str,
    rhs: str,
    out_lines: list[str],
) -> int:
    """Collapse a value that opens with `"` but spans multiple lines.

    Handles SSH private keys and other values that start with a quote but
    contain embedded content that might otherwise confuse configparser.
    Returns the index of the next unprocessed line.
    """
    content_lines = [rhs[1:]]  # Remove opening quote
    i = start + 1
    while i < len(lines):
        current_line = lines[i]
        stripped = current_line.strip()
        if stripped.endswith('"') and not stripped.endswith('\\"'):
            # Found closing quote - remove it and add final line
            final_content = current_line.rstrip()
            if final_content.endswith('"'):
                final_content = final_content[:-1]
            # Only add if there's content after removing quote
            if final_content:
                content_lines.append(final_content)
            break
        content_lines.append(current_line)
        i += 1

    # Join all content and sanitize for SSH keys
    full_content = "\\n".join(content_lines)

    # Special handling for SSH private keys - remove extra whitespace
    # and line breaks
    key_name = left.split("=")[0].strip().upper()
    if "SSH" in key_name and "KEY" in key_name:
        full_content = _sanitize_ssh_key_content(content_lines)

    log.debug(
        "Processed multi-line value for key %s (length: %d)",
        left.split("=")[0].strip(),
        len(full_content),
    )
    out_lines.append(f'{left} "{full_content}"')
    return i + 1


def _preprocess_config_text(raw_text: str) -> str:
    """Collapse multi-line quoted values into single escaped lines.

    Pre-process simple multi-line quoted values of the form::

        key = "
        line1
        line2
        "

    We collapse these into a single line with '\\n' escapes so that
    configparser can ingest them reliably; later, _coerce_value()
    converts the escapes back to real newlines. SSH private keys and
    other multi-line values with formatting inconsistencies are
    sanitized as part of this process.
    """
    lines = raw_text.splitlines()
    out_lines: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        eq_idx = line.find("=")
        if eq_idx == -1:
            out_lines.append(line)
            i += 1
            continue

        left = line[: eq_idx + 1]
        rhs = line[eq_idx + 1 :].strip()

        # Handle standard multi-line quoted values: key = "
        if rhs == '"':
            i = _consume_multiline_quote(lines, i, left, out_lines)
            continue

        if rhs.startswith('"') and not rhs.endswith('"'):
            i = _consume_inline_quote(lines, i, left, rhs, out_lines)
            continue

        out_lines.append(line)
        i += 1

    return "\n".join(out_lines) + ("\n" if out_lines else "")


def _load_ini(path: Path) -> configparser.RawConfigParser:
    cp = configparser.RawConfigParser()
    # Preserve option case; mypy requires a cast for attribute requirement
    cast(Any, cp).optionxform = str
    try:
        with path.open("r", encoding="utf-8") as fh:
            raw_text = fh.read()
        preprocessed = _preprocess_config_text(raw_text)
        cp.read_string(preprocessed)
    except FileNotFoundError as exc:
        log.debug("Config file not found: %s (%s)", path, exc)
    except Exception as exc:
        log.warning("Failed to read config file %s: %s", path, exc)
    return cp
