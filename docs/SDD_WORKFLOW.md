# Lean Spec-Driven Development Workflow

## 1. Purpose

This document defines the Spec-Driven Development (SDD) workflow for the Free LLM Router project.

The goal is to gain the benefits of SDD without introducing unnecessary process, context consumption, or repeated work for the AI coding agents used on the project.

The project is intentionally being built with a small amount of available AI inference capacity. The workflow therefore favors:

- Small, focused specifications.
- Minimal persistent agent context.
- Progressive disclosure of project information.
- Small implementation batches.
- Frequent verification.
- Repository-based project memory.
- GitHub Projects for planning and work tracking.
- The simplest process that preserves clear engineering intent.

This workflow is inspired by the useful ideas behind full SDD systems such as Spec Kit, but does not require Spec Kit or any particular SDD framework.

---

## 2. Source-of-Truth Hierarchy

Different artifacts answer different questions. They must not be used interchangeably.

### `CONSTITUTION.md`

Answers:

> What principles and constraints govern how this project should be built?

The Constitution contains stable engineering principles, hard constraints, and long-lived architectural rules.

It should change rarely.

### `ROADMAP.md`

Answers:

> What are we building, why, and in what progression?

The Roadmap defines milestones and the intended evolution of the product.

It should remain relatively high-level.

### `specs/<feature>/spec.md`

Answers:

> What must this particular capability do?

The specification is the behavioral contract for a feature.

It contains requirements, acceptance scenarios, edge cases, and explicit non-goals.

### `specs/<feature>/plan.md`

Answers:

> How will this specification be implemented?

The plan records the technical approach, affected components, interfaces, data changes, dependencies, and testing strategy.

### `specs/<feature>/tasks.md`

Answers:

> What concrete implementation work needs to happen?

Tasks decompose the approved plan into manageable implementation steps.

### `docs/adr/`

Answers:

> Why was an important architectural decision made?

Architecture Decision Records capture significant decisions that should remain understandable after the original discussion is forgotten.

### GitHub Projects

Answers:

> What work is currently planned, active, blocked, or completed?

The GitHub Project is the authoritative work-state and work-log system for the project.

It replaces the need for a repository-level `STATUS.md`.

### Source code and tests

Answers:

> What does the system actually do?

The implementation must satisfy the applicable specifications and acceptance criteria. Passing tests alone does not make an implementation correct if the specification is not satisfied.

---

## 3. Core SDD Loop

Every meaningful feature should follow this sequence:

```text
Roadmap
   ↓
Feature Specification
   ↓
Clarification, when necessary
   ↓
Implementation Plan
   ↓
Task Breakdown
   ↓
GitHub Project Work Tracking
   ↓
Implementation
   ↓
Tests
   ↓
Verification / Convergence
   ↓
GitHub Project Completion
   ↓
Next Feature
```

The process is intentionally lightweight.

Not every small bug fix or maintenance change requires a complete specification.

---

## 4. Feature Decomposition

A Roadmap milestone should normally be decomposed into several independently implementable features.

For example:

```text
M0 Technical Prototype
├── 001 Project Bootstrap
├── 002 OpenAI-Compatible Ingress
├── 003 Google Provider Adapter
└── 004 Basic Response Normalization
```

A later milestone might become:

```text
M1 MVP Proxy
├── 005 Mistral Provider Adapter
├── 006 Provider/Model Registry
├── 007 API Key Pool
├── 008 Key Selection
├── 009 Quota Reservation
└── 010 Basic Failover
```

This decomposition keeps each agent interaction bounded and ensures that useful functionality can be delivered incrementally.

A feature should be large enough to represent a coherent capability but small enough that its specification, plan, implementation, and verification can be understood without loading the entire project into an agent's context.

---

## 5. When a Feature Needs a Specification

Create a feature specification for coherent capabilities such as:

- OpenAI-compatible API endpoints.
- Provider adapters.
- Key pooling and selection.
- Quota accounting.
- Persistence.
- Streaming.
- Tool calling.
- Capability-aware routing.
- Quality-based model selection.
- Adaptive routing.
- Administrative APIs.
- Web UI features.
- Provider onboarding.

A full specification is generally unnecessary for:

- Typo fixes.
- Small documentation corrections.
- Simple refactors with no behavioral change.
- Dependency patch updates.
- Trivial bug fixes whose expected behavior is already unambiguous.

When in doubt, prefer a small spec over silently making a material behavioral decision.

