<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2025 The Linux Foundation
-->

# github2gerrit

Submit a GitHub pull request to a Gerrit repository, implemented in Python.

This action is a drop‑in replacement for the shell‑based
`lfit/github2gerrit` composite action. It mirrors the same inputs,
outputs, environment variables, and secrets so you can adopt it without
changing existing configuration in your organizations.

The tool expects a `.gitreview` file in the repository to derive Gerrit
connection details and the destination project. It uses `git` over SSH
and `git-review` semantics to push to `refs/for/<branch>` and relies on
Gerrit `Change-Id` trailers to create or update changes.

Note: the initial versions focus on compatibility and clear logging.
The behavior matches the existing action, and this implementation
refactors it to Python with typed modules and test support.

## How it works (high level)

- Discover pull request context and inputs.
- Check for duplicate changes to prevent spam from automated tools.
- Read `.gitreview` for Gerrit host, port, and project.
- Set up `git` user config and SSH for Gerrit.
- Prepare commits:
  - one‑by‑one cherry‑pick with `Change-Id` trailers, or
  - squash into a single commit and keep or reuse `Change-Id`.
- Optionally replace the commit message with PR title and body.
- Push with a topic to `refs/for/<branch>` using `git-review` behavior.
- Query Gerrit for the resulting URL, change number, and patchset SHA.
- Add a back‑reference comment in Gerrit to the GitHub PR and run URL.
- Comment on the GitHub PR with the Gerrit change URL(s).
- Optionally close the PR (mirrors the shell action policy).

## Requirements

- Repository contains a `.gitreview` file. If you cannot provide it,
  you must pass `GERRIT_SERVER`, `GERRIT_SERVER_PORT`, and
  `GERRIT_PROJECT` via the reusable workflow interface.
- SSH key for Gerrit and known hosts are available to the workflow.
- The default `GITHUB_TOKEN` is available for PR metadata and comments.
- The workflow grants permissions required for PR interactions:
  - `pull-requests: write` (to comment on and close PRs)
  - `issues: write` (to create PR comments via the Issues API)
- The workflow runs with `pull_request_target` or via
  `workflow_dispatch` using a valid PR context.

### Note on sitecustomize.py

This repository includes a sitecustomize.py that is automatically
imported by Python’s site initialization. It exists to make pytest and
coverage runs in CI more robust by:

- assigns a unique COVERAGE_FILE per process to avoid mixing data across runs
- proactively removing stale .coverage artifacts in common base directories.

The logic runs during pytest sessions and is best effort.
It never interferes with normal execution. Maintainers can keep it to
stabilize coverage reporting for parallel/xdist runs.

## Duplicate detection

By default, the tool checks for duplicate changes to prevent spam
submissions from automated tools like Dependabot. It compares PR titles,
content, and files changed against recent PRs (last 7 days) and will
exit with an error when it finds duplicates.

### Examples of detected duplicates

- Identical Dependabot PRs: "Bump package from 1.0 to 1.1"
- Sequential dependency updates: "Bump package 1.0→1.1", "Bump package 1.1→1.2"
- Similar bug fixes with slightly different wording

### Allowing duplicates

Use `--allow-duplicates` or set `ALLOW_DUPLICATES=true` to override:

```bash
# CLI usage
github2gerrit --allow-duplicates https://github.com/org/repo

# GitHub Actions
uses: onap/github2gerrit@main
with:
  ALLOW_DUPLICATES: 'true'
```

When allowed, duplicates generate warnings but processing continues.
The tool exits with code 3 when it detects duplicates and they are not allowed.

## Usage

This action runs as part of a workflow that triggers on
`pull_request_target` and also supports manual runs via
`workflow_dispatch`.

Minimal example:

```yaml
name: github2gerrit

on:
  pull_request_target:
    types: [opened, reopened, edited, synchronize]
  workflow_dispatch:

permissions:
  contents: read
  pull-requests: write
  issues: write

jobs:
  submit-to-gerrit:
    runs-on: ubuntu-latest
    steps:
      - name: Submit PR to Gerrit
        id: g2g
        uses: lfit/github2gerrit@main
        with:
          SUBMIT_SINGLE_COMMITS: "false"
          USE_PR_AS_COMMIT: "false"
          FETCH_DEPTH: "10"
          GERRIT_KNOWN_HOSTS: ${{ vars.GERRIT_KNOWN_HOSTS }}
          GERRIT_SSH_PRIVKEY_G2G: ${{ secrets.GERRIT_SSH_PRIVKEY_G2G }}
          GERRIT_SSH_USER_G2G: ${{ vars.GERRIT_SSH_USER_G2G }}
          GERRIT_SSH_USER_G2G_EMAIL: ${{ vars.GERRIT_SSH_USER_G2G_EMAIL }}
          ORGANIZATION: ${{ github.repository_owner }}
          REVIEWERS_EMAIL: ""
          ISSUE_ID: ""  # Optional: adds 'Issue-ID: ...' trailer to the commit message
```

