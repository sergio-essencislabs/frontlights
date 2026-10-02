# <One observable outcome>

PRD: <approved revision/link>

## Outcome

## Boundaries and non-goals

## Acceptance criteria

- [ ] <observable result>

## Approach and conventions

Chosen approach (rejected alternatives in the PRD or discovery log) and the
project conventions to follow, with example paths. Any approved break of the
pattern is named here with its reason.

## Seams under test

- <public interface> — <acceptance behavior observed there>

Only these seams receive tests. A missing or wrong seam goes back to the user.

## Test expectations

Focused regression, broader integration and actual type-check commands.

## Navegador e testes ligados

- Toca o frontend: <sim | não>
- Conta: <conta 1 | contas 1 e 2>
- Fluxo: <telas e ações a exercitar, com o resultado esperado>
- Testes ligados: <navegador, integração, regressão, permissões entre contas, smoke>; casos extras: <nenhum>

Contas e processos vêm de `browserTest` no `.frontlights/config.json` do projeto,
nunca deste texto: aqui não entram login, senha nem URL.

## Dependencies

Depends on: none

Replace `none` with canonical #issue links after approved publication.

## Ownership and contention

Files/components, generated files, migrations, ports, shared test services.

## Vertical slice check

What works end to end, and how a reviewer can observe it. A prerequisite must
name its working seam, test and dependent consumer.

## Risks and session size

## Evidence checkpoints

Timestamp, branch/PR, exact HEAD/diff hashes, commands/results, independent review,
remaining blockers and next action. GitHub remains canonical; local evidence links
are not separate tickets. Do not close automatically.