---

## 6. The Specification

Each feature specification should normally live at:

```text
specs/<feature-id>-<short-name>/spec.md
```

For example:

```text
specs/002-openai-ingress/spec.md
```

A specification should normally contain:

```text
# Feature Name

## Purpose

## Scope

## Requirements

## Behavioral Rules

## Acceptance Scenarios

## Edge Cases

## Error Behavior

## Security / Privacy Considerations

## Explicit Non-Goals
```

The specification should describe observable behavior rather than prematurely dictate implementation details.

Prefer:

> When multiple eligible keys exist, select the key that has gone the longest without being used.

over:

> Implement an `LRUCache` class.

The first is a requirement. The second is an implementation choice.

---

## 7. Acceptance Scenarios

Acceptance scenarios are one of the most important parts of the SDD process.

Write requirements so that an implementation can later be tested against them.

Example:

```text
Scenario: Select the least recently used eligible key

Given Key A was used at 10:00
And Key B was used at 10:05
And both keys are eligible
When a request arrives
Then Key A is selected
```

Also specify failure cases.

Example:

```text
Scenario: All keys are unavailable

Given all keys for the requested provider/model are cooling down
When a request arrives
Then the router must not dispatch the request
And it must return the defined normalized failure response
```

Acceptance scenarios should cover normal behavior, boundary conditions, and important failure modes.

---

## 8. Clarification

Clarification is optional.

Use it when the feature specification contains ambiguity that could materially alter behavior, architecture, security, or user-visible results.

Do not turn clarification into a mandatory ceremony.

For simple features:

```text
spec
→ plan
→ tasks
```

For ambiguous or complex features:

```text
spec
→ clarification
→ plan
→ tasks
```

Never invent a material requirement merely to avoid asking a necessary question.

When working through ambiguity with an AI agent, prefer recording the resulting decision in the specification or an ADR so the decision does not have to be reconstructed later.

---

## 9. Implementation Plan

Each feature should normally have:

```text
specs/<feature-id>-<short-name>/plan.md
```

The plan should translate the behavioral specification into a technical approach.

A plan should normally include:

```text
## Technical Approach

## Components Affected

## Interfaces

## Data / Persistence Changes

## External Integrations

## Error Handling

## Testing Strategy

## Risks / Tradeoffs

## Architectural Decisions
```

The plan should respect the Constitution and Roadmap.

It should also favor the simplest design that satisfies the current milestone.

Do not implement future roadmap capabilities merely because the architecture could support them.

Build extension points only where doing so is inexpensive and materially reduces future rework.

---

## 10. Architecture Decision Records

Create an ADR when a decision is:

- architecturally significant,
- difficult to reverse,
- likely to be questioned later,
- or useful for future AI agents to understand.

Examples include:

```text
docs/adr/0001-initial-technology-stack.md
docs/adr/0002-provider-adapter-boundary.md
docs/adr/0003-local-persistence.md
docs/adr/0004-quota-accounting-model.md
```

An ADR should capture:

```text
Context
Decision
Reasoning
Alternatives Considered
Consequences
```

Do not create ADRs for ordinary implementation details.

---

## 11. Task Breakdown

Each feature should normally have:

```text
specs/<feature-id>-<short-name>/tasks.md
```

Tasks should be:

- small enough to implement in a bounded agent interaction,
- ordered where dependencies matter,
- directly traceable to the plan,
- testable.

Example:

```text
- [ ] T001 Create project structure
- [ ] T002 Add configuration loader
- [ ] T003 Add structured logging foundation
- [ ] T004 Add configuration unit tests
```

Avoid creating dozens of extremely granular tasks.

The purpose of `tasks.md` is to give the implementer a clear set of bounded work units, not to reproduce every edit that will be made.

---

## 12. GitHub Projects as the Work Log

GitHub Projects is the authoritative system for project work state.

The repository documents **what the software should do**.

The GitHub Project documents **what work is being planned and what is happening to it**.

Do not create a parallel `STATUS.md` or maintain a second work log in the repository.

### Recommended relationship

A feature specification should correspond to a GitHub Project item, normally an Issue.

Conceptually:

```text
Roadmap Milestone
      ↓
GitHub Issue
      ↓
spec.md
      ↓
plan.md
      ↓
tasks.md
```

The exact GitHub Project fields may evolve, but the Project should at minimum make it possible to identify:

