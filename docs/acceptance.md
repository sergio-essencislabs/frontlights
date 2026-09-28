# Evidências do piloto — 24/09/2026

> O plugin se chamava **Workflows** até a versão 0.6.1 e passou a se chamar
> **Frontlights** na 0.7.0. Os registros abaixo anteriores a essa versão citam os
> nomes da época (`workflows@inline`, `/workflows:total-remote-control`).

Branch de implementação: `codex/workflows-pilot`, no clone de desenvolvimento.
Ambiente: Windows, Python 3.14.6 e Claude Code 2.1.278. Não houve publicação remota,
instalação, alteração do GuardianS, merge ou implantação.

## Verificado localmente

- `python -m unittest discover -s tests -v`: **22 testes passaram**. Inclui repositórios
  Git temporários separados, mudanças nas evidências, divergências na retomada,
  seleção de tarefas conforme dependências e verificações positivas e negativas
  de autorização.
- `python -m compileall -q scripts tests`: passou. Não há verificador de tipos externo
  configurado; esse resultado verifica compilação sintática, não análise estática de tipos.
- `python scripts/frontlights.py validate-plan --plan examples/plan.json`: passou.
- `python scripts/frontlights.py schedule --plan examples/plan.json --limit 2`:
  retornou `[1, 2]`; a issue dependente 3 não foi selecionada.
- `claude plugin validate . --json`: **sucesso**, sem erros ou avisos no manifesto,
  com um aviso de conteúdo: o CLAUDE.md da raiz não é carregado nos projetos consumidores.
  As regras essenciais estão na skill de entrada. Por isso, a validação estrita da
  raiz não passa; o CLAUDE.md curto para colaboradores é mantido intencionalmente.
- `claude --plugin-dir . plugin details workflows`: carregou `workflows@inline`,
  versão 0.1.0, e encontrou seis skills, sem agentes, hooks ou servidores MCP/LSP.

Não interprete `claude plugin validate skills` como validação das skills: a versão
local retornou uma lista de conteúdos vazia para esse caminho. Informar o diretório
de uma skill ou seu SKILL.md também não a validou como skill. A evidência real de
descoberta é o inventário nativo acima; o menu e a execução interativa ainda exigem
verificação em uma sessão real.

Evidência de TDD: a estrutura inicial do módulo e do ponto de retomada produziu erros
de símbolos ausentes, que **não** comprovam uma falha comportamental esperada.
Depois foram reproduzidas duas regressões comportamentais: uma resposta malformada
do GitHub era marcada como `available`, e uma issue em execução era aceita com
dependências pendentes. Os dois testes falharam nas asserções e passaram após a
validação do formato da resposta e das dependências de tarefas em execução.

## Matriz de aceitação

| Verificação dos requisitos | Evidência e situação |
| --- | --- |
| 1. Carregamento e descoberta, com entrada pública única | Carregamento nativo identifica seis skills; uma delas tem entrada pública. Verificação interativa do menu pendente. Comando: `/frontlights`. |
| 2. Planejamento com RoadS e GitHub reais | Utilitário de consulta e testes de fontes indisponíveis implementados. Projeto e RoadS reais configurados não foram fornecidos; teste real pendente. |
| 3. Descoberta, PRD e aprovações | Skills e modelos persistentes implementados. Entrevista real e teste de bloqueio de gravações pendentes. |
| 4. Decomposição vertical | Validador estrutural e avaliação de conversão de tickets por camada em entregas verticais fornecidos. Avaliação real do modelo pendente. |
| 5. Implementação paralela isolada | Planejador testado, incluindo maior lote seguro, reposição de vagas e conflitos. Execução simultânea real de modelos em worktrees não testada. |
| 6. TDD, verificações e revisão independente | Testes locais e evidências de repositórios Git temporários verificados. Sem revisor independente; entrega de produto vinculada a issues pendente. |
| 7. Renovação de contexto | Testes de limites e retomada passaram. Teto acumulado de tokens imposto pelo ambiente e retomada em sessão nova não demonstrados. |
| 8. Celular e permissão pendente | Verificação prévia de energia e host implementada e coberta por testes unitários; `phone_connected` só é gravado por confirmação do usuário. Conexão do celular, sobrevivência a bloqueio longo, tampa fechada e a janela de reancoragem de ~4 h continuam **não testadas**. |
| 9. Controles AFK | Testes positivos e negativos da verificação prévia passaram. Controle obrigatório de ferramentas genéricas indisponível; gravações externas continuam sujeitas à aprovação. Segurança AFK completa NÃO homologada. |
| 10. Documentação, instalação, recuperação e reversão | Carregamento local, configuração, testes, limitações e recuperação documentados. Teste real de desinstalação e convivência pendente. |

