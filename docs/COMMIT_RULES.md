<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- SPDX-FileCopyrightText: 2025 The Linux Foundation -->

# Commit Rules (`COMMIT_RULES_JSON`)

The **commit rules** feature provides a flexible, JSON-driven mechanism for
injecting arbitrary lines into commit messages submitted to Gerrit. It
generalises the existing `ISSUE_ID` / `ISSUE_ID_LOOKUP_JSON` support to handle
per-project requirements such as FD.io VPP's mandatory `Type:` field.

## Quick Start

Set the `COMMIT_RULES_JSON` GitHub Actions variable (organisation or
repository level) to a JSON object describing the rules:

```yaml
# .github/workflows/g2g.yml
jobs:
  submit:
    steps:
      - uses: lfreleng-actions/github2gerrit-action@main
        with:
          COMMIT_RULES_JSON: ${{ vars.COMMIT_RULES_JSON }}
          # ... other inputs ...
```

## JSON Schema

The top-level object has three optional sections:

| Section    | Type                          | Description                                    |
|------------|-------------------------------|------------------------------------------------|
| `defaults` | `array` of rule objects       | Baseline rules applied to every commit.        |
| `projects` | `object` (project → rules[])  | Per-Gerrit-project overrides.                  |
| `actors`   | `object` (actor → rules[])    | Per-GitHub-actor overrides (e.g. bots).        |

### Rule Object

Each rule object describes a single line to insert into the commit message:

| Field       | Type     | Required | Default        | Description                                                                |
|-------------|----------|----------|----------------|----------------------------------------------------------------------------|
| `key`       | `string` | Yes      | —              | The label name (e.g. `Type`, `Issue-ID`, `Ticket`).                        |
| `value`     | `string` | Yes      | —              | The value to insert.                                                       |
| `location`  | `string` | No       | `"trailer"`    | Where to place the line — `"trailer"` or `"body"`.                         |
| `separator` | `string` | No       | `"blank_line"` | Separation style when `location` is `"body"` — `"blank_line"` or `"none"`. |

### Locations

- **`trailer`** — places the line in the Git trailer block at the end of
  the commit message, alongside `Change-Id`, `Signed-off-by`, etc.
- **`body`** — places the line in the commit body, before the trailer
  block. Fields like VPP's `Type:` need this location because Gerrit
  server-side hooks expect them in the body rather than the trailer section.

### Separators (body location only)

- **`blank_line`** (default) — inserts a blank line before the new
  content, matching the conventional VPP commit style.
- **`none`** — appends the line directly after the existing body text
  without extra blank lines.

## Resolution Precedence

The engine resolves rules in this order when building the commit message
(last writer wins for a given `key`):

1. **`defaults`** — baseline rules for all projects and actors.
2. **`projects[<gerrit_project>]`** — overrides defaults for the matching
   Gerrit project (from `.gitreview` or `GERRIT_PROJECT`).
3. **`actors[<github_actor>]`** — overrides everything for the matching
   GitHub actor (from `GITHUB_ACTOR`).

The existing `ISSUE_ID` input always takes priority over any `Issue-ID`
rule from commit rules. Both mechanisms can coexist safely.

## Examples

### FD.io (VPP + CSIT on the same Gerrit server)

VPP requires a `Type:` field in the commit body; CSIT does not.
Both projects need `Issue-ID` in the trailer block.

```json
{
  "defaults": [
    {
      "key": "Issue-ID",
      "value": "CIMAN-33",
      "location": "trailer"
    }
  ],
  "projects": {
    "vpp": [
      {
        "key": "Type",
        "value": "ci",
        "location": "body",
        "separator": "blank_line"
      },
      {
        "key": "Issue-ID",
        "value": "CIMAN-33",
        "location": "trailer"
      }
    ],
    "hicn": [
      {
        "key": "Type",
        "value": "ci",
        "location": "body",
        "separator": "blank_line"
      }
    ]
  },
  "actors": {
    "dependabot[bot]": [
      {
        "key": "Type",
        "value": "ci",
        "location": "body"
      },
      {
        "key": "Issue-ID",
        "value": "CIMAN-33",
        "location": "trailer"
      }
    ],
    "renovate[bot]": [
      {
        "key": "Issue-ID",
        "value": "CIMAN-44",
        "location": "trailer"
      }
    ]
  }
}
```

**Result for VPP + dependabot:**

```text
gha: update actions/checkout from v3 to v4

Type: ci

Issue-ID: CIMAN-33
Change-Id: I1234567890abcdef...
Signed-off-by: dependabot[bot] <support@github.com>
```

**Result for CSIT + human user:**

```text
fix: correct test assertion

Issue-ID: CIMAN-33
Change-Id: I1234567890abcdef...
Signed-off-by: Jane Doe <jane@example.com>
```

### ONAP (Issue-ID only)

ONAP projects only need `Issue-ID` in the trailer:

```json
{
  "actors": {
    "dependabot[bot]": [
      {
        "key": "Issue-ID",
        "value": "CIMAN-33"
      }
    ]
  }
}
```

### Extra body fields

Some projects need more than one body field:

```json
{
  "projects": {
    "vpp": [
      {
        "key": "Type",
        "value": "ci",
        "location": "body",
        "separator": "blank_line"
      },
      {
        "key": "Ticket",
        "value": "VPP-2088",
        "location": "body",
        "separator": "none"
      }
    ]
  }
}
```

**Result:**

```text
gha: update dependency versions

Type: ci
Ticket: VPP-2088

Change-Id: I1234567890abcdef...
Signed-off-by: bot <bot@example.com>
```

## CLI Usage

You can also pass the commit rules JSON via the command line:

```bash
github2gerrit --commit-rules '{"defaults": [...]}' \
  https://github.com/org/repo/pull/123
```

Or via the environment variable:

```bash
export COMMIT_RULES_JSON='{"defaults": [...]}'
github2gerrit https://github.com/org/repo/pull/123
```

## Interaction with ISSUE_ID / ISSUE_ID_LOOKUP_JSON

The existing `ISSUE_ID` and `ISSUE_ID_LOOKUP_JSON` inputs continue to
work unchanged. When both mechanisms specify an `Issue-ID`:

1. An explicit `ISSUE_ID` input (or a value resolved from
   `ISSUE_ID_LOOKUP_JSON`) **always wins**.
2. If `ISSUE_ID` is empty, the engine applies the `Issue-ID` rule from
   `COMMIT_RULES_JSON` instead.

This means you can safely enable `COMMIT_RULES_JSON` for an organisation
without breaking workflows that already set `ISSUE_ID` directly.

## Validation and Error Handling

- Invalid JSON produces a warning but does **not** fail the workflow
  (matching the existing `ISSUE_ID_LOOKUP_JSON` convention).
- Individual rule entries with missing or invalid `key`/`value` fields
  produce a warning and the engine skips them; valid entries in the same
  document still apply.
- Unknown `location` values default to `"trailer"` with a warning.
- Unknown `separator` values default to `"blank_line"` with a warning.
- Duplicate lines are automatically detected and skipped (both in body
  and trailer locations).
