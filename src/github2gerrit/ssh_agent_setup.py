# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""
SSH agent-based authentication for github2gerrit.

This module provides functionality to use SSH agent for authentication
instead of writing private keys to disk, which is more secure and
avoids file permission issues in CI environments.

SSH Agent Validation Strategy:
- Early CLI validation defers SSH agent checks to avoid duplicate validation
- Runtime validation in setup_ssh_agent_auth() performs comprehensive checks:
  1. Checks for existing SSH agent (SSH_AUTH_SOCK environment variable)
  2. Validates agent has keys loaded using "ssh-add -l" command
  3. Gracefully falls back to file-based SSH if agent validation fails
  4. Supports starting new agents when private keys are provided

This approach ensures robust SSH authentication while maintaining clean
separation between configuration validation and runtime validation.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import cast

from .gitutils import CommandError
from .gitutils import run_cmd
from .ssh_common import augment_known_hosts_with_bracketed_entries


log = logging.getLogger(__name__)


class SSHAgentError(Exception):
    """Raised when SSH agent operations fail."""


# Error message constants to comply with TRY003
_MSG_PARSE_FAILED = "Failed to parse ssh-agent output"
_MSG_START_FAILED = "Failed to start SSH agent: {error}"
_MSG_NOT_STARTED = "SSH agent not started"
_MSG_ADD_FAILED = "ssh-add failed: {error}"
_MSG_ADD_TIMEOUT = "ssh-add timed out"
_MSG_ADD_KEY_FAILED = "Failed to add key to SSH agent: {error}"
_MSG_SETUP_HOSTS_FAILED = "Failed to setup known hosts: {error}"
_MSG_HOSTS_NOT_CONFIGURED = "Known hosts not configured"
_MSG_LIST_FAILED = "Failed to list keys: {error}"
_MSG_NO_KEYS_LOADED = "No keys were loaded into SSH agent"
_MSG_SSH_AGENT_NOT_FOUND = "ssh-agent not found in PATH"
_MSG_SSH_ADD_NOT_FOUND = "ssh-add not found in PATH"
_MSG_TOOL_NOT_FOUND = "Required tool '{tool_name}' not found in PATH"
_MSG_NO_AGENT_AND_KEY = "No SSH agent and no key provided"


def _raise_no_keys_error() -> None:
    """Raise error when no keys are loaded."""
    raise SSHAgentError(_MSG_NO_KEYS_LOADED)


def _raise_no_agent_error() -> None:
    """Raise error when no SSH agent found and no key provided."""
    raise SSHAgentError(_MSG_NO_AGENT_AND_KEY)


