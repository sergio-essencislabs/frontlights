# Vertical issue planning

Read the approved PRD revision and current GitHub issues/board. Reuse an existing
matching issue when its acceptance scope fits; never create a duplicate local task.
Use `templates/issue.md` and `examples/plan.json`. Before publication, temporary
integer IDs are proposal IDs only; remap every dependency to real GitHub numbers
after creation and verify links. Local status is always a timestamped snapshot.

Each issue must have outcome, boundaries/non-goals, observable acceptance,
approach and conventions to follow, seams under test, tests, dependency IDs, concrete ownership (or `*` when unknown), risks and a
session-sized end-to-end demonstration. Do not split by UI/API/database layers.
For an observation editor, prefer "save one observation and see it after reload"
with UI/API/storage/tests in one slice, then "edit with conflict feedback", then
"filter observations". If the proposal starts as database/backend/frontend
tickets, correct it yourself before presenting it; no extra user prompt needed.
A genuine prerequisite must expose a working seam tested by a named consumer.

Seams under test name the public interfaces where each acceptance behavior is
observed (an HTTP endpoint, a CLI command, a module's exported function, a UI
action), never internals. Favor critical paths and complex logic over every edge
case. `to-development` writes tests only at these seams, so approving the issue
approves them; name them in the approval table too.

Run `frontlights.py validate-plan --plan <plan.json> --config <.frontlights/config.json>`;
this checks structure, DAG and board fields, not semantic verticality. Review every vertical demonstration manually. Check
missing cross-layer tests, hidden sequencing, generated/shared files, migrations,
ports, shared services, dependency cycles and context size. Add dependencies or
shared ownership for resources that cannot safely run together. Split oversized
issues into independently acceptable outcomes. See `evals/vertical-slices.md`.

Before asking for approval, show the complete plan in the user's language: the
table of slices with dependencies and ownership, and the full body of every
issue, in the conversation, as a file sent with the host's file-sending tool
when one exists, and in the `preview` of the approve option (as much as fits).
Then ask approval through `AskUserQuestion`, with options, for the exact issue
creation/update scope before any write. A local project without a remote writes
the approved bodies under `.frontlights/issues/<n>/` with `source: "local"`
snapshots; the same approval rule applies. Use installed GitHub
tools or `gh issue create/edit --repo <approved-repo> --body-file <file>` with
literal UTF-8 bodies, not interpolated shell strings. Keep native permissions.
If a network response is uncertain, reread/list issues before retrying to avoid
duplicates. Record partial publication and resume remaining writes idempotently.

## Project board

When `.frontlights/config.json` has `project` (reported by `inspect` as
`sources.project`), every issue this stage creates or adopts belongs on that board;
never publish one outside it. Plan it with the issue:

- For each issue, record `issue_type` (or rely on `project.issue_type`) and
  `project_fields` for every board field whose default is `null`. Choose values
  that exist as options on the board; `validate-plan --config` refuses the plan
  while any field is still open.
- Show the board values of every issue in the approval table (type, assignee and
  each field), so approving the plan approves them. They are part of the named
  writes, not a later step.
- After `gh issue create`, in this order: `gh project item-add <number> --owner
  <owner> --url <issue-url>`; `gh issue edit <n> --repo <repo> --add-assignee
  <assignee> --type <issue_type>`; one `gh project item-edit <number> --owner
  <owner> --url <issue-url> --field <name> --value <value>` per field. Use
  `assignee` exactly as configured (`@me` is whoever runs `gh`), never a guessed login.
- Verify by rereading GitHub (the issue's `projectItems`, `issueType`, `assignees`
  and each single-select value) and report any value that did not land. A failed
  board write is a partial publication: record it and retry only the missing
  writes, never by creating the issue again.
- If `sources.project` is `unavailable` (commonly a `gh` token without the `project`
  scope), do not publish silently outside the board. Report the exact blocked
  operation and give the user the command to fix it (`gh auth refresh -s project`).

Without `project` configured: for a repository owned by an organization, ask
through `AskUserQuestion` whether its issues live on a board (options: a board
you found with `gh project list --owner <owner>`, "no board", free text). Record
the answer in `.frontlights/config.json` as `project` (or `"project": null`) before
publishing, so later sessions do not ask again. A local project has no board.

Put `Depends on: #N, #M` in each canonical issue body (or `none`); verify dependency
IDs and content after publication. Link local snapshots to returned issue URLs.
An issue update preserves user text outside the approved plan. Surface divergence
between approved draft and GitHub rather than overwriting it silently. Return
verified issue links/graph to the coordinator for batch authorization.
