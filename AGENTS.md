# Project Instructions

Read CONSTITUTION.md before making architectural decisions.

The current feature specification is the authoritative behavioral
contract for the feature being implemented.

Do not implement functionality outside the current specification.

Prefer the simplest implementation that satisfies the current
milestone.

Run relevant tests after changes. `make check` is the gate that mirrors CI
(see *Verification*).

Do not modify the Roadmap, Constitution, or specification merely to
make an implementation appear compliant.

When requirements are ambiguous, stop and ask before making a
material assumption.

Use the `gh` CLI for all GitHub operations, whether inspecting or
executing actions against repositories and planning projects
(e.g., issues, PRs, project boards, or code changes). Do not use a
GitHub MCP server for these.

Prefer the `gh issue` and `gh pr` subcommands. Sub-issue relationships
are set with `gh issue edit --parent` / `--add-sub-issue`, and issue
types with `gh issue edit --type`.

Issue custom fields (Start date, Target date, Priority, Effort) have
no CLI flag and must be written through `gh api graphql` using the
`setIssueFieldValue` mutation. Read each field's `id` and any
single-select `optionId` values from the repository's `issueFields`
before writing.

Always read the value back after writing it. A successful mutation
response does not prove the value changed; treat the write as
unverified until a follow-up read confirms it.

Two distinct field families share these names. The repository's
issue-level custom fields and the GitHub Project's own columns are
separate objects with separate IDs (`IFD_...` versus `PVTF_...` and
`PVTSSF_...`). The board's `Start date`, `Target date`, `Priority` and
`Size` are not the issue-level fields of the same name. `gh project
item-edit` writes board columns; the GraphQL mutation writes
issue-level fields. Writing one and expecting the other to move is the
common failure, and it fails silently.

Read the existing values on the target issues first and preserve
established conventions rather than assuming defaults. Note that label
vocabularies and custom field vocabularies may differ (for example
`effort:S/M/L` labels versus High/Medium/Low field options); derive the
mapping from existing issues instead of guessing.

## Verification

CI is `.github/workflows/ci.yml`. `make check` runs the same four fast gates in
the same order, so a green local run means the `Lint & Format`, `Type Check`,
`Test` and `Markdown Lint` jobs will pass:

```bash
make check   # ruff check, ruff format --check, mypy --strict src/, pytest, markdownlint
```

`make docker` covers the `Docker Build` job up to the container smoke test, which
only matters when `config.yaml` or the Dockerfile changes.

CI runs on **pull requests**, and on pushes to `main` (plus the historical `M0`).
Pushing a batch or milestone branch does not run CI; the checks appear once a PR
exists.

The suite must pass with **no provider credential set**. CI fails if one is
present, because a suite that needs a key cannot be trusted not to spend quota.
`make test-hermetic` reproduces that condition locally.

Job-by-job detail, including the traps, is in `docs/SDD_WORKFLOW.md` section 19
under *Continuous integration*.

## Branches and milestone integration

Batch PRs target the milestone's integration branch (`M1`, `M2`, ...),
never `main`.

Closing a milestone includes opening an integration PR from the
milestone branch into `main`. This is a required step of close-out, not
an optional courtesy:

```text
milestone branch (M1)  --batch PRs-->  reviewed, merged one at a time
        |
        +--integration PR-->  main    <-- part of milestone close-out
```

M1 closed with its issues and milestone marked done while `main` still
carried only M0, leaving 27 commits of merged and reviewed work on a
feature branch. Every later milestone then stacked on a branch that was
never the trunk, so "merged" and "on main" quietly diverged.

Before reporting a milestone complete, verify the milestone branch is
merged into `main` and that `main` actually contains its work:

```text
gh api repos/OWNER/REPO/compare/main...M1 --jq '.ahead_by, .behind_by'
```

`ahead_by > 0` means `main` is missing that milestone's commits.

Do not report a milestone complete while its integration PR is still
open. The report must distinguish three separate states: work merged to
the milestone branch, the milestone branch merged to `main`, and the
milestone closed. Only the third implies the first two.
