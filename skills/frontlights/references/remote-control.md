# Fixed machine in the Claude mobile app

The `/frontlights` session reads this reference only when the user asks for a
guided Remote Control setup (power settings, the phone test). Stage 0 never
sends the user here on its own: it only shows the `claude rc` guidance. The goal is one thing only: this PC
appears in the Claude mobile app as a machine where the user can open sessions,
and the user has proven it by opening one from the phone.

Everything the user reads is in Brazilian Portuguese (or the user's language).
Every step ends in one `AskUserQuestion` with two to four options; never leave an
open question in prose. Every command goes in its own code block, ready to
copy, with real paths. Configure nothing silently and never start the host
yourself: a host started from your own tools dies with this session.

Be explicit about the difference, because users confuse them: following one
existing session from the phone (the desktop app's remote access, or native
`/remote-control` inside a session) is not a fixed machine. Only the host
started by `claude remote-control` in a terminal that stays open makes the
machine appear for new sessions.

## 1. Read the machine

Run `python "${CLAUDE_PLUGIN_ROOT}/scripts/frontlights.py" monitoring --root .` and
tell the user, in one short paragraph, the two power values and whether a host
process exists. A missing power value reads as unknown, never as zero. No
candidate process is a safe conclusion that no host runs here. A candidate is a
process, not a connected phone.

If a candidate already exists, ask header `Host existente`: `É o meu host, seguir
para o celular` (go to step 5), `Iniciar outro host`, `Não sei` (explain how to
find the window that runs `claude remote-control`).

## 2. The two conditions

State them as requirements, in plain words, then ask two questions together:

- A janela do PowerShell que roda o host precisa ficar **aberta o tempo todo**.
  Fechar a janela encerra o host em segundos; nenhuma configuração de energia
  muda isso.
- A tampa do notebook fica **aberta** até um teste com a tampa fechada passar
  nesta máquina. O PC pode ficar bloqueado.

Header `Condições`: `Concordo`, `Não posso`. Header `Energia`, "A máquina fica na
tomada enquanto você estiver fora?": `Na tomada`, `Na bateria`, `Não sei`.
`Não posso` ends this setup: tell the user the phone can still follow a single
session, and that `/frontlights` will run in local mode. Battery or unknown power
continues with the caveat that only about ninety seconds of locked survival were
ever measured, on battery; promise nothing beyond that.

## 3. Power, only when needed

This step is the offer to make the machine safe to work from the phone with the
lid closed or the PC locked. Always make the offer here, in plain words: with
the current values, say whether closing the lid or locking would put the
machine to sleep or turn the session off. If a measured value is not zero or is
unknown: show the current values and the exact `powercfg` lines for the active
scheme and these two settings only, with the previous values so they can be
restored. The user runs them; you do not change system settings. Say that
locking the PC (`Win+L`) is the safe way to leave it, instead of leaving it
unlocked. Ask header `Ajuste de energia`: `Vou rodar os comandos`, `Deixar como
está`. After `Vou rodar os comandos`, rerun the
preflight and report the new values. A zero lock-display value attempts to stop
lock from entering standby; it is not a proven fix. Never set sleep to "never",
never call `SetThreadExecutionState`, never touch another scheme.

## 4. Start the host in a second window

Tell the user to keep this window open and open **another** PowerShell window,
then paste, with the real folder and a short name derived from it. One window
per project: each project folder needs its own host.

```powershell
cd "<pasta do projeto>"
```

```powershell
claude remote-control --name <nome-curto>
```

`claude rc` is the short alias and works the same. If the folder is not trusted
yet and the quick safety check does not appear by itself, explain that the user
must run `claude` in that folder to trust it, accept, leave with `/exit`, and run
the host command again.

Warn in one line: it is `claude remote-control`, with a space and no dash before
`remote`. `claude -remote-control` is not the host command: the single dash
turns it into short options such as `-r` (resume), which was observed to open a
list of old sessions instead of starting a host. Keep `--spawn`, `--capacity` and
`--permission-mode` at their defaults; never suggest `bypassPermissions`,
`acceptEdits`, `auto` or `dontAsk`.

Ask header `Host`: `A janela mostra o host ativo`, `Apareceu uma lista de
sessões`, `Deu erro ou a janela fechou`.

- List of sessions: explain the one-dash cause, tell them to leave it with `Esc`,
  and repeat this step.
- Error: offer the known causes as options — `Não estou logado` (run `claude`,
  then `/login`, in that window), `Pediu para confiar na pasta` (run `claude`
  once in the folder, accept, `/exit`, then the host command), `Outro erro`
  (paste it in the free-text choice). Repeat this step afterwards.

## 5. Confirm the host exists

Rerun the preflight. With no candidate process, say plainly that the host is not
running on this machine, whatever the window seems to show, and return to step
4 with header `Host`: `Tentar de novo`, `Ficar no modo local`. Never continue to
the phone without a detected process.

## 6. Pin it on the phone

Tell the user: no app Claude do celular, entre na área de código (Code), procure a
máquina `<nome-curto>` na lista de ambientes ou sessões, inicie uma sessão pelo
dispositivo escolhendo o repositório desejado, envie "teste" e espere a resposta.
Explain that this session stays synchronized between the phone and the desktop
window as long as the host window stays open. Menu names may vary with the app version; say so rather than
inventing exact labels. Ask header `Celular`: `A máquina aparece e respondeu`,
`Não aparece`, `Aparece mas não abre`.

- Does not appear: the same account on phone and PC, the app updated, pull to
  refresh, the host window still open; then ask again.
- Appears but does not open: check the host window for a message and rerun the
  preflight; then ask again.

Only a detected host plus `A máquina aparece e respondeu`, given in this session,
records `phone_connected: true`. A process, a URL, a QR code or a prompt shown
on screen is never that evidence.

## 7. Record and hand back

Write `.frontlights/monitoring.json` in the project as a historical record: mode
`phone`, `confirmed_by`, `observed_phone_confirmation` with its time, host name,
detected process id, measured power values, caveats and
`"valid_while_process_runs": true`. Never write `phone_connected` there; the
preflight of later sessions reads this record and reports `host.known` only while
the same process, started before the confirmation, is still running. Write nothing else, and nothing outside `.frontlights/`: no
scheduled task, startup shortcut, service or background logon host (hosts
started that way died about twenty seconds after the phone connected).

Close with the reminders — host window open, lid open, PC may lock — rerun the
preflight, record the stage 0 `monitoring` block from it, and go back to where
the `/frontlights` session was. From then on, new `/frontlights` sessions report
`host.known` in one line while this host window stays open; closing it or
restarting the host brings back the `claude rc` guidance.

## After a restart or disconnection

Tell the user to repeat the host command (`claude rc`) in each project folder, as
in step 4. One pilot on one machine, after closing Claude and restarting Windows
for an update, showed all three sessions returning synchronized on phone and
desktop this way; say it is an observation, not a guarantee.

Detect first. `claude remote-control --help` (CLI 2.1.282) documents that `-c`
reattaches to the session last recorded for this folder or one of its worktrees
and fails when nothing was recorded there within roughly the last four hours.
Offer it before a fresh host, describe the window as documented rather than
verified here, and never plan around it. Never reuse an earlier confirmation.

The survival figures above (about ninety seconds locked on battery, about twenty
seconds for hosts started from a scheduled task or background logon) and the
single-dash behaviour are observations from earlier pilots on one machine, not
guarantees. Say so when you cite them.