The action reads `.gitreview`. If `.gitreview` is absent, you must
supply Gerrit connection details through a reusable workflow or by
setting the corresponding environment variables before invoking the
action. The shell action enforces `.gitreview` for the composite
variant; this Python action mirrors that behavior for compatibility.

## Command Line Usage and Debugging

### Direct Command Line Usage

You can run the tool directly from the command line to process GitHub pull requests:

```bash
# Process a specific pull request
github2gerrit https://github.com/owner/repo/pull/123

# Process all open pull requests in a repository
github2gerrit https://github.com/owner/repo

# Run in CI mode (reads from environment variables)
github2gerrit
```

### Available Options

```bash
github2gerrit --help
```

Key options include:

- `--verbose` / `-v`: Enable verbose debug logging
- `--dry-run`: Check configuration without making changes
- `--submit-single-commits`: Submit each commit individually
- `--use-pr-as-commit`: Use PR title/body as commit message
- `--issue-id`: Add an Issue-ID trailer (e.g., "Issue-ID: ABC-123")
  to the commit message
- `--preserve-github-prs`: Don't close GitHub PRs after submission

### Debugging and Troubleshooting

When encountering issues, enable verbose logging to see detailed execution:

```bash
# Using the CLI flag
github2gerrit --verbose https://github.com/owner/repo/pull/123

# Using environment variable
G2G_LOG_LEVEL=DEBUG github2gerrit https://github.com/owner/repo/pull/123

# Alternative environment variable
G2G_VERBOSE=true github2gerrit https://github.com/owner/repo/pull/123
```

Debug output includes:

- Git command execution and output
- SSH connection attempts
- Gerrit API interactions
- Branch resolution logic
- Change-Id processing

Common issues and solutions:

1. **Configuration Validation Errors**: The tool provides clear error messages when
   required configuration is missing or invalid. Look for messages starting with
   "Configuration validation failed:" that specify missing inputs like
   `GERRIT_KNOWN_HOSTS`, `GERRIT_SSH_PRIVKEY_G2G`, etc.
2. **SSH Permission Denied**: Ensure `GERRIT_SSH_PRIVKEY_G2G` and
   `GERRIT_KNOWN_HOSTS` are properly set
3. **Branch Not Found**: Check that the target branch exists in both GitHub and Gerrit
4. **Change-Id Issues**: Enable debug logging to see Change-Id generation and validation
5. **Gerrit API Errors**: Verify Gerrit server connectivity and project permissions

> **Note**: The tool displays configuration errors cleanly without Python tracebacks.
> If you see a traceback in the output, please report it as a bug.

### Environment Variables

The tool respects these environment variables for configuration:

- `G2G_LOG_LEVEL`: Set to `DEBUG` for verbose output (default: `INFO`)
- `G2G_VERBOSE`: Set to `true` to enable debug logging
- `GERRIT_SSH_PRIVKEY_G2G`: SSH private key content
- `GERRIT_KNOWN_HOSTS`: SSH known hosts entries
- `GERRIT_SSH_USER_G2G`: Gerrit SSH username
- `DRY_RUN`: Set to `true` for check mode

## Advanced usage

You can explicitly install the SSH key and provide a custom SSH configuration
before invoking this action. This is useful when:

- You want to override the port/host used by SSH
- You need to define host aliases or SSH options
- Your Gerrit instance uses a non-standard HTTP base path (e.g. /r)

Example:

