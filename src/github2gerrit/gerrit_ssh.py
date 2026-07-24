# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""
SSH-based Gerrit operations.

Some Gerrit deployments (notably those fronted by a CDN/WAF, or that
disallow anonymous REST writes) reject mutating REST calls such as
``POST /changes/{id}/abandon`` with HTTP 403 unless dedicated HTTP API
credentials are supplied.  The github2gerrit action authenticates to
Gerrit over SSH (the same channel used to push changes), so this module
performs the abandon over SSH instead, reusing the already-configured
SSH key and known_hosts.

The single public entry point :func:`abandon_change_via_ssh` is designed
to be used as the preferred path with a REST fallback: it never raises
and returns ``True`` only when the change was actually abandoned.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import shlex
import tempfile
from pathlib import Path

from .gitutils import CommandError
from .gitutils import run_cmd
from .ssh_common import augment_known_hosts_with_bracketed_entries
from .ssh_common import build_non_interactive_ssh_env


log = logging.getLogger(__name__)

DEFAULT_GERRIT_SSH_PORT = 29418
_SSH_TIMEOUT_SECONDS = 30.0


def _write_secure_file(path: Path, content: str, mode: int) -> None:
    """Write *content* to *path* with restrictive *mode* permissions."""
    # Create with secure permissions from the start (avoid a window where
    # the file is world-readable).
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        handle = os.fdopen(fd, "w", encoding="utf-8")
    except BaseException:
        # os.fdopen did not take ownership of fd; close it to avoid a leak.
        os.close(fd)
        raise
    with handle:
        handle.write(content)
    # Re-assert mode in case umask altered it.
    path.chmod(mode)


def _build_ssh_base_argv(
    *,
    key_path: Path,
    known_hosts_path: Path | None,
    port: int,
    user: str,
    host: str,
) -> list[str]:
    """Build the common ``ssh`` argv prefix for non-interactive auth."""
    argv: list[str] = [
        "ssh",
        "-F",
        "/dev/null",
        "-i",
        str(key_path),
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "IdentityAgent=none",
        "-o",
        "BatchMode=yes",
        "-o",
        "PreferredAuthentications=publickey",
        "-o",
        "PasswordAuthentication=no",
        "-o",
        "PubkeyAcceptedKeyTypes=+ssh-rsa",
        "-o",
        "ConnectTimeout=10",
    ]
    if known_hosts_path is not None:
        argv += [
            "-o",
            f"UserKnownHostsFile={known_hosts_path}",
            "-o",
            "StrictHostKeyChecking=yes",
        ]
    else:
        # No known_hosts supplied: we cannot verify the host key, but we
        # still want a non-interactive connection.  accept-new records the
        # key on first use; pair it with an explicit (throwaway)
        # UserKnownHostsFile so OpenSSH does not mutate the runner's
        # default ~/.ssh/known_hosts (which may be missing/unwritable).
        log.warning(
            "No GERRIT_KNOWN_HOSTS available for SSH abandon; using "
            "StrictHostKeyChecking=accept-new with a throwaway known_hosts"
        )
        argv += [
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "StrictHostKeyChecking=accept-new",
        ]
    argv += ["-n", "-p", str(port), f"{user}@{host}"]
    return argv


def _resolve_current_patchset(
    base_argv: list[str],
    change_number: str,
    env: dict[str, str],
) -> str | None:
    """Return the current patch-set number for *change_number*, or None."""
    remote_cmd = (
        "gerrit query --format=JSON --current-patch-set "
        f"change:{shlex.quote(change_number)}"
    )
    try:
        result = run_cmd(
            [*base_argv, remote_cmd],
            timeout=_SSH_TIMEOUT_SECONDS,
            env=env,
        )
    except CommandError as exc:
        log.debug(
            "Gerrit SSH query for change %s failed: %s",
            change_number,
            exc,
        )
        return None

    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except (ValueError, TypeError):
            continue
        patch_set = record.get("currentPatchSet")
        if isinstance(patch_set, dict) and patch_set.get("number") is not None:
            return str(patch_set["number"])
    log.debug(
        "No currentPatchSet found in Gerrit query output for change %s",
        change_number,
    )
    return None


