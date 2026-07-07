<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2026 The Linux Foundation
-->

# Feature Reference

This document describes the features of the github2gerrit tool and composite
action in detail. For a quick start and the full inputs table, see the
[README](../README.md).

## Contents

- [PR Update Handling](#pr-update-handling)
- [PR Comment Commands](#pr-comment-commands)
- [Closing Merged PRs](#closing-merged-prs)
- [Automatic Cleanup](#automatic-cleanup)
- [Restricting PRs to Automation Tools](#restricting-prs-to-automation-tools)
- [Duplicate Detection](#duplicate-detection)
- [Commit Message Normalization](#commit-message-normalization)
- [Change-ID Reconciliation](#change-id-reconciliation)
- [Configuration](#configuration)
- [Behavior Details](#behavior-details)

## PR Update Handling

The tool handles PR updates from automation tools such as Dependabot by
mapping GitHub pull request events to distinct operation modes.

### PR Event Types

| Event         | Operation | Behavior                                  |
| ------------- | --------- | ----------------------------------------- |
| `opened`      | CREATE    | Creates new Gerrit change(s)              |
| `synchronize` | UPDATE    | Updates existing change with new patchset |
| `edited`      | EDIT      | Syncs metadata changes to Gerrit          |
| `reopened`    | REOPEN    | Treats as CREATE if no existing change    |
| `closed`      | CLOSE     | Handles PR closure                        |

### How Updates Work

When a PR updates (for example, Dependabot rebases or updates dependencies):

1. The `synchronize` event triggers UPDATE mode.
2. The tool recovers the existing Gerrit change using five strategies,
   in order:
   - Topic-based query (`GH-<owner>-<repo>-<pr-number>`, where `<repo>` is
     the GitHub repository name without the owner prefix)
   - `GitHub-Hash` trailer matching
   - `GitHub-PR` trailer URL matching
   - Mapping comment parsing from PR comments
   - Dependency package match (reuses an open change that bumps the same
     dependency, for Dependabot/Renovate supersession)
3. The tool reuses the existing Change-ID(s), so the push creates a new
   patchset rather than a new change.
4. PR title/description edits sync to the Gerrit change metadata.
5. The tool verifies that the push created a new patchset.

### Workflow Trigger Example

```yaml
on:
  pull_request_target:
    types: [opened, reopened, edited, synchronize, closed]
```

Typical Dependabot lifecycle:

1. Dependabot opens PR #29 → the tool creates Gerrit change 73940.
2. Dependabot rebases PR #29 → change 73940 gets patchset 2.
3. Dependabot updates dependencies in PR #29 → change 73940 gets patchset 3.
4. Someone edits the PR title → metadata syncs to change 73940.
5. Change 73940 merges in Gerrit → PR #29 closes in GitHub.

### Error Handling

UPDATE mode requires an existing Gerrit change. If none exists, the run fails
with a message explaining how to recover:

```text
UPDATE FAILED: Cannot update non-existent Gerrit change
To create a new change, set CREATE_MISSING=true or add a
'@github2gerrit create missing change' comment on the PR.
```

See [PR Comment Commands](#pr-comment-commands) for the recovery mechanism.

## PR Comment Commands

The tool supports directives issued through pull request comments. Add a
comment containing `@github2gerrit` followed by a command phrase and the tool
acts on it during the next workflow run.

### Command Format

```text
@github2gerrit <command>
```

- Commands are case-insensitive.
- When the same command appears in more than one comment, only the latest
  occurrence takes effect.
- The tool logs unrecognized directives at debug level and ignores them.

### Available Commands

<!-- markdownlint-disable MD013 -->

| Command                 | Aliases                            | Description                                                                 |
| ----------------------- | ---------------------------------- | --------------------------------------------------------------------------- |
| `create missing change` | `create-missing`, `create missing` | Create a Gerrit change when an UPDATE operation cannot find an existing one |

<!-- markdownlint-enable MD013 -->

### Create Missing Change

When a `synchronize` event fires, the tool runs an UPDATE operation and
expects a Gerrit change to exist. If the original `opened` event failed, no
change exists and every following update fails. The create-missing fallback
resolves this without manual intervention in Gerrit. Three mechanisms
trigger it:

1. PR comment directive — add a comment on the stuck PR, then re-trigger the
   workflow (push a trivial change or re-run manually):

   ```text
   @github2gerrit create missing change
   ```

2. CLI flag or environment variable:

   ```shell
   github2gerrit --create-missing https://github.com/MyOrg/my-repo/pull/42
   # or
   export CREATE_MISSING=true
   github2gerrit https://github.com/MyOrg/my-repo/pull/42
   ```

3. Action input (default `"false"`):

   ```yaml
   - name: Submit PR to Gerrit
     uses: lfreleng-actions/github2gerrit-action@main
     with:
       GERRIT_SSH_PRIVKEY_G2G: ${{ secrets.GERRIT_SSH_PRIVKEY_G2G }}
       CREATE_MISSING: "true"
   ```

   Setting `CREATE_MISSING: "true"` in the workflow means stuck PRs self-heal
   on the next `synchronize` event without requiring a comment directive.

Fallback sequence:

1. The tool attempts the normal UPDATE flow and finds no existing change.
2. It checks for `--create-missing`/`CREATE_MISSING` or scans PR comments for
   the directive.
3. If authorized, the operation switches from UPDATE to CREATE.
4. The tool posts a notice on the PR explaining the fallback.
5. The pipeline continues as a normal CREATE: preparing commits, pushing to
   Gerrit, and posting the change URL back on the PR.

## Closing Merged PRs

The `CLOSE_MERGED_PRS` setting (default `"true"`) closes GitHub PRs when
their corresponding Gerrit changes merge and sync back to GitHub. This
completes the lifecycle for automation PRs such as Dependabot.

How it works:

1. A bot creates a GitHub PR.
2. The tool converts it to a Gerrit change with tracking trailers.
3. When the Gerrit change merges and syncs to GitHub (a `push` event on the
   mirror), the tool closes the original PR with a comment.
4. When a change ends up abandoned in Gerrit, handling depends on the setting:
   - `CLOSE_MERGED_PRS=true`: closes the PR with an abandoned comment.
   - `CLOSE_MERGED_PRS=false`: the PR remains open but receives an abandoned
     notification comment.

Behavior by Gerrit change status:

| Scenario                    | `CLOSE_MERGED_PRS=true` (default) | `CLOSE_MERGED_PRS=false`                  |
| --------------------------- | --------------------------------- | ----------------------------------------- |
| Change has MERGED status    | Closes PR with merged comment     | No action                                 |
| Change has ABANDONED status | Closes PR with abandoned comment  | Adds notification comment (PR stays open) |
| Change is NEW/OPEN          | Closes PR with a warning          | No action                                 |
| Status UNKNOWN              | Closes PR with a warning          | No action                                 |

Notes:

- The operation is non-fatal: the tool logs missing or already-closed PRs as
  informational messages, not errors.
- The tool skips commits with no `GitHub-PR` URL trailer.

## Automatic Cleanup

Two cleanup operations run automatically after successful PR processing, on
push events, and on PR close events. They keep GitHub and Gerrit in sync by
removing orphaned or stale changes.

### CLEANUP_ABANDONED

Closes GitHub PRs for abandoned Gerrit changes. Default: `"true"`.

- Scans open GitHub PRs in the repository.
- Finds the associated Gerrit change via the `GitHub-PR` trailer.
- If the Gerrit change has `ABANDONED` status, closes the GitHub PR with a
  comment explaining the abandonment.
- Respects the `CLOSE_MERGED_PRS` setting for whether to close or comment.

### CLEANUP_GERRIT

Abandons Gerrit changes for closed GitHub PRs. Default: `"true"`.

- Queries open Gerrit changes in the project.
- Extracts the `GitHub-PR` trailer from each change.
- For each closed GitHub PR, abandons the Gerrit change with a message
  including the PR number and URL, any comments made when closing the PR,
  and attribution to github2gerrit.
- Detects Dependabot supersession ("Superseded by #X" comments) and includes
  that context in the abandon message.

### PR Close Event Handling

When a GitHub PR closes, the tool performs these actions in order:

1. Abandons the specific Gerrit change for the closed PR, capturing the last
   3 PR comments to preserve closure context.
2. Runs `CLEANUP_ABANDONED` to close any other GitHub PRs with abandoned
   Gerrit changes.
3. Runs `CLEANUP_GERRIT` to abandon any other Gerrit changes with closed
   GitHub PRs.

### Configuration Example

```yaml
- uses: lfreleng-actions/github2gerrit-action@main
  with:
    GERRIT_SSH_PRIVKEY_G2G: ${{ secrets.GERRIT_SSH_PRIVKEY_G2G }}
    CLEANUP_ABANDONED: "false"  # Don't close GitHub PRs for abandoned changes
    CLEANUP_GERRIT: "false"     # Don't abandon Gerrit changes for closed PRs
```

### Cleanup Notes

- Cleanup operations are non-fatal: failures log warnings without failing
  the workflow.
- Operations are idempotent and parallel-safe; the tool skips PRs already
  closed and changes already abandoned.
- Both operations respect the `DRY_RUN` setting for testing.

## Restricting PRs to Automation Tools

The `AUTOMATION_ONLY` setting (default `"true"`) restricts pull request
processing to known automation tools. Use this for GitHub mirrors where
contributors should submit changes via Gerrit, while still accepting
automated dependency updates.

Recognized automation tool usernames:

| Tool           | GitHub Username(s)                    |
| -------------- | ------------------------------------- |
| Dependabot     | `dependabot[bot]`, `dependabot`       |
| Pre-commit.ci  | `pre-commit-ci[bot]`, `pre-commit-ci` |
| GitHub Copilot | `Copilot`                             |

When enabled, the tool rejects PRs from other users by:

1. Logging a warning message.
2. Closing the PR with this comment:

   ```text
   This GitHub mirror does not accept pull requests.
   Please submit changes to the project's Gerrit server.
   ```

3. Exiting with code 1.

Set `AUTOMATION_ONLY: "false"` to accept PRs from all users:

```yaml
- uses: lfreleng-actions/github2gerrit-action@main
  with:
    AUTOMATION_ONLY: "false"
    GERRIT_SSH_PRIVKEY_G2G: ${{ secrets.GERRIT_SSH_PRIVKEY_G2G }}
```

## Duplicate Detection

Duplicate detection uses a scoring-based approach. The detector compares the
first line of the commit message (subject/PR title), analyzes the body text
and the set of files changed, and computes a similarity score. When the score
meets or exceeds a threshold (default 0.8), the tool treats the change as a
duplicate. This remains robust even when similar changes appeared outside
this pipeline.

Examples of detected duplicates:

- Dependency bumps for the same package across close versions (for example,
  "Bump foo from 1.0 to 1.1" vs "Bump foo from 1.1 to 1.2") with overlapping
  files
- Pre-commit autoupdates that change `.pre-commit-config.yaml` and hook
  versions
- GitHub Actions version bumps that update `.github/workflows/*` uses lines
- Similar bug fixes with the same subject and significant file overlap

### Allowing Duplicates

The composite action defaults to `ALLOW_DUPLICATES: "true"`, and the CLI
default is also to allow duplicates. When allowed, duplicates generate
warnings but processing continues. When not allowed, the tool exits with
code 3 on detection.

```bash
# CLI usage
github2gerrit --no-allow-duplicates https://github.com/org/repo
```

```yaml
# GitHub Actions
- uses: lfreleng-actions/github2gerrit-action@main
  with:
    ALLOW_DUPLICATES: "false"  # fail on duplicate detection
```

### Duplicate Detection Scope

By default, the detector checks changes with status `open` when searching
for potential duplicates (`DUPLICATE_TYPES` default `"open"`). Customize the
Gerrit change states with `--duplicate-types` or `DUPLICATE_TYPES`:

```bash
# CLI usage - check against open and merged changes
github2gerrit --duplicate-types=open,merged https://github.com/org/repo

# Environment variable
DUPLICATE_TYPES=open,merged,abandoned github2gerrit https://github.com/org/repo
```

```yaml
# GitHub Actions
- uses: lfreleng-actions/github2gerrit-action@main
  with:
    DUPLICATE_TYPES: "open,merged"
```

Valid change states are `open`, `merged`, and `abandoned`.

## Commit Message Normalization

The `NORMALISE_COMMIT` setting converts automated PR titles (from tools like
Dependabot and pre-commit.ci) to conventional commit format. The composite
action defaults this to `"false"`; the CLI defaults to enabled
(`--normalise-commit`).

### How It Works

1. Repository analysis — the tool determines preferred conventional commit
   patterns by examining:
   - `.pre-commit-config.yaml` for commit message formats
   - `.github/release-drafter.yml` for commit type patterns
   - Recent git history for existing conventional commit usage
2. Detection — normalization applies to automated PRs from known bots
   (`dependabot[bot]`, `pre-commit-ci[bot]`, `renovate[bot]`, and others) or
   PRs with automation patterns in the title.
3. Adaptive formatting — respects the repository's existing conventions:
   - Capitalization: detects whether the repository uses `feat:` or `Feat:`
   - Commit types: uses appropriate types (`chore`, `build`, `ci`, etc.)
   - Dependency updates: converts "Bump package from X to Y" to
     "chore: bump package from X to Y"

### Normalization Examples

Before:

```text
Bump net.logstash.logback:logstash-logback-encoder from 7.4 to 8.1
pre-commit autoupdate
Update GitHub Action dependencies
```

After:

```text
chore: bump net.logstash.logback:logstash-logback-encoder from 7.4 to 8.1
chore: pre-commit autoupdate
build: update GitHub Action dependencies
```

### Enabling or Disabling

```bash
# CLI usage (enabled by default)
github2gerrit --normalise-commit https://github.com/org/repo
github2gerrit --no-normalise-commit https://github.com/org/repo

# Environment variable
NORMALISE_COMMIT=true github2gerrit https://github.com/org/repo
```

```yaml
# GitHub Actions (action default is "false")
- uses: lfreleng-actions/github2gerrit-action@main
  with:
    NORMALISE_COMMIT: "true"
```

### Influencing Normalization

To steer capitalization and commit types, configure the repository's
`.pre-commit-config.yaml` (`ci.autofix_commit_msg`,
`ci.autoupdate_commit_msg`) or `.github/release-drafter.yml`
(`autolabeler` title patterns such as `/chore:/i`). The tool detects the
capitalization style from these files and applies it to normalized commit
messages.

## Change-ID Reconciliation

The reconciliation system reuses existing Gerrit Change-IDs when updating
pull requests, preventing duplicate changes when developers rebase, add
commits, or amend a PR.

### Reconciliation Process

When a PR updates (for example, via a `synchronize` event):

1. The tool queries existing Gerrit changes using the PR's topic (or falls
   back to GitHub comments).
2. It matches local commits to existing changes using these strategies:
   - Trailer matching: reuses Change-IDs already present in commit messages
   - Exact subject matching: matches commits with identical subjects
   - File signature matching: matches commits with identical file changes
     (optional, see `SIMILARITY_FILES`)
   - Subject similarity matching: uses Jaccard similarity on commit subjects
3. It generates new Change-IDs for commits that don't match any existing
   change.

### Reconciliation Settings

These settings are environment variables and CLI flags. They are not
composite action inputs; in workflows, set them via `env:` on the step.

`REUSE_STRATEGY` (default: `topic+comment`)

- `topic`: query Gerrit changes by topic
- `comment`: search GitHub PR comments for Change-IDs
- `topic+comment`: try topic first, fall back to comments
- `none`: disable reconciliation (always generate new Change-IDs)

`SIMILARITY_SUBJECT` (CLI: `--similarity-subject`, default: `0.7`)

- Jaccard similarity threshold (0.0-1.0) for subject matching.
- Higher values require more similarity between commit subjects.

`SIMILARITY_UPDATE_FACTOR` (CLI: `--similarity-update-factor`, default: `0.75`)

- Multiplier applied to the similarity threshold for UPDATE operations,
  allowing more lenient matching for rebased or amended commits.
- Applied as `update_threshold = max(0.5, base_threshold × factor)`.
- Example: with base `0.7` and factor `0.75`, the UPDATE threshold becomes
  `0.525`. The floor of `0.5` prevents too-loose matching.

`SIMILARITY_FILES` (CLI: `--similarity-files/--no-similarity-files`, default: `false`)

- Whether to require exact file signature matches during reconciliation.
- When `true`, commits must touch the exact same set of files to match.
- The default is `false` because file signature matching is too strict for
  common workflows: developers add/remove files during PR updates, rebasing
  shifts file changes between commits, and conflict resolution changes which
  files a commit touches.
- Enable it for controlled workflows where file sets never change.

`ALLOW_ORPHAN_CHANGES` (default: `false`)

- When enabled, unmatched Gerrit changes don't generate warnings. Useful
  when you expect to remove changes from the topic.

### Why an Adjustable Update Factor?

PR updates often involve rebasing, which can change commit messages slightly
(updated references, fixed typos, resolved conflicts). The update factor lets
the system recognize these as the same logical change despite minor message
differences: the base threshold applies to initial PR creation, and the
reduced threshold applies to `synchronize` events.

### Reconciliation Examples

```bash
# Strict matching - require 90% similarity, minor relaxation on updates
SIMILARITY_SUBJECT=0.9
SIMILARITY_UPDATE_FACTOR=0.85

# Disable reconciliation (always create new Change-IDs)
REUSE_STRATEGY=none
```

GitHub Actions — set these via `env:` on the step (they are environment
variable settings, not action inputs):

```yaml
- uses: lfreleng-actions/github2gerrit-action@main
  with:
    GERRIT_KNOWN_HOSTS: ${{ secrets.GERRIT_KNOWN_HOSTS }}
    GERRIT_SSH_PRIVKEY_G2G: ${{ secrets.GERRIT_SSH_PRIVKEY_G2G }}
  env:
    SIMILARITY_SUBJECT: "0.75"
    SIMILARITY_UPDATE_FACTOR: "0.8"
    # SIMILARITY_FILES defaults to 'false' - set 'true' for strict mode
```

CLI:

```bash
github2gerrit \
  --similarity-subject 0.75 \
  --similarity-update-factor 0.8 \
  https://github.com/owner/repo/pull/123
```

## Configuration

### Configuration Precedence

The tool resolves configuration values in this order:

1. CLI flags (highest priority)
2. Environment variables
3. Configuration file values
4. Derived values (see [Credential Derivation](#credential-derivation))
5. Tool defaults (lowest priority)

### Configuration File

The tool loads configuration from `~/.config/github2gerrit/configuration.txt`
by default, or from the path in the `G2G_CONFIG_PATH` environment variable.
The file uses INI format with a `[default]` section and per-organization
sections:

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

Notes:

- The tool skips the configuration file entirely when running in GitHub
  Actions (`GITHUB_ACTIONS=true`); configure the action via inputs and
  environment variables instead.
- `${ENV:VAR}` references expand from the environment.
- Unknown configuration keys generate warnings to help catch typos.

### Using .netrc Files

The tool loads Gerrit HTTP credentials from `.netrc` files, following the
standard format used by curl and other tools.

Search order:

1. `.netrc` in the current directory
2. `~/.netrc` in the home directory
3. `~/_netrc` (Windows fallback)

Example:

```text
machine gerrit.onap.org login myuser password mytoken
machine gerrit.opendaylight.org login myuser password anothertoken
```

CLI options:

| Option              | Description                                     |
| ------------------- | ----------------------------------------------- |
| `--no-netrc`        | Disable .netrc file lookup                      |
| `--netrc-file PATH` | Use a specific .netrc file                      |
| `--netrc-optional`  | Do not fail if .netrc file is missing (default) |
| `--netrc-required`  | Require a .netrc file and fail if missing       |

Lookup is optional by default: when no `.netrc` file exists, the tool falls
back to environment variables. Credential priority order:

1. CLI arguments (highest priority)
2. `.netrc` file (unless disabled with `--no-netrc`)
3. Environment variables (`GERRIT_HTTP_USER`, `GERRIT_HTTP_PASSWORD`)

### Supersession Sweep Without REST Credentials

The dependency supersession sweep (which reuses or abandons older open Gerrit
changes when the tool pushes a newer update for the same dependency) needs to
list the tool's own open changes. With Gerrit REST credentials it does this
via the `owner:self` query operator.

`owner:self` requires an authenticated session. Without REST credentials the
tool falls back to an anonymous query: it lists the project/branch's open
github2gerrit changes (narrowed server-side via the `GitHub-PR` trailer, a
public read-only operation) and keeps only those whose trailer points at the
current repository.

- `G2G_ANON_SUPERSEDE_FALLBACK` (default `true`) controls the fallback. Set
  it to `false` to disable; without credentials the tool then skips the
  sweep and logs a warning.
- The fallback depends on the Gerrit server allowing anonymous change
  queries. If the server rejects the query, the tool skips the sweep
  gracefully.
- On large projects the query has a result cap; if it reaches that cap the
  tool warns that the sweep may be incomplete.

### Credential Derivation

When `GERRIT_SSH_USER_G2G` and `GERRIT_SSH_USER_G2G_EMAIL` are not explicitly
provided, the tool derives them from these sources, in priority order:

1. SSH config user (if `G2G_RESPECT_USER_SSH=true` in local mode) — reads
   `~/.ssh/config` for the Gerrit host, matching host patterns including
   wildcards, and extracts the `User` directive.
2. Git user email (if `G2G_RESPECT_USER_SSH=true` in local mode) — reads
   `git config user.email` for the commit email address.
3. Organization-based fallback (the default in GitHub Actions) — derives
   standardized values from the GitHub organization name.

Organization-based pattern, from the `ORGANIZATION` value:

- Gerrit server: `gerrit.{organization}.org` (or from config file)
- SSH username: `{organization}.gh2gerrit`
- Email: `releng+{organization}-gh2gerrit@linuxfoundation.org`

For example, for organization `onap`: server `gerrit.onap.org`, username
`onap.gh2gerrit`, email `releng+onap-gh2gerrit@linuxfoundation.org`.

The tool determines the organization name from, in order:

1. The explicit `ORGANIZATION` parameter (action input or environment
   variable)
2. `GITHUB_REPOSITORY_OWNER` (set automatically by GitHub Actions)

The tool normalizes the organization name to lowercase before constructing
the Gerrit server hostname and credentials.

Local development mode — set `G2G_RESPECT_USER_SSH=true` to use personal SSH
and git configuration instead of organization-based defaults:

```bash
export G2G_RESPECT_USER_SSH=true
github2gerrit https://github.com/org/repo/pull/123
```

GitHub Actions mode — credentials use the organization-based fallback unless
explicitly provided:

```yaml
- uses: lfreleng-actions/github2gerrit-action@main
  with:
    GERRIT_SSH_USER_G2G: ${{ vars.GERRIT_SSH_USER_G2G }}
    GERRIT_SSH_USER_G2G_EMAIL: ${{ vars.GERRIT_SSH_USER_G2G_EMAIL }}
    ORGANIZATION: ${{ github.repository_owner }}
```

To disable derivation entirely, set `G2G_ENABLE_DERIVATION=false`. All Gerrit
parameters must then be explicitly provided.

### Issue ID Lookup

The action resolves Issue IDs via JSON lookup when you omit `ISSUE_ID`.
Set the `ISSUE_ID_LOOKUP_JSON` input to a JSON array mapping GitHub usernames
to Issue IDs, and the action looks up the Issue ID based on the GitHub actor
who created the pull request.

```yaml
- uses: lfreleng-actions/github2gerrit-action@main
  with:
    GERRIT_SSH_PRIVKEY_G2G: ${{ secrets.GERRIT_SSH_PRIVKEY_G2G }}
    ISSUE_ID_LOOKUP_JSON: ${{ vars.ISSUE_ID_LOOKUP_JSON }}
```

Example JSON format (store as a repository or organization variable):

```json
[
  { "key": "dependabot[bot]", "value": "AUTO-123" },
  { "key": "renovate[bot]", "value": "AUTO-456" },
  { "key": "alice", "value": "PROJ-789" }
]
```

Lookup logic:

1. When you supply `ISSUE_ID`, the action uses it directly.
2. If `ISSUE_ID` is empty and `ISSUE_ID_LOOKUP_JSON` is valid JSON, the
   action looks up the Issue ID using `github.actor`.
3. If the lookup fails or the JSON is invalid, the action logs a warning and
   skips the Issue ID. Invalid JSON does not fail the workflow.

## Behavior Details

- Branch resolution: uses `GITHUB_BASE_REF` as the target branch for Gerrit,
  defaulting to `master` when unset.
- Topic naming: pushes use `<prefix>-<project>-<pr-number>`, where
  `<prefix>` comes from `G2G_TOPIC_PREFIX` (default `GH`) and `<project>`
  is the Gerrit project path with `/` replaced by `-` (or the GitHub
  repository name when `.gitreview` is unavailable). Change recovery
  queries use `GH-<owner>-<repo>-<pr-number>` with the GitHub owner and
  repository name.
- GitHub Enterprise support: direct URL mode accepts enterprise GitHub hosts
  when explicitly enabled via `--allow-ghe-urls` or `ALLOW_GHE_URLS="true"`
  (default: off). The tool determines the GitHub API base URL from
  `GITHUB_API_URL` or `GITHUB_SERVER_URL/api/v3`.
- Change-Id handling:
  - Single commits: the tool amends each cherry-picked commit with a
    `Change-Id`, and the tool collects these values for querying.
  - Squashed: collects trailers from original commits, preserves
    `Signed-off-by`, and reuses the `Change-Id` when PRs reopen or
    synchronize.
- Reviewers: if `REVIEWERS_EMAIL` is empty, defaults to the Gerrit SSH user
  email.
- Comments: adds a back-reference comment in Gerrit with the GitHub PR and
  run URL, and a comment on the GitHub PR with the Gerrit change URL(s).
- Closing PRs: by default, the tool preserves PRs after submission
  (`PRESERVE_GITHUB_PRS` default `"true"`). Set it to `"false"` to close PRs
  after submission on `pull_request_target` events.
- Exit codes: `0` success, `1` general error, `2` configuration error,
  `3` duplicate detected (when duplicates not allowed). Other codes
  cover GitHub API, Gerrit connection, network, repository, PR state, and
  validation failures.
