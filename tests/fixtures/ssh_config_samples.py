# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2025 The Linux Foundation

"""
Sample SSH configurations for testing.

This module provides various SSH configuration samples to test different
scenarios and patterns in SSH config parsing.
"""

# Basic SSH config with simple host matching
BASIC_SSH_CONFIG = """
Host gerrit.example.com
    User gerrituser
    Port 29418

Host github.com
    User gituser
"""

# SSH config with wildcard patterns
WILDCARD_SSH_CONFIG = """
Host gerrit.*
    User gerritbot
    Port 29418

Host *.example.org
    User corpuser

Host *
    User defaultuser
"""

# Complex SSH config with multiple patterns and precedence
COMPLEX_SSH_CONFIG = """
# Global settings
CanonicalDomains example.org.vpn.net corp.local
CanonicalizeHostname yes
ControlMaster auto
ControlPersist 10m
ServerAliveInterval 120
ForwardAgent yes

Host mailserver server1.example.org server2.example.org
    User admin
    IdentityFile ~/.ssh/mailserver

Host gerrit.*
    User gerritbot
    Port 29418
    HostkeyAlgorithms +ssh-rsa
    PubkeyAcceptedAlgorithms +ssh-rsa

Host git.upstream.org
    User gerritbot
    Port 29418
    HostkeyAlgorithms +ssh-rsa
    PubkeyAcceptedAlgorithms +ssh-rsa

Host github.com
    User GitHubUser

Host *.example.org.vpn.net *.corp.local
    User corpuser

Host *
    User defaultuser
    IdentityAgent /path/to/secretive/socket.ssh
"""

# SSH config with port-specific configurations
PORT_SPECIFIC_SSH_CONFIG = """
Host gerrit.example.com
    User normaluser
    Port 22

Host gerrit.example.com
    User gerrituser
    Port 29418

Host *.example.com
    User wildcarduser
"""

# SSH config with quoted values and special characters
QUOTED_VALUES_SSH_CONFIG = """
Host "gerrit with spaces"
    User "user with spaces"

Host gerrit.example.com
    User regularuser

Host "*.quoted.example.org"
    User quoteduser
"""

# SSH config with multiple host patterns per entry
MULTIPLE_HOSTS_SSH_CONFIG = """
Host gerrit.example.com gerrit.test.com *.gerrit.org
    User multiuser
    Port 29418

Host github.com gitlab.com
    User gituser

Host server1 server2 server3
    User serveruser
"""

# SSH config with case variations
CASE_INSENSITIVE_SSH_CONFIG = """
HOST gerrit.example.com
    USER testuser
    PORT 29418

host github.com
    user gituser

Host *.EXAMPLE.ORG
    User corpuser
"""

# Minimal SSH config for testing empty/missing scenarios
MINIMAL_SSH_CONFIG = """
# This is just a comment
# No actual configuration
"""

# SSH config with only global settings
GLOBAL_ONLY_SSH_CONFIG = """
# Global settings only
CanonicalizeHostname yes
ControlMaster auto
ControlPersist 10m
ServerAliveInterval 120
ForwardAgent yes

# No Host entries
"""

# SSH config for testing precedence rules
PRECEDENCE_SSH_CONFIG = """
Host gerrit.specific.com
    User specificuser

Host gerrit.*
    User wildcarduser

Host *.example.com
    User domainuser

Host *
    User defaultuser
"""

# SSH config with comments and empty lines
COMMENTED_SSH_CONFIG = """
# SSH configuration file
# This is a test configuration

Host gerrit.*
    # This is a comment inside a host block
    User gerritbot
    Port 29418
    # Another comment

# Empty line above
Host github.com
    User githubuser
    # Comment at end of block

# Final comment
"""

# SSH config simulating corporate environment
CORPORATE_SSH_CONFIG = """
# Corporate SSH configuration
Host jumphost.corp.local
    User jumpuser
    ProxyCommand none

Host *.internal.corp.local
    User internaluser
    ProxyJump jumphost.corp.local

Host gerrit.corp.local
    User gerritservice
    Port 29418

Host git.*.corp.local
    User gitservice
    Port 29418

Host *.corp.local
    User corpuser

Host *
    User defaultuser
"""

# SSH config for testing various authentication methods
AUTH_METHODS_SSH_CONFIG = """
Host gerrit.example.com
    User gerrituser
    IdentityFile ~/.ssh/gerrit_key
    IdentityAgent none

Host github.com
    User gituser
    IdentityAgent /path/to/ssh/agent

Host secure.example.org
    User secureuser
    IdentitiesOnly yes
    PasswordAuthentication no

Host *
    User defaultuser
    IdentityAgent /path/to/default/agent
"""

# SSH config for testing error conditions
MALFORMED_SSH_CONFIG = """
# This config has some malformed entries for testing
Host gerrit.example.com
    User gerrituser
    # This line is incomplete
    Port

# Missing host pattern
Host
    User orphanuser

# Valid entry after malformed ones
Host github.com
    User gituser
"""

# Collection of all sample configs for easy access
SSH_CONFIG_SAMPLES = {
    "basic": BASIC_SSH_CONFIG,
    "wildcard": WILDCARD_SSH_CONFIG,
    "complex": COMPLEX_SSH_CONFIG,
    "port_specific": PORT_SPECIFIC_SSH_CONFIG,
    "quoted_values": QUOTED_VALUES_SSH_CONFIG,
    "multiple_hosts": MULTIPLE_HOSTS_SSH_CONFIG,
    "case_insensitive": CASE_INSENSITIVE_SSH_CONFIG,
    "minimal": MINIMAL_SSH_CONFIG,
    "global_only": GLOBAL_ONLY_SSH_CONFIG,
    "precedence": PRECEDENCE_SSH_CONFIG,
    "commented": COMMENTED_SSH_CONFIG,
    "corporate": CORPORATE_SSH_CONFIG,
    "auth_methods": AUTH_METHODS_SSH_CONFIG,
    "malformed": MALFORMED_SSH_CONFIG,
}

# Expected results for testing host matching
SSH_CONFIG_TEST_CASES = {
    "basic": {
        "gerrit.example.com": "gerrituser",
        "github.com": "gituser",
        "unknown.com": None,
    },
    "wildcard": {
        "gerrit.example.com": "gerritbot",
        "gerrit.test.org": "gerritbot",
        "test.example.org": "corpuser",
        "random.com": "defaultuser",
    },
    "complex": {
        "gerrit.example.org": "gerritbot",
        "git.upstream.org": "gerritbot",
        "github.com": "GitHubUser",
        "test.example.org.vpn.net": "corpuser",
        "example.corp.local": "corpuser",
        "random.example.com": "defaultuser",
    },
    "precedence": {
        "gerrit.specific.com": "specificuser",
        "gerrit.other.com": "wildcarduser",
        "other.example.com": "domainuser",
        "random.other.org": "defaultuser",
    },
}
