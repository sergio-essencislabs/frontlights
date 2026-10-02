# Browser, checks and cross-account testing at the end of a slice

Read this in stage 6 after the TDD loop of an issue whose plan has the section
`## Navegador e testes ligados` (`templates/issue.md`). It decides what runs, with
which helper, what to ask and what to record. Everything the user reads stays in
Portuguese. `checks integration` and `checks smoke` refuse a non-local host
before any request; `serve start` does not check the `health` host, so keep
`health` and `baseUrl` on `127.0.0.1`, `localhost` or `::1`, on the issue's own
branch; never point a test at a real, staging or production environment. Installing Playwright or a browser in the target project is out of
scope: use what the session and the project already have.

## What the plan turns on

Read the four fields of that section and restate them in the handoff before
running anything:

- `Toca o frontend:` `sim` turns the browser test on; `não` skips it.
- `Conta:` `conta 1` (the default) or `contas 1 e 2`.
- `Fluxo:` the screens and actions to exercise, with the expected result.
- `Testes ligados:` any of `navegador`, `integração`, `regressão`,
  `permissões entre contas`, `smoke`, plus extra cases.

A missing section on a slice that changes screens, or a field that contradicts the
diff, parks the issue and goes to `AskUserQuestion`; never turn tests on or off
alone. Run only what is on, in this order: `checks regression`, `serve start`,
`checks integration`, browser and cross-account steps, `checks smoke` (it reuses
the running serve), `serve stop`. The commands below read `browserTest` and `checks` from the project's
`.frontlights/config.json` (main worktree included, as stage 2 says).

## Environment: `serve`

```powershell
python "${CLAUDE_PLUGIN_ROOT}/scripts/serve.py" start --config <config> --root <worktree> --issue <n>
python "${CLAUDE_PLUGIN_ROOT}/scripts/serve.py" status --config <config> --root <worktree> --issue <n>
python "${CLAUDE_PLUGIN_ROOT}/scripts/serve.py" stop --config <config> --root <worktree> --issue <n>
```

`serve start` brings up every process of `browserTest.processes` in the issue's
worktree, waits for each health URL and records pids in
`.frontlights/serve/<n>.json`; `serve stop` kills the process trees and releases
the ports. Exit `0` done, `1` refused or failed; the JSON `category` says:

- `uso`: invalid config or a busy lock;
- `infraestrutura`: everything else, an unreadable or missing config included.

Either way, ask (see below).

Ports. A process with `"port": "auto"` gets a free port reserved per process in the
repository's Git common directory, shared by every worktree, and receives it in
`PORT`, `FRONTLIGHTS_PORT_<NAME>` and `{port}` in its argv. Its health must carry
`{port}` in place of the host's port (`http://127.0.0.1:{port}/health`); `{port}`
only in the path is refused as `uso`. The output reports
`reservas_compartilhadas`. Use `auto` only when the process really accepts the
port by variable or argument. A dev server that pins its own ports keeps a fixed
`port`, and then only one issue at a time can run it: say so when recommending
parallelism. A process that ignores the reserved port fails the start as
infrastructure, naming the process.

Stop what this session started before reporting the slice, and say in one line
which processes are still up when the user chose to keep them.

## Checks: `checks`

```powershell
python "${CLAUDE_PLUGIN_ROOT}/scripts/checks.py" regression --config <config> --root <worktree> `
  --base <base-checkout> --issue <n>
python "${CLAUDE_PLUGIN_ROOT}/scripts/checks.py" integration --config <config> --root <worktree> `
  --issue <n> --base <base-checkout>
python "${CLAUDE_PLUGIN_ROOT}/scripts/checks.py" smoke --config <config> --root <worktree> `
  --issue <n> --base <base-checkout>
```

- `checks regression` runs the suite on the base and then on the branch; only a
  new failure blocks, a failure already on the base is reported apart.
- `checks integration` runs `checks.integration.argv` against the backend that
  `serve start` brought up; it never starts anything itself.
- `checks smoke` starts the serve when none is up, requests the declared paths and
  stops only what it started; a warning "Rode `serve stop`" means an orphan was
  left: tell the user and offer to run `serve stop`.

Exit codes: `0` passed, `1` product failure, `2` config or usage refused (nothing
ran), `3` infrastructure. Records go to `.frontlights/issues/<n>/checks/`; those
of `integration` and `smoke` carry `simulacao: false`. A login or password of `browserTest.users` shorter than four
characters makes `integration` and `smoke` refuse; both values are masked in every
output and record.

## Browser

Explore with the browser available in the session (Chrome or Claude's browser);
run the project's own Playwright specs when it already has them. Open the
`browserTest.baseUrl` of the branch, log in with account 1 and walk the `Fluxo:`
step by step, comparing each screen with the expected result. Collect console
errors, failed network requests and a screenshot per step. Type credentials only
into the login form; never repeat them in the conversation, a record or a
screenshot caption.

Without a browser or network the test did not run: report the reason, record it,
and the slice never counts as passed on that test.

## Cross-account permissions

Account 1 is the first entry of `browserTest.users`, account 2 the second.
Account 1 runs every browser test. Account 2 enters only when `Conta:` names it or
`Testes ligados:` includes `permissões entre contas`. Then:

1. With account 1, create or open the data the slice touches and note its
   identifiers and URLs.
2. Log out, log in with account 2 and try to reach that data: lists, search,
   direct URL, edit and delete actions, and the API calls the screens make.
3. Repeat the other way when the plan says the data is private to both.

Any data of one account visible to, or changeable by, the other is a
product failure. A missing account 2 in the config is a config gap: ask, never
reuse account 1 as both.

## Every failure is a question

Classify each failure before asking:

- **infrastructure failure**: `serve` category `infraestrutura`, `checks` code
  `3`, no browser or network, login page unreachable.
- **product failure**: `checks` code `1`, a new regression failure, a wrong
  screen, console or network error caused by the slice, failed login with valid
  test accounts, data crossing accounts.
- **config refusal**: `serve` category `uso` or `checks` code `2`.

Report the three kinds separately, then ask with `AskUserQuestion`, one question
per failure (batched when several), with the evidence path in the text and options
such as "Bloquear a issue", "Corrigir e testar de novo", "Seguir e registrar o
risco". Never decide alone to retry, ignore or continue; a slice with a failure
is reported as passed only when every test passed or the user chose to go on, and
the answer is recorded verbatim.

## Evidence

Before each browser run, take `python "${CLAUDE_PLUGIN_ROOT}/scripts/frontlights.py" evidence --root <worktree>`.
Write `.frontlights/issues/<n>/browser/result.json` with timestamp, branch,
`head`, `diff_sha256`, the account used (`conta 1` or `conta 2`, never the login),
each step and its result, console and network errors, screenshot file names, the
failure kind and the user's answer. Screenshots stay in the same folder. The
records of `.frontlights/issues/<n>/checks/` sit beside it. A later commit or
diff makes this evidence stale: rerun before reporting. Link both folders in the
handoff.

A summary comment on the GitHub issue is text only (no screenshots, no login,
no password, no local path) and is posted only when the recorded authorization
allows routine issue updates; otherwise keep it in the handoff as pending.