```yaml
name: github2gerrit (advanced)

on:
  pull_request_target:
    types: [opened, reopened, edited, synchronize]
  workflow_dispatch:

permissions:
  contents: read
  pull-requests: write
  issues: write

jobs:
  submit-to-gerrit:
    runs-on: ubuntu-latest
    steps:
      - name: Install SSH key and custom SSH config
        <!-- markdownlint-disable-next-line MD013 -->
        uses: shimataro/ssh-key-action@d4fffb50872869abe2d9a9098a6d9c5aa7d16be4 # v2.7.0
        with:
          key: ${{ secrets.GERRIT_SSH_PRIVKEY_G2G }}
          name: "id_rsa"
          known_hosts: ${{ vars.GERRIT_KNOWN_HOSTS }}
          config: |
            Host ${{ vars.GERRIT_SERVER }}
              User ${{ vars.GERRIT_SSH_USER_G2G }}
              Port ${{ vars.GERRIT_SERVER_PORT }}
              PubkeyAcceptedKeyTypes +ssh-rsa
              IdentityFile ~/.ssh/id_rsa

      - name: Submit PR to Gerrit (with explicit overrides)
        id: g2g
        uses: lfit/github2gerrit@main
        with:
          # Behavior
          SUBMIT_SINGLE_COMMITS: "false"
          USE_PR_AS_COMMIT: "false"
          FETCH_DEPTH: "10"

          # Required SSH/identity
          GERRIT_KNOWN_HOSTS: ${{ vars.GERRIT_KNOWN_HOSTS }}
          GERRIT_SSH_PRIVKEY_G2G: ${{ secrets.GERRIT_SSH_PRIVKEY_G2G }}
          GERRIT_SSH_USER_G2G: ${{ vars.GERRIT_SSH_USER_G2G }}
          GERRIT_SSH_USER_G2G_EMAIL: ${{ vars.GERRIT_SSH_USER_G2G_EMAIL }}

          # Optional overrides when .gitreview is missing or to force values
          GERRIT_SERVER: ${{ vars.GERRIT_SERVER }}
          GERRIT_SERVER_PORT: ${{ vars.GERRIT_SERVER_PORT }}
          GERRIT_PROJECT: ${{ vars.GERRIT_PROJECT }}

          # Optional Gerrit REST base path and credentials (if required)
          # e.g. '/r' for some deployments
          GERRIT_HTTP_BASE_PATH: ${{ vars.GERRIT_HTTP_BASE_PATH }}
          GERRIT_HTTP_USER: ${{ vars.GERRIT_HTTP_USER }}
          GERRIT_HTTP_PASSWORD: ${{ secrets.GERRIT_HTTP_PASSWORD }}

          ORGANIZATION: ${{ github.repository_owner }}
          REVIEWERS_EMAIL: ""
```

Notes:

- If both this step and the action define SSH configuration, the last
  configuration applied in the runner wins.
- For most users, you can rely on the action’s built-in SSH setup. Use this
  advanced configuration when you need custom SSH behavior or hosts.

## GitHub Enterprise support

- Direct-URL mode accepts enterprise GitHub hosts when explicitly enabled.
  Default: off (use github.com by default). Enable via the CLI flag
  --allow-ghe-urls or by setting ALLOW_GHE_URLS="true".
- In GitHub Actions, this action works with GitHub Enterprise when the
  workflow runs in that enterprise environment and provides a valid
  GITHUB_TOKEN. For direct-URL runs outside Actions, ensure ORGANIZATION
  and GITHUB_REPOSITORY reflect the target repository.

## Inputs

All inputs are strings, matching the composite action.

- SUBMIT_SINGLE_COMMITS
  - Submit one commit at a time to Gerrit. Default: "false".
- USE_PR_AS_COMMIT
  - Use PR title and body as the commit message. Default: "false".
- FETCH_DEPTH
  - Depth used when checking out the repository. Default: "10".
- GERRIT_KNOWN_HOSTS
  - SSH known hosts content for the Gerrit host. Required.
- GERRIT_SSH_PRIVKEY_G2G
  - SSH private key for Gerrit. Required.
- GERRIT_SSH_USER_G2G
  - Gerrit SSH username. Required.
- GERRIT_SSH_USER_G2G_EMAIL
  - Gerrit SSH user email (used for commit identity). Required.
- ORGANIZATION
  - Organization name, defaults to `github.repository_owner`.
- REVIEWERS_EMAIL
  - Comma separated reviewer emails. If empty, defaults to
    `GERRIT_SSH_USER_G2G_EMAIL`.
- ALLOW_GHE_URLS
  - Allow non-github.com GitHub Enterprise URLs in direct URL mode. Default: "false".
  - Set to "true" to allow non-github.com enterprise hosts.

Optional inputs when `.gitreview` is not present (parity with
the reusable workflow):