def abandon_change_via_ssh(
    *,
    host: str,
    change_number: str,
    message: str,
    user: str,
    ssh_privkey: str,
    known_hosts: str | None = None,
    port: int = DEFAULT_GERRIT_SSH_PORT,
) -> bool:
    """Abandon a Gerrit change over SSH using ``gerrit review --abandon``.

    This is the preferred abandon path because it uses the same SSH
    credentials that the action already relies on to push changes, and
    therefore works on Gerrit servers that reject unauthenticated REST
    writes.

    Args:
        host: Gerrit SSH hostname (no scheme).
        change_number: Numeric Gerrit change number.
        message: Abandon message (may be multi-line).
        user: Gerrit SSH username.
        ssh_privkey: SSH private key content.
        known_hosts: Optional known_hosts content for host verification.
        port: Gerrit SSH port (default 29418).

    Returns:
        ``True`` if the change was abandoned successfully; ``False`` when
        prerequisites are missing or the SSH operation failed (so the
        caller may fall back to another mechanism).  This function never
        raises.
    """
    if not (host and user and ssh_privkey and str(change_number).strip()):
        log.debug(
            "SSH abandon prerequisites missing (host=%s, user=%s, "
            "key=%s, change=%s); skipping SSH abandon",
            bool(host),
            bool(user),
            bool(ssh_privkey),
            change_number,
        )
        return False

    change_number = str(change_number).strip()
    try:
        tmp_dir = Path(
            tempfile.mkdtemp(prefix=f"g2g_abandon_{secrets.token_hex(8)}_")
        )
    except OSError as exc:
        # Honor the "never raises" contract so callers can fall back to REST.
        log.debug("Could not create temp dir for SSH abandon: %s", exc)
        return False
    try:
        tmp_dir.chmod(0o700)
        key_path = tmp_dir / "gerrit_key"
        _write_secure_file(key_path, ssh_privkey.strip() + "\n", 0o600)

        known_hosts_path: Path | None = None
        if known_hosts and known_hosts.strip():
            # OpenSSH looks up host keys under a bracketed "[host]:port"
            # entry when connecting on a non-default port (Gerrit uses
            # 29418).  Augment the provided content with bracketed variants
            # so StrictHostKeyChecking can verify the key and we don't fall
            # back to REST unnecessarily.
            augmented = augment_known_hosts_with_bracketed_entries(
                known_hosts.strip(), host, port
            )
            known_hosts_path = tmp_dir / "known_hosts"
            _write_secure_file(known_hosts_path, augmented, 0o644)

        base_argv = _build_ssh_base_argv(
            key_path=key_path,
            known_hosts_path=known_hosts_path,
            port=port,
            user=user,
            host=host,
        )

        # Disable any ambient SSH agent so only the provided key is used.
        ssh_env = build_non_interactive_ssh_env()

        patch_set = _resolve_current_patchset(base_argv, change_number, ssh_env)
        if patch_set is None:
            log.debug(
                "Could not resolve current patch-set for change %s via SSH",
                change_number,
            )
            return False

        target = f"{change_number},{patch_set}"
        remote_cmd = (
            "gerrit review --abandon "
            f"-m {shlex.quote(message)} {shlex.quote(target)}"
        )
        try:
            run_cmd(
                [*base_argv, remote_cmd],
                timeout=_SSH_TIMEOUT_SECONDS,
                env=ssh_env,
            )
        except CommandError as exc:
            log.warning(
                "SSH abandon failed for change %s: %s",
                change_number,
                exc,
            )
            return False
        else:
            log.debug(
                "Successfully abandoned Gerrit change %s via SSH",
                change_number,
            )
            return True
    except Exception:
        log.warning(
            "Unexpected error during SSH abandon for change %s",
            change_number,
            exc_info=True,
        )
        return False
    finally:
        try:
            import shutil

            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception as exc:
            log.debug("Failed to clean up SSH temp dir %s: %s", tmp_dir, exc)