- Roadmap milestone.
- Feature/specification.
- Current work state.
- Blocked work.
- Completed work.
- Relevant pull request or commit where applicable.

### GitHub Project is not the requirements source of truth

The Project should not contain the complete technical specification.

Avoid duplicating `spec.md`, `plan.md`, and `tasks.md` into giant Issue descriptions.

Instead, the GitHub Issue should link to the relevant repository artifacts.

This reduces duplication and reduces the amount of context agents need to process.

---

## 13. Using the `gh` CLI

The `gh` CLI should be used to keep the GitHub Project synchronized with actual development activity.

At the start of a work session:

```text
1. Identify the feature being worked on.
2. Locate its GitHub Project item/Issue.
3. Move/update the item to the active work state.
4. Begin implementation against the current spec and plan.
```

At the end of a work session:

```text
1. Run relevant tests and verification.
2. Record any remaining work in the GitHub Project.
3. Move/update the item to the appropriate state.
4. If the feature is complete, mark it complete and reference the relevant commit/PR.
5. If blocked, record the blocker in GitHub.
```

The `gh` CLI should be treated as an **operational interface to the project's work-tracking system**, not as a replacement for repository documentation.

The coding agent should not fabricate Project status.

A work item should only be moved to a completed state when the implementation has been verified against the applicable specification and acceptance criteria.

### Project board commands

This repository uses a GitHub Projects v2 board. There are no classic
project boards.

```text
gh project list --owner Laughing-Man-Studios
gh project field-list <number> --owner Laughing-Man-Studios
gh project item-list <number> --owner Laughing-Man-Studios
```

The board's `Status` field accepts: `Backlog`, `Ready`, `In progress`,
`In review`, `Done`.

Move an item by issue URL, which avoids having to look up the internal
project item ID first. The project number is a positional argument;
`--id` refers to the project *item* ID (`PVTI_...`) and must not be
combined with `--url`.

```text
gh project item-edit <project-number> --owner Laughing-Man-Studios \
  --url <issue-url> --field "Status" --value "In progress"
```

The board also carries `Start date`, `Target date`, `Priority` and
`Size` columns. These are currently empty. If they are populated later,
note that they are separate from the issue-level custom fields of the
same name and must be kept consistent with them.

### Issue fields

`gh issue edit` covers titles, bodies, labels, milestones, assignees,
sub-issue relationships (`--parent`, `--add-sub-issue`) and issue types
(`--type`).

Issue-level custom fields (Start date, Target date, Priority, Effort)
have no CLI flag. Write them with the `setIssueFieldValue` GraphQL
mutation via `gh api graphql`, reading each field's `id` and any
single-select `optionId` from the repository's `issueFields` first.
See AGENTS.md for the governing convention.

---

## 14. Implementation Sessions

An AI coding agent should normally be given only the context needed for the current task.

The default context bundle should be:

```text
AGENTS.md
Current spec.md
Current plan.md
Current tasks.md
```

Add other files only when the task requires them.

Do not routinely provide:

```text
entire Roadmap
all specifications
all ADRs
unrelated provider documentation
entire repository
```

unless the current decision genuinely depends on that information.

This progressive-disclosure strategy is intended to conserve AI context and reduce unnecessary token consumption.

---

## 15. Agent Instructions

The repository should contain a small:

```text
AGENTS.md
```

It should provide only instructions that are useful across essentially every coding session.

At minimum, it should tell agents to:

- read the Constitution when making architectural decisions,
- treat the current feature specification as the behavioral contract,
- avoid implementing functionality outside the current scope,
- favor the simplest implementation that satisfies the current milestone,
- run relevant tests,
- avoid changing requirements merely to accommodate an implementation,
- preserve security and privacy constraints,
- identify material ambiguity rather than inventing requirements.

`AGENTS.md` should remain small.

Do not turn it into a duplicate of the Constitution, Roadmap, or architecture documentation.

---

## 16. Small-Batch Implementation

Do not ask an agent to implement an entire large feature when the task list can be divided into bounded batches.

Prefer:

```text
Implement T001–T004.
Run the relevant tests.
Stop.
```

Then:

```text
Implement T005–T008.
Run the relevant tests.
Stop.
```

Small batches reduce:

- context usage,
- implementation drift,
- diff size,
- debugging scope,
- wasted work when an agent reaches a rate limit,
- and the cost of switching between agents.

The target is maximum useful engineering progress per agent interaction.

---

## 17. Multiple AI Coding Agents