- GERRIT_SERVER
  - Gerrit host, e.g. `git.opendaylight.org`. Default: "".
- GERRIT_SERVER_PORT
  - Gerrit port, default "29418".
- GERRIT_PROJECT
  - Gerrit project name, e.g. `releng/builder`. Default: "".

## Outputs

- url
  - Gerrit change URL(s). Multi‑line when the action submits more than one change.
- change_number
  - Gerrit change number(s). Multi‑line when the action submits more than one change.

These outputs mirror the composite action. They are also exported into
the environment as:

- GERRIT_CHANGE_REQUEST_URL
- GERRIT_CHANGE_REQUEST_NUM

## Known Keys

The table below lists all the configuration directives supported by the tool,
along with the corresponding environment variable (also GitHub action input)
and the corresponding CLI flags.

<!-- markdownlint-disable MD013 -->

| Environment Variable / GitHub Input | Configuration Directive | CLI Flag | Description |
|-------------------------------------|-------------------------|----------|-------------|
| `SUBMIT_SINGLE_COMMITS` | `submit_single_commits` | `--submit-single-commits` | Submit one commit at a time to the Gerrit repository |
| `USE_PR_AS_COMMIT` | `use_pr_as_commit` | `--use-pr-as-commit` | Use PR title and body as the commit message |
| `FETCH_DEPTH` | `fetch_depth` | `--fetch-depth` | Fetch-depth for the clone (default: 10) |
| `GERRIT_KNOWN_HOSTS` | `gerrit_known_hosts` | `--gerrit-known-hosts` | Known hosts entries for Gerrit SSH |
| `GERRIT_SSH_PRIVKEY_G2G` | `gerrit_ssh_privkey_g2g` | `--gerrit-ssh-privkey-g2g` | SSH private key for Gerrit (string content) |
| `GERRIT_SSH_USER_G2G` | `gerrit_ssh_user_g2g` | `--gerrit-ssh-user-g2g` | Gerrit SSH user |
| `GERRIT_SSH_USER_G2G_EMAIL` | `gerrit_ssh_user_g2g_email` | `--gerrit-ssh-user-g2g-email` | Email address for the Gerrit SSH user |
| `ORGANIZATION` | `organization` | `--organization` | Organization (defaults to GITHUB_REPOSITORY_OWNER when unset) |
| `REVIEWERS_EMAIL` | `reviewers_email` | `--reviewers-email` | Comma-separated list of reviewer emails |
| `ALLOW_GHE_URLS` | `allow_ghe_urls` | `--allow-ghe-urls` | Allow non-github.com GitHub Enterprise URLs in direct URL mode |
| `PRESERVE_GITHUB_PRS` | `preserve_github_prs` | `--preserve-github-prs` | Do not close GitHub PRs after pushing to Gerrit |
| `DRY_RUN` | `dry_run` | `--dry-run` | Check settings and PR metadata; do not write to Gerrit |
| `GERRIT_SERVER` | `gerrit_server` | `--gerrit-server` | Gerrit server hostname (optional; .gitreview preferred) |
| `GERRIT_SERVER_PORT` | `gerrit_server_port` | `--gerrit-server-port` | Gerrit SSH port (default: 29418) |
| `GERRIT_PROJECT` | `gerrit_project` | `--gerrit-project` | Gerrit project (optional; .gitreview preferred) |
| `ISSUE_ID` | `issue_id` | `--issue-id` | Issue ID to include in commit message (e.g., Issue-ID: ABC-123) |
| `ALLOW_DUPLICATES` | `allow_duplicates` | `--allow-duplicates` | Allow submitting duplicate changes without error |
| `G2G_VERBOSE` | `g2g_verbose` | `--verbose` / `-v` | Enable verbose debug logging |
| `G2G_SKIP_GERRIT_COMMENTS` | `g2g_skip_gerrit_comments` | N/A | Skip adding back-reference comments to Gerrit changes |
| `GITHUB_TOKEN` | `github_token` | N/A | GitHub API token for accessing repository and PR data |
| `PR_NUMBER` | `pr_number` | N/A | Pull request number (set automatically in CI) |
| `SYNC_ALL_OPEN_PRS` | `sync_all_open_prs` | N/A | Process all open pull requests (internal use) |
| `GERRIT_HTTP_BASE_PATH` | `gerrit_http_base_path` | N/A | HTTP base path for Gerrit API (e.g., "/r") |
| `GERRIT_HTTP_USER` | `gerrit_http_user` | N/A | Gerrit HTTP username for REST API authentication |
| `GERRIT_HTTP_PASSWORD` | `gerrit_http_password` | N/A | Gerrit HTTP password/token for REST API authentication |