class SSHAgentManager:
    """Manages SSH agent lifecycle and key loading for secure authentication."""

    def __init__(self, workspace: Path):
        """Initialize SSH agent manager.

        This class manages both SSH agent lifecycle and secure file storage.
        All instances require a workspace for consistent behavior.

        Args:
            workspace: Secure temporary directory for storing SSH files (outside
                git workspace). Required for all operations including
                known_hosts management and secure cleanup.
        """
        self.workspace = workspace
        self.agent_pid: int | None = None
        self.auth_sock: str | None = None
        self.known_hosts_path: Path | None = None
        self._original_env: dict[str, str] = {}
        self._agent_owned_by_us: bool = False

    def start_agent(self) -> None:
        """Start a new SSH agent process."""
        try:
            # Locate ssh-agent executable
            ssh_agent_path = _ensure_tool_available("ssh-agent")

            # Start ssh-agent and capture its output
            result = run_cmd([ssh_agent_path, "-s"], timeout=10)

            # Parse the ssh-agent output to get environment variables
            for line in result.stdout.strip().split("\n"):
                if line.startswith("SSH_AUTH_SOCK="):
                    # Format: SSH_AUTH_SOCK=/path/to/socket; export
                    # SSH_AUTH_SOCK;
                    value = line.split("=", 1)[1].split(";")[0].strip()
                    self.auth_sock = value
                elif line.startswith("SSH_AGENT_PID="):
                    # Format: SSH_AGENT_PID=12345; export SSH_AGENT_PID;
                    value = line.split("=", 1)[1].split(";")[0].strip()
                    self.agent_pid = int(value)
                    self._agent_owned_by_us = True  # We started this agent

            if not self.auth_sock or not self.agent_pid:
                _raise_parse_error()

            # Store original environment
            self._original_env = {
                "SSH_AUTH_SOCK": os.environ.get("SSH_AUTH_SOCK", ""),
                "SSH_AGENT_PID": os.environ.get("SSH_AGENT_PID", ""),
            }

            # Set environment variables for this process
            if self.auth_sock:
                os.environ["SSH_AUTH_SOCK"] = self.auth_sock
            if self.agent_pid:
                os.environ["SSH_AGENT_PID"] = str(self.agent_pid)

            log.debug(
                "Started SSH agent with PID %d, socket %s",
                self.agent_pid,
                self.auth_sock,
            )

        except Exception as exc:
            raise SSHAgentError(_MSG_START_FAILED.format(error=exc)) from exc

    def use_existing_agent(self) -> bool:
        """Attempt to use an existing SSH agent.

        Returns:
            True if existing SSH agent is available and usable, False otherwise
        """
        try:
            # Check if SSH_AUTH_SOCK environment variable is set
            auth_sock = os.environ.get("SSH_AUTH_SOCK")
            if not auth_sock:
                log.debug("No SSH_AUTH_SOCK environment variable found")
                return False

            # Check if the socket file exists
            if not os.path.exists(auth_sock):
                log.debug("SSH agent socket does not exist: %s", auth_sock)
                return False

            # Try to list keys to verify agent is working
            # Use original environment to preserve SSH_AUTH_SOCK
            ssh_add_path = _ensure_tool_available("ssh-add")
            result = subprocess.run(  # noqa: S603
                [ssh_add_path, "-l"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
                env=os.environ.copy(),  # Use original environment
            )

            if result.returncode != 0:
                log.debug(
                    "ssh-add failed with exit code %d: %s",
                    result.returncode,
                    result.stderr,
                )
                return False

            # Store the existing agent info
            self.auth_sock = auth_sock
            # Try to get PID from environment for informational purposes only
            agent_pid_str = os.environ.get("SSH_AGENT_PID")
            if agent_pid_str and agent_pid_str.isdigit():
                self.agent_pid = int(agent_pid_str)
            else:
                self.agent_pid = None
            self._agent_owned_by_us = False  # We're borrowing this agent

            log.debug(
                "Successfully connected to existing SSH agent: %s", auth_sock
            )
            log.debug("Existing agent keys: %s", result.stdout.strip())

        except Exception as exc:
            log.debug("Failed to connect to existing SSH agent: %s", exc)
            return False

        return True

    def add_key(self, private_key_content: str) -> None:
        """Add a private key to the SSH agent.

        Args:
            private_key_content: The private key content as a string
        """
        if not self.auth_sock:
            raise SSHAgentError(_MSG_NOT_STARTED)

        # Locate ssh-add executable
        ssh_add_path = _ensure_tool_available("ssh-add")

        process = None
        try:
            # Use ssh-add with stdin to add the key
            # Security: ssh_add_path is validated by _ensure_tool_available()
            # which uses shutil.which() to find the actual ssh-add binary
            process = subprocess.Popen(  # noqa: S603  # ssh_add_path validated by _ensure_tool_available via shutil.which
                [ssh_add_path, "-"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                shell=False,  # Explicitly disable shell for security
                env=self._get_ssh_env(),
            )

            _stdout, stderr = process.communicate(
                input=private_key_content.strip() + "\n", timeout=10
            )

            if process.returncode != 0:
                _raise_add_key_error(stderr)

            log.debug("Successfully added SSH key to agent")

        except subprocess.TimeoutExpired as exc:
            if process:
                process.kill()
            raise SSHAgentError(_MSG_ADD_TIMEOUT) from exc
        except Exception as exc:
            raise SSHAgentError(_MSG_ADD_KEY_FAILED.format(error=exc)) from exc

    def setup_known_hosts(self, known_hosts_content: str) -> None:
        """Setup known hosts file.

        Args:
            known_hosts_content: The known hosts content
        """
        try:
            # Create tool-specific SSH directory in secure temp location
            # Note: workspace is now a separate secure temp directory outside
            # git workspace
            workspace = self.workspace
            tool_ssh_dir = workspace / ".ssh-g2g"
            tool_ssh_dir.mkdir(mode=0o700, exist_ok=True)

            # Write known hosts file (normalize/augment with [host]:port
            # entries)
            self.known_hosts_path = tool_ssh_dir / "known_hosts"
            host = (os.getenv("GERRIT_SERVER") or "").strip()
            port = (os.getenv("GERRIT_SERVER_PORT") or "29418").strip()
            try:
                port_int = int(port)
            except Exception:
                port_int = 29418

            # Use centralized augmentation logic
            augmented_content = augment_known_hosts_with_bracketed_entries(
                known_hosts_content, host, port_int
            )

            with open(self.known_hosts_path, "w", encoding="utf-8") as f:
                f.write(augmented_content)
            self.known_hosts_path.chmod(0o644)

            log.debug("Known hosts written to %s", self.known_hosts_path)

        except Exception as exc:
            raise SSHAgentError(
                _MSG_SETUP_HOSTS_FAILED.format(error=exc)
            ) from exc

    def get_git_ssh_command(self) -> str:
        """Generate GIT_SSH_COMMAND for SSH agent-based authentication.

        Returns:
            SSH command string for git operations
        """
        if not self.known_hosts_path:
            raise SSHAgentError(_MSG_HOSTS_NOT_CONFIGURED)

        # Check if we should respect user SSH config
        respect_user_ssh = os.getenv(
            "G2G_RESPECT_USER_SSH", "false"
        ).lower() in ("true", "1", "yes")

        ssh_options = []

        if not respect_user_ssh:
            ssh_options.append("-F /dev/null")

        ssh_options.extend(
            [
                f"-o UserKnownHostsFile={self.known_hosts_path}",
                "-o IdentitiesOnly=no",  # Allow SSH agent
                "-o BatchMode=yes",
                "-o PreferredAuthentications=publickey",
                "-o StrictHostKeyChecking=yes",
                "-o PasswordAuthentication=no",
                "-o PubkeyAcceptedKeyTypes=+ssh-rsa",
                "-o ConnectTimeout=10",
            ]
        )

        return f"ssh {' '.join(ssh_options)}"

    def _get_ssh_env(self) -> dict[str, str]:
        """Get SSH environment variables for internal subprocess calls.

        Returns:
            Dictionary of environment variables
        """
        if not self.auth_sock:
            raise SSHAgentError(_MSG_NOT_STARTED)

        env = {
            **os.environ,
            "SSH_AUTH_SOCK": self.auth_sock,
        }

        # Only set SSH_AGENT_PID if we have one (borrowed agents might not)
        if self.agent_pid is not None:
            env["SSH_AGENT_PID"] = str(self.agent_pid)

        return env

    def get_ssh_env(self) -> dict[str, str]:
        """Get SSH environment variables for subprocess calls.

        Returns:
            Dictionary of environment variables
        """
        if not self.auth_sock:
            raise SSHAgentError(_MSG_NOT_STARTED)

        env = {"SSH_AUTH_SOCK": self.auth_sock}

        # Only set SSH_AGENT_PID if we have one (borrowed agents might not)
        if self.agent_pid is not None:
            env["SSH_AGENT_PID"] = str(self.agent_pid)

        return env

    def list_keys(self) -> str:
        """List keys currently loaded in the agent using 'ssh-add -l'.

        This method performs the actual SSH agent validation by executing
        'ssh-add -l' to check if any SSH keys are loaded in the agent.
        It's used throughout the SSH setup process to validate agent state
        and ensure SSH operations will succeed.

        Returns:
            Output from ssh-add -l, or "No keys loaded" if agent has no keys

        Raises:
            SSHAgentError: If SSH agent is not started or ssh-add fails
        """
        if not self.auth_sock:
            raise SSHAgentError(_MSG_NOT_STARTED)

        try:
            # Locate ssh-add executable
            ssh_add_path = _ensure_tool_available("ssh-add")

            result = run_cmd(
                [ssh_add_path, "-l"],
                env=self._get_ssh_env(),
                timeout=5,
            )
        except CommandError as exc:
            if exc.returncode == 1:
                return "No keys loaded"
            raise SSHAgentError(_MSG_LIST_FAILED.format(error=exc)) from exc
        except Exception as exc:
            raise SSHAgentError(_MSG_LIST_FAILED.format(error=exc)) from exc
        else:
            return result.stdout

    def cleanup(self) -> None:
        """Securely clean up SSH agent and temporary files."""
        try:
            # Only kill SSH agent if we started it ourselves
            # Never kill an existing SSH agent that we're borrowing
            if self.agent_pid and self._agent_owned_by_us:
                try:
                    run_cmd(["/bin/kill", str(self.agent_pid)], timeout=5)
                    log.debug("SSH agent (PID %d) terminated", self.agent_pid)
                except Exception as exc:
                    log.warning("Failed to kill SSH agent: %s", exc)
            else:
                if self.agent_pid and not self._agent_owned_by_us:
                    log.debug(
                        "Not terminating SSH agent (borrowed existing agent "
                        "with PID %d)",
                        self.agent_pid,
                    )
                else:
                    log.debug("Not terminating SSH agent (no agent running)")

            # Restore original environment
            for key, value in self._original_env.items():
                if value:
                    os.environ[key] = value
                elif key in os.environ:
                    del os.environ[key]

            # Securely clean up temporary files
            tool_ssh_dir = self.workspace / ".ssh-g2g"
            if tool_ssh_dir.exists():
                import shutil

                # First, overwrite any key files to prevent recovery
                try:
                    for root, _dirs, files in os.walk(tool_ssh_dir):
                        for file in files:
                            file_path = Path(root) / file
                            if file_path.exists() and file_path.is_file():
                                # Overwrite file with random data
                                try:
                                    size = file_path.stat().st_size
                                    if size > 0:
                                        import secrets

                                        with open(file_path, "wb") as f:
                                            f.write(secrets.token_bytes(size))
                                            # Sync to ensure write completes
                                            os.fsync(f.fileno())
                                except Exception as overwrite_exc:
                                    log.debug(
                                        "Failed to overwrite %s: %s",
                                        file_path,
                                        overwrite_exc,
                                    )
                except Exception as walk_exc:
                    log.debug(
                        "Failed to walk SSH temp directory for secure "
                        "cleanup: %s",
                        walk_exc,
                    )

                shutil.rmtree(tool_ssh_dir)
                log.debug(
                    "Securely cleaned up temporary SSH directory: %s",
                    tool_ssh_dir,
                )

        except Exception as exc:
            log.warning("Failed to clean up SSH agent: %s", exc)
        finally:
            self.agent_pid = None
            self.auth_sock = None
            self.known_hosts_path = None
            self._agent_owned_by_us = False


def setup_ssh_agent_auth(
    workspace: Path, private_key_content: str, known_hosts_content: str
) -> SSHAgentManager:
    """Setup SSH agent-based authentication with comprehensive validation.

    This function performs the runtime SSH agent validation that was deferred
    from early CLI validation to avoid duplicate checks. It implements a
    robust validation and fallback strategy:

    1. Check for existing SSH agent (SSH_AUTH_SOCK environment variable)
    2. Validate existing agent has keys loaded using "ssh-add -l"
    3. If existing agent has no keys but private key provided, start new agent
    4. If no existing agent, start new agent with provided private key
    5. Fail gracefully if no agent available and no private key provided

    The validation using "ssh-add -l" ensures that SSH operations will succeed
    and helps prevent authentication failures during Git operations.

    Args:
        workspace: Secure temporary directory for SSH files (outside git
            workspace)
        private_key_content: SSH private key content (empty string to use
            existing agent only)
        known_hosts_content: Known hosts content for SSH host verification

    Returns:
        SSHAgentManager instance for the configured agent

    Raises:
        SSHAgentError: If SSH agent setup fails (no agent + no key, or
                      validation failures)
    """
    manager = SSHAgentManager(workspace)

    try:
        # First, always try to use existing SSH agent if available
        log.debug("Checking for existing SSH agent...")
        log.debug(
            "SSH_AUTH_SOCK environment variable: %s",
            os.environ.get("SSH_AUTH_SOCK"),
        )
        if manager.use_existing_agent():
            log.debug("Using existing SSH agent successfully")
            # Setup known hosts for existing agent
            manager.setup_known_hosts(known_hosts_content)

            # Verify existing agent has keys using "ssh-add -l"
            # This validation ensures SSH operations will succeed
            keys_list = manager.list_keys()
            if "No keys loaded" in keys_list:
                log.debug("Existing SSH agent has no keys loaded")
                # If we have a private key, we can start a new agent
                if private_key_content.strip():
                    log.debug(
                        "Starting new SSH agent with provided private key"
                    )
                    # Fall through to start new agent
                else:
                    log.debug("No private key provided for new agent")
                    _raise_no_keys_error()
            else:
                log.debug(
                    "SSH agent auth configured successfully (existing agent)"
                )
                log.debug("Loaded keys: %s", keys_list)
                return manager
        else:
            log.debug("No existing SSH agent found")

        # Only start new SSH agent if we have a private key to load
        if not private_key_content.strip():
            log.debug(
                "No private key provided and no existing SSH agent available"
            )
            _raise_no_agent_error()

        # Start new SSH agent and add the private key
        log.debug("Starting new SSH agent with provided private key")
        manager.start_agent()

        # Add the private key
        manager.add_key(private_key_content)

        # Setup known hosts
        manager.setup_known_hosts(known_hosts_content)

        # Verify key was added successfully using "ssh-add -l"
        # This final validation ensures the agent is ready for SSH operations
        keys_list = manager.list_keys()
        if "No keys loaded" in keys_list:
            _raise_no_keys_error()

        log.debug(
            "SSH agent authentication configured successfully (new agent)"
        )
        log.debug("Loaded keys: %s", keys_list)

    except Exception:
        # Clean up on failure
        manager.cleanup()
        raise
    else:
        return manager


def _raise_parse_error() -> None:
    """Raise SSH agent parse error."""
    raise SSHAgentError(_MSG_PARSE_FAILED)


def _raise_add_key_error(stderr: str) -> None:
    """Raise SSH key addition error."""
    raise SSHAgentError(_MSG_ADD_FAILED.format(error=stderr))


def _ensure_tool_available(tool_name: str) -> str:
    """Ensure a required tool is available and return its path.

    Args:
        tool_name: Name of the tool to locate

    Returns:
        Path to the tool executable

    Raises:
        SSHAgentError: If the tool is not found
    """
    tool_path = shutil.which(tool_name)
    if not tool_path:
        if tool_name == "ssh-agent":
            _raise_ssh_agent_not_found()
        elif tool_name == "ssh-add":
            _raise_ssh_add_not_found()
        else:
            raise SSHAgentError(_MSG_TOOL_NOT_FOUND.format(tool_name=tool_name))
    # At this point, tool_path is guaranteed not to be None
    # (the above conditions raise exceptions if it was None)
    return cast(str, tool_path)


def _raise_ssh_agent_not_found() -> None:
    """Raise SSH agent not found error."""
    raise SSHAgentError(_MSG_SSH_AGENT_NOT_FOUND)


def _raise_ssh_add_not_found() -> None:
    """Raise SSH add not found error."""
    raise SSHAgentError(_MSG_SSH_ADD_NOT_FOUND)