Codex, Google's coding environment, and Mistral Vibe may all be used on the project.

The repository should be the common source of context so that switching agents does not require reconstructing the project from conversation history.

Do not have multiple agents independently implement the same feature.

Prefer a role-based workflow such as:

```text
Architect
    ↓
Implementation
    ↓
Independent Review
    ↓
Corrections
```

For example:

```text
Codex → implementation
Google agent → review
Codex → corrections
```

or the reverse.

The exact agent can change based on availability, task fit, or current rate limits.

The repository artifacts, not any particular AI service, are the durable project memory.

---

## 18. Separate Thinking From Mechanical Work

Use higher-context reasoning sessions for work that benefits from discussion and design review:

- requirements discovery,
- architecture,
- tradeoffs,
- specifications,
- complex routing algorithms,
- difficult debugging,
- design reviews.

Use coding agents for bounded implementation work:

- code generation,
- unit tests,
- refactoring,
- straightforward integration,
- repetitive changes,
- local verification.

This division is particularly important because the project is intentionally being built primarily through free-tier AI tooling with limited inference capacity.

---

## 19. Testing and Verification

Testing is part of the SDD process, not a separate activity performed after implementation.

### MVP

Unit tests are required.

Core areas should include:

- request normalization,
- response normalization,
- provider/model identity,
- key selection,
- LRU behavior,
- cooldown behavior,
- quota reservation,
- quota reconciliation,
- concurrency behavior,
- error normalization,
- retry/failover behavior,
- context-limit validation,
- configuration parsing.

### Early follow-up

Build a deterministic fake-provider test harness capable of simulating:

- success,
- 429 rate limits,
- 4xx errors,
- 5xx errors,
- timeouts,
- slow responses,
- quota exhaustion,
- stream interruptions,
- different provider capabilities,
- concurrent requests.

This should allow most routing and failover logic to be tested without consuming real free-tier provider quota.

### Manual integration testing

Manual tests remain appropriate for:

- real provider request/response translation,
- authentication,
- streaming,
- OpenHands compatibility,
- provider-specific behavior.

---

## 20. Verification / Convergence

After implementation, explicitly verify the implementation against the specification.

A verification pass should inspect:

```text
spec.md
tasks.md
git diff
tests
```

and answer:

- Are all required behaviors implemented?
- Are acceptance scenarios covered?
- Are important edge cases handled?
- Do tests actually verify the specified behavior?
- Did implementation introduce behavior outside the approved scope?
- Were any Constitution principles violated?
- Are there unresolved architectural or security issues?

The verification step should not silently modify the specification to make the implementation appear correct.

Requirements may only change deliberately and should be recorded in the appropriate project artifact.

---

## 21. Definition of Done

A feature is complete when:

```text
[ ] Specification is complete and understood.
[ ] Plan reflects the approved technical approach.
[ ] Required tasks are implemented.
[ ] Relevant unit tests pass.
[ ] Relevant integration/manual tests pass where applicable.
[ ] Acceptance scenarios are verified.
[ ] Logging and privacy requirements are satisfied.
[ ] No secrets were committed.
[ ] No unintended out-of-scope functionality was introduced.
[ ] Documentation/ADRs are updated where necessary.
[ ] GitHub Project work item reflects the completed state.
[ ] Relevant commit/PR is linked from the work item when applicable.
```

Passing the test suite is necessary but not sufficient.

---

## 22. Handling Bugs Discovered During Implementation

When implementation reveals a bug in an existing feature:

1. Determine whether the bug violates the current specification.
2. If yes, fix it within the current scope when reasonably small.
3. If the correction is materially larger than the current feature, create or update a dedicated GitHub work item.
4. If the required behavior itself has changed, update the specification deliberately before changing implementation.
5. Do not quietly expand scope because an agent discovers an interesting improvement.

Use the GitHub Project to record discovered work rather than creating an undocumented backlog in local files.

---

## 23. Handling New Ideas

New ideas should not automatically enter the current implementation.

When a useful future capability appears:

```text
Idea
  ↓
Record in GitHub Project/backlog
  ↓
Determine whether it belongs on the Roadmap
  ↓
Create a future specification when the work is ready
```

This protects the current task from scope creep.

---

## 24. Avoid Premature Abstraction

The project should be designed for extension but not generalized unnecessarily.

Good early abstraction:

```text
Router
   ↓
ProviderAdapter interface
   ├── GoogleAdapter
   └── MistralAdapter
```

Poor early abstraction:

```text
UniversalLLMProtocolEngine
CapabilityOntologyManager
GenericDistributedQuotaOrchestrator
```

when the project only has two providers and one node.

Use interfaces and boundaries where they isolate genuine variation.

Do not build abstractions solely because the Roadmap might eventually need them.

---

## 25. Roadmap Evolution

The Roadmap should evolve as the project generates real information.

Update it when:

- a milestone is completed,
- a design assumption is invalidated,
- a future feature becomes clearly unnecessary,
- a new architectural constraint emerges,
- a milestone needs to be decomposed further,
- or implementation experience materially changes the expected progression.

Do not update the Roadmap merely because a small task was completed.

The Roadmap remains a product-level planning artifact, not a task log.

GitHub Projects remains the detailed work-state system.

---

## 26. Project Directory Convention

The recommended repository structure is:

```text
Free-LLM-Router/
│
├── AGENTS.md
├── CONSTITUTION.md
├── ROADMAP.md
│
├── docs/
│   ├── SDD_WORKFLOW.md
│   └── adr/
│       ├── 0001-initial-technology-stack.md
│       └── ...
│
├── specs/
│   ├── 001-project-bootstrap/
│   │   ├── spec.md
│   │   ├── plan.md
│   │   └── tasks.md
│   │
│   ├── 002-openai-ingress/
│   │   ├── spec.md
│   │   ├── plan.md
│   │   └── tasks.md
│   │
│   └── ...
│
├── src/
├── tests/
└── ...
```

There is intentionally no `STATUS.md`.

GitHub Projects holds current work state.

---

## 27. Standard Feature Lifecycle

For each substantial feature:

### Step 1 — Select the Roadmap item

Identify the milestone and capability being implemented.

### Step 2 — Create the GitHub work item

Create or identify the Issue/Project item representing the feature.

Link it to the relevant Roadmap milestone.

### Step 3 — Write `spec.md`

Define behavior, acceptance scenarios, edge cases, and non-goals.

### Step 4 — Clarify if needed

Resolve only ambiguities that materially affect implementation.

### Step 5 — Write `plan.md`

Choose the simplest technical approach consistent with the Constitution and specification.

Create an ADR when a significant architectural decision deserves a permanent explanation.

### Step 6 — Write `tasks.md`

Break implementation into bounded tasks.

### Step 7 — Start work

Use the `gh` CLI to move the Project item into the appropriate active state.

Give the coding agent only the relevant context.

### Step 8 — Implement in small batches

Complete a small group of tasks, run relevant tests, inspect the result, then continue.

### Step 9 — Verify

Compare the implementation against the specification and acceptance criteria.

### Step 10 — Complete the work item

Use the `gh` CLI to update the Project item when the feature is actually complete.

Link the relevant PR/commit where appropriate.

### Step 11 — Continue

Select the next ready feature rather than accumulating large unverified changes.

---

## 28. Efficiency Principles for AI-Assisted Development

Because AI inference capacity is a scarce resource, use the following priorities:

1. **Repository artifacts over conversation memory.**
2. **Current feature context over entire-project context.**
3. **Small tasks over giant implementation requests.**
4. **Verification over repeated regeneration.**
5. **Reuse existing decisions instead of rediscovering them.**
6. **Ask for clarification only when ambiguity is materially important.**
7. **Prefer deterministic tests over repeated real-provider calls.**
8. **Avoid unnecessary provider API calls during development.**
9. **Do not have multiple agents independently solve the same problem.**
10. **Use the cheapest adequate agent interaction for the job.**

The goal is not to minimize the number of files or agent prompts at all costs.

The goal is to minimize wasted inference while preserving a clear, reviewable engineering process.

---

## 29. Relationship to the Project Constitution

The Constitution has priority over ordinary implementation convenience.

When a proposed implementation conflicts with the Constitution:

```text
Constitution
     ↓
resolve conflict
     ↓
then update plan/tasks/code
```

Do not work around a constitutional constraint implicitly.

If the project genuinely needs to change a constitutional rule, amend the Constitution deliberately and record the reason.

---

## 30. The Guiding Principle

The purpose of this workflow is not to create process for its own sake.

The desired outcome is:

> **Every meaningful piece of functionality should have an explicit behavioral contract, a consciously chosen implementation approach, bounded work items, and a verifiable completion state—while consuming as little unnecessary AI context as possible.**

The process should remain lightweight enough that it helps development rather than becoming development.