<!-- markdownlint-enable MD013 -->

### Configuration Precedence

The tool follows this precedence order for configuration values:

1. **CLI flags** (highest priority)
2. **Environment variables**
3. **Configuration file values**
4. **Tool defaults** (lowest priority)

### Configuration File Format

Configuration files use INI format with organization-specific sections:

```ini
[default]
GERRIT_SERVER = "gerrit.example.org"
PRESERVE_GITHUB_PRS = "true"

[onap]
ISSUE_ID = "CIMAN-33"
REVIEWERS_EMAIL = "user@example.org"

[opendaylight]
GERRIT_HTTP_USER = "bot-user"
GERRIT_HTTP_PASSWORD = "${ENV:ODL_GERRIT_TOKEN}"
```

The tool loads configuration from `~/.config/github2gerrit/configuration.txt`
by default, or from the path specified in the `G2G_CONFIG_PATH` environment
variable.

**Note**: Unknown configuration keys will generate warnings to help catch typos
and missing functionality.

## Behavior details

- Branch resolution
  - Uses `GITHUB_BASE_REF` as the target branch for Gerrit, or defaults
    to `master` when unset, matching the existing workflow.
- Topic naming
  - Uses `GH-<repo>-<pr-number>` where `<repo>` replaces slashes with
    hyphens.
- GitHub Enterprise support
  - Direct URL mode accepts enterprise GitHub hosts when explicitly enabled
    (default: off; use github.com by default). Enable via --allow-ghe-urls or
    ALLOW_GHE_URLS="true". The tool determines the GitHub API base URL from
    GITHUB_API_URL or GITHUB_SERVER_URL/api/v3.
- Change‑Id handling
  - Single commits: the process amends each cherry‑picked commit to include a
    `Change-Id`. The tool collects these values for querying.
  - Squashed: collects trailers from original commits, preserves
    `Signed-off-by`, and reuses the `Change-Id` when PRs reopen or synchronize.
- Reviewers
  - If empty, defaults to the Gerrit SSH user email.
- Comments
  - Adds a back‑reference comment in Gerrit with the GitHub PR and run
    URL. Adds a comment on the GitHub PR with the Gerrit change URL(s).
- Closing PRs
  - On `pull_request_target`, the workflow may close the PR after submission to
    match the shell action’s behavior.

## Security notes

- Do not hardcode secrets or keys. Provide the private key via the
  workflow secrets and known hosts via repository or org variables.
- SSH handling is non-invasive: the tool creates temporary SSH files in
  the workspace without modifying user SSH configuration or keys.
- SSH agent scanning prevention uses `IdentitiesOnly=yes` to avoid
  unintended key usage (e.g., signing keys requiring biometric auth).
- Temporary SSH files are automatically cleaned up after execution.
- All external calls should use retries and clear error reporting.

## Development

This repository follows the guidelines in `CLAUDE.md`.

- Language and CLI
  - Python 3.11. The CLI uses Typer.
- Packaging
  - `pyproject.toml` with setuptools backend. Use `uv` to install and run.
- Structure
  - `src/github2gerrit/cli.py` (CLI entrypoint)
  - `src/github2gerrit/core.py` (orchestration)
  - `src/github2gerrit/gitutils.py` (subprocess and git helpers)
- Linting and type checking
  - Ruff and MyPy use settings in `pyproject.toml`.
  - Run from pre‑commit hooks and CI.
- Tests
  - Pytest with coverage targets around 80%.
  - Add unit and integration tests for each feature.

### Local setup

- Install `uv` and run:
  - `uv pip install --system .`
  - `uv run github2gerrit --help`
- Run tests:
  - `uv run pytest -q`
- Lint and type check:
  - `uv run ruff check .`
  - `uv run black --check .`
  - `uv run mypy src`

### Notes on parity

- Inputs, outputs, and environment usage match the shell action.
- The action assumes the same GitHub variables and secrets are present.
- Where the shell action uses tools such as `jq` and `gh`, the Python
  version uses library calls and subprocess as appropriate, with retries
  and clear logging.

## License

Apache License 2.0. See `LICENSE` for details.
