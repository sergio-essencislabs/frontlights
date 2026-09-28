# Bounded implementation

Read the recorded authorization, current GitHub issues and `docs/security.md`.
Without exact issue IDs, concurrency, permitted worktree/repository operations,
verification commands, monitoring arrangement and stop conditions, return the
missing decision to the coordinator's `AskUserQuestion`. Never infer permission
for external writes from local shell access. Run structured authorization checks
where applicable; they are advisory preflight, not sandbox enforcement.

## Dispatch and ownership

Inspect Git status/remotes/worktrees and current issue acceptance criteria.
Create one isolated branch/worktree per issue from the approved base using native
Git tooling; verify the resolved path, actual branch and common repository before
editing. Never reuse a dirty shared checkout, discard changes or write a protected
branch. Recheck branch/worktree identity before each mutation boundary.

Branches use the charter's `branch_prefix` (default `claude/`). Choose the
worktree base before asking the authorization: when `inspect` reports
`path_risk` other than `ok`, propose a short base such as `C:/wt/<project>` in
the authorization question itself instead of discovering a
`'$GIT_DIR' too big` failure afterwards. Any other change of base after
approval goes back to the user through `AskUserQuestion`.

Refresh the plan from GitHub and run `frontlights.py schedule --plan <file> --limit
<approved-limit>`. The result is a recommendation, not proof of authority. Start
all eligible non-conflicting issues up to the ceiling with supported generic
subagents or separate supported sessions. Give each a bounded outcome, owned
paths, tests, checkpoint path and no authority to expand scope. Tell workers they
share the codebase and must preserve others' changes. Hide internal worker names.
If concurrent sessions/tools are unavailable, report the capacity limitation.
Do not silently serialize for convenience. Isolate ports, databases and test data
as well as Git. Recompute readiness and refill free slots after each completion.

An issue is `verified` for dependency readiness only when its acceptance evidence
is current and its required changes are present in the dependent issue's approved
base. An unmerged PR alone does not meet this condition. Stacked bases/cherry-picks
need the recorded repository-operation authorization; otherwise park dependents.

## Test-driven development

TDD is the red → green loop. This section is the reference that makes that loop
produce tests worth keeping: what a good test is, where tests go, the
anti-patterns and the rules of the loop. Every subsection applies on every cycle:
consult them before and during the loop, not after.

When exploring the codebase, read `CONTEXT.md` (if it exists) so test names and
interface vocabulary match the project's domain language, and respect ADRs in the
area you're touching.

Follow the conventions recorded in the issue's `## Approach and conventions` and
in the discovery log: structure, naming, error handling, libraries and test
style of the surrounding code. Break a convention only where that section names
the approved break. Finding mid-work that the pattern is concretely worse, or
that the approved approach does not fit, parks the issue and returns the
question to the coordinator's `AskUserQuestion`; never switch approach or
pattern silently.

### What a good test is

Tests verify behavior through public interfaces, not implementation details. Code
can change entirely; tests shouldn't. A good test reads like a specification:
"user can checkout with valid cart" tells you exactly what capability exists, and
it survives refactors because it doesn't care about internal structure. See
[tdd-tests.md](tdd-tests.md) for examples and [tdd-mocking.md](tdd-mocking.md) for mocking
guidelines.

### Seams: where tests go

A **seam** is the public boundary you test at: the interface where you observe
behavior without reaching inside. Tests live at seams, never against internals.

**Test only at pre-agreed seams.** The seams under test are part of each issue
(`## Seams under test` in `templates/issue.md`) and were approved when the issues
were published. You can't test everything; agreeing the seams up front is how
testing effort lands on the critical paths and complex logic instead of every
edge case. Before the first test, restate the issue's seams in the handoff. No
test is written at an unconfirmed seam. If the issue lists none, a listed seam
does not exist or cannot observe the acceptance behavior, or the shape of the
interface is itself in question (how deep the module is, where the seam belongs,
what it should expose), park the issue and return the question "What's the public
interface, and which seams should we test?" to the coordinator's
`AskUserQuestion`. Continue independent issues meanwhile.

