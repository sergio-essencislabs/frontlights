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
this checks structure, DAG, board fields and `parent` links (see Follow-ups), not semantic verticality. Review every vertical demonstration manually. Check
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
`sources.project`), every top-level issue this stage creates or adopts belongs on
that board; never publish one outside it. A sub-issue joins only through its parent
and takes none of the board writes below (see Follow-ups). Plan it with the issue:

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

Put `Depends on: #N, #M` and `Parent: #P` in each canonical issue body (or `none`);
verify dependency IDs, parent and content after publication. Link local snapshots to returned issue URLs.
An issue update preserves user text outside the approved plan. Surface divergence
between approved draft and GitHub rather than overwriting it silently. Return
verified issue links/graph to the coordinator for batch authorization.

## Follow-ups

Findings from an independent review, a test run or a live check that belong to an
issue never become one new issue each. Classify every finding by the first
destination that fits:

1. Fix it in the same PR, when it is inside the issue's approved scope and ownership.
2. A checklist item (`- [ ] ...`) in the source issue's body, for a small leftover
   that does not need its own branch or review.
3. A sub-issue of the source issue, for work of its own that still belongs to that
   outcome.
4. A top-level issue only for new scope or for a problem that crosses several
   issues; then make it a sub-issue of the epic that gathers them, when one exists.

No finding becomes a top-level issue by default. Show the whole batch (each finding,
its proposed destination with the reason, and the full text of every checklist item
and sub-issue body) under the show-before-approval rule, then decide it in a
single `AskUserQuestion`: one option approves the proposed destinations and their named
writes, others adjust them. Approving the batch approves exactly those writes; it
is not an implementation authorization for the new work.

Record each sub-issue in `plan.json` with `parent` set to the id of the source issue,
which must be in the same plan (a published issue keeps its GitHub number as id), so
`validate-plan` checks the link without network. A sub-issue takes no
`project_fields`. Give the parent `existing_sub_issues` with the `subIssuesSummary.total`
read from GitHub, so the check counts the children it already has (GitHub allows 100
per parent). When the parent is itself a sub-issue, include its ancestors too, so
the depth check (8 levels below a top-level issue) sees the whole chain.

Publish a sub-issue in this order, pausing a few seconds between creations to stay
under the secondary rate limit:

- Read what the child inherits: `gh issue view <parent> --repo <repo> --json
  assignees,labels,issueType,state`.
- Create it already linked: `gh issue create --repo <repo> --title <title> --body-file <file> --parent <parent>`
  plus one `--assignee <login>` per parent assignee, one `--label <name>` per parent
  label and `--type <issueType.name>` when the parent has a type. Put `Parent: #<parent>`
  in the body. For an issue that already exists, link it with `gh issue edit <child>
  --repo <repo> --parent <parent>` (or `gh issue edit <parent> --repo <repo>
  --add-sub-issue <child>`).
- A `gh` without these flags falls back to the REST route: `gh api
  repos/<owner>/<repo>/issues/<parent>/sub_issues --method POST -F sub_issue_id=<id>`,
  where `<id>` is the child's numeric `id` from `gh api repos/<owner>/<repo>/issues/<child>`
  (not its number).
- Run no board command for the child: the parent's card shows the children's
  progress (x/y).

Verify by rereading GitHub: `gh issue view <child> --repo <repo> --json
parent,projectItems,assignees,labels,issueType` must show `parent.number` equal to the
parent, an empty `projectItems` and the inherited values; `gh issue view <parent>
--repo <repo> --json subIssuesSummary` must show the total grown by the new children
(the REST listing `gh api repos/<owner>/<repo>/issues/<parent>/sub_issues` is the
fallback). A child that landed on a board anyway (a project auto-add rule, say) is a
divergence to report, not to undo. A failed link is a partial publication: retry only
the link, never by creating the issue again.

A parent is reported complete only when every child is closed
(`subIssuesSummary.completed` equal to `total`); otherwise list the open children.
A late finding, after the parent was merged or closed, still becomes a sub-issue of
it: say that the parent must be reopened for its progress to count, and leave that
decision to the user; never reopen, close, move or archive an issue yourself.
