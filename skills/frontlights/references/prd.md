# Proportionate PRD

Read the discovery log, repository instructions and relevant implementation.
Use `templates/prd.md`; a trivial change may use one compact issue-ready brief.
Include users, measurable outcome, current/proposed behavior, scope/non-scope,
observable acceptance criteria, constraints, dependencies, risks, rollout and
rollback when applicable, open questions and evidence links. Mark inapplicable
sections with a reason rather than inventing work.

Trace every acceptance criterion to an approved decision. Check feasibility,
contradictions, regressions and testability. Do not smuggle unresolved product
choices into technical assumptions. Use an independent critique where supported;
it does not substitute for human approval.

Write the PRD in the user's language. Before asking for approval, show the
complete revision: the full text in the conversation, the file sent with the
host's file-sending tool when one exists, and the text (or as much as fits) in
the `preview` of the approve option. A summary or a path is not showing it.
Then use `AskUserQuestion`, with options, for material decisions and explicit
PRD approval, and preserve the response reference. Do not publish issues
or implement code from PRD approval alone. Return the approved revision to the
coordinator; later material changes invalidate downstream approvals.