### Anti-patterns

- **Implementation-coupled**: mocks internal collaborators, tests private
  methods, or verifies through a side channel (querying the database instead of
  using the interface). The tell: the test breaks when you refactor but behavior
  hasn't changed.
- **Tautological**: the assertion recomputes the expected value the way the code
  does (`expect(add(a, b)).toBe(a + b)`, a snapshot derived by hand the same way,
  a constant asserted equal to itself), so it passes by construction and can
  never disagree with the code. Expected values must come from an independent
  source of truth: a known-good literal, a worked example, the spec.
- **Horizontal slicing**: writing all tests first, then all implementation. Bulk
  tests verify _imagined_ behavior: you test the _shape_ of things rather than
  user-facing behavior, the tests go insensitive to real changes, and you commit
  to test structure before understanding the implementation. Work in **vertical
  slices** instead: one test → one implementation → repeat, each test a **tracer
  bullet** that responds to what the last cycle taught you.

### Rules of the loop

- **Red before green.** Write the failing test first, run it and capture the
  actual expected failure, then write only enough code to pass it and rerun the
  focused tests. Don't anticipate future tests or add speculative features.
- **The red must be behavioral.** An import error or broken harness is not the
  intended red. Fix the harness and obtain a meaningful failing assertion.
- **One slice at a time.** One seam, one test, one minimal implementation per
  cycle, each tied to one acceptance behavior.
- **Refactoring is not part of the loop.** It belongs to the review stage below,
  not the red → green implementation cycle.
- **Evidence is real.** Preserve commands, exit codes, counts and output paths.
  Never invent failure evidence for a nontestable documentation change; state the
  exception and verify the relevant behavior another way.

### After the loop

Run relevant broader tests and actual type checks discovered from repository/CI;
do not assume npm scripts. If no type checker exists, say so and record applicable
syntax/build checks. Inspect the full vertical behavior, diff, regressions and
acceptance coverage. Tests may execute arbitrary code; native permission and
sandbox controls still apply. Unexpected effects stop the affected issue.

## Review, checkpoints and renewal

Require an independent reviewer to inspect the current diff and test evidence.
They must not author the changes under review. Bind review to `frontlights.py
evidence --root <worktree>` hashes/HEAD; any change invalidates review. If no
independent reviewer is available, report "implemented, awaiting independent
review", not completed. Do not manufacture reviewer identities or approvals.

Refactoring happens here. The reviewer may recommend refactors of the green code;
the author applies them without changing behavior or adding tests at new seams,
reruns the full relevant suite and type checks, and records fresh evidence. The
new hashes invalidate the earlier review, so the independent reviewer reviews
again before the issue can be reported as reviewed.

Save compact `templates/handoff.md` and a machine-readable checkpoint before each
session renewal. Include the issue URL and acceptance snapshot, repository map,
decisions, changed files, commands/results, review evidence, blockers and next
concrete step. `frontlights.py checkpoint` records the issue snapshot, handoff and
current Git evidence; `frontlights.py resume` detects drift without overwriting it.
Read both the human handoff and current issue/diff in the fresh session.

Check context using measured accumulated session usage plus a conservative next
step reserve, not remaining context after compaction. Target renewal near 100k;
stop before 150k. Unknown telemetry means no unattended continuation. The helper
checks reported numbers, not the host's actual token meter. If the host cannot
enforce renewal, explicitly report that hard-cap AFK acceptance is unproven.

Update GitHub checkpoints/status only if authorized and supported by current
evidence. Draft PRs also require charter permission and native approval. Never
merge, deploy, release, delete data or close issues as routine AFK work. Park
blocked issues with the precise pending question and continue independent ones.
Report each issue's state, branch/PR, exact tests/results, review evidence,
remaining risk and next action. Leave worktrees and evidence intact for recovery.
