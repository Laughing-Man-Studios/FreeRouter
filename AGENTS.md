# Project Instructions

Read CONSTITUTION.md before making architectural decisions.

The current feature specification is the authoritative behavioral
contract for the feature being implemented.

Do not implement functionality outside the current specification.

Prefer the simplest implementation that satisfies the current
milestone.

Run relevant tests after changes.

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

Read the existing values on the target issues first and preserve
established conventions rather than assuming defaults. Note that label
vocabularies and custom field vocabularies may differ (for example
`effort:S/M/L` labels versus High/Medium/Low field options); derive the
mapping from existing issues instead of guessing.