O GitHub Actions está configurado para Windows/Linux com Python 3.11/3.14, mas ainda
não executou remotamente. O sucesso local não comprova execução em Linux, outras
versões do Python ou integração contínua remota. A implementação foi verificada
somente pelo autor; não há revisão independente declarada. Ainda não recomende
alterar o fluxo padrão ou desativar o GuardianS.

## Informações e validações pendentes

Use `evals/pilot.md` para a execução real. São necessários um repositório/projeto
descartável escolhido explicitamente, configuração de consulta do RoadS verificada,
aprovação de gravações externas de issues, confirmação do celular na sessão atual
(ou escolha de alternativa local) e um revisor independente. A homologação AFK
completa também exige mecanismos do ambiente que imponham o escopo exato das
operações e renovem o contexto antes do limite. São limitações técnicas; uma
aprovação do usuário, por si só, não as resolve.

## Ensaio local — 25/09/2026 (versão 0.2.0)

Ensaio conduzido pelo app desktop, com o celular acompanhando a sessão, num
repositório Git descartável, sem remoto e sem RoadS. O ensaio foi abandonado
antes do desenvolvimento. Nenhuma gravação externa foi feita.

Verificado: fonte RoadS `unconfigured` e GitHub `unavailable` informados sem
bloquear o fluxo; uma aprovação de publicação recusada deixou de fato de escrever
as issues; a proposta com tickets horizontais foi revisada para fatias verticais
sem pergunta extra; `validate-plan` e `schedule --limit 2` produziram `[1, 2]`.

Achados que motivaram a versão 0.3.0:

- A configuração do celular só foi oferecida na etapa 6, depois da PRD e das issues.
- A PRD e o plano de issues foram aprovados sem que o usuário os tivesse visto na
  íntegra; só um resumo apareceu, fora da pergunta.
- A conversa seguiu em inglês, apesar de o usuário escrever em português.
- As instruções para iniciar o host vieram soltas, sem pergunta com opções.
- O usuário confundiu acompanhar uma sessão pelo celular com ter uma máquina fixa
  no app. `claude -remote-control`, com um traço só, abriu a lista de sessões
  antigas em vez de iniciar o host. Duas confirmações do celular chegaram sem
  nenhum processo de host detectado.
- `inspect` exigia `repository`; `validate-plan` e `checkpoint` não tinham modo local.
- `authorize` só aceitava branches `codex/`.
- `inspect` não detectava verificação Python.
- `git worktree add` falhou por exceder o limite de caminho do Windows, porque o app
  desktop redireciona o diretório de trabalho para um caminho longo.

Correções na 0.3.0: pergunta do celular primeiro; regras de idioma, perguntas
com opções e "mostrar antes de aprovar"; `/workflows:total-remote-control`
público e guiado; oferta do comando `claude --resume` para abrir no CLI a mesma
sessão do desktop; `repository: null`, issues locais com `source: "local"`,
`branch_prefix` (padrão `claude/`), candidatos de verificação Python e
`path_risk`. Os utilitários têm 45 testes automatizados, e uma revisão independente
por outro agente, que não escreveu o código, apontou achados que foram corrigidos
antes do fechamento. O novo fluxo de
conversa e a máquina fixa no celular **ainda não foram testados em sessão real**.
