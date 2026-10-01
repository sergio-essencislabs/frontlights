# Frontlights — piloto para Claude Code

Um plugin independente que conduz **qualquer pedido seu** — uma funcionalidade,
um bug, uma refatoração, uma pesquisa, um script pontual, um documento — por
descoberta do problema, documentos de requisitos (PRDs) aprovados, issues
verticais no GitHub e implementação com testes e autorização delimitada. O
caminho é dimensionado ao pedido: trabalho pequeno é feito direto, sem PRD nem
issue. Observações do RoadS são uma entrada **opcional**, usada quando
configurada; nada exige RoadS para começar. Uma única skill, com um arquivo de
referência por etapa. Não depende do GuardianS nem altera sua instalação.
O plugin se chamava Workflows e foi renomeado para não colidir com os workflows
do próprio Claude Code.

**Situação do piloto:** implementado localmente, com testes automatizados dos
utilitários e validação nativa do pacote. A integração real com RoadS, o
monitoramento pelo celular, a revisão independente e a execução completa sem
supervisão **ainda não foram homologados**. Consulte as
[evidências de aceitação](docs/acceptance.md) antes de depender de execução sem supervisão.

## Idioma

A documentação, os modelos, os exemplos e os conteúdos apresentados ao usuário
são em português. As skills, destinadas ao modelo, são escritas em inglês, mas
orientam a produção de respostas e documentos em português. Comandos, caminhos,
chaves de configuração e identificadores técnicos mantêm sua grafia original.

## Carregar localmente

Requer Claude Code com suporte a plugins, skills e `AskUserQuestion`, Python 3.11+,
Git e, para consultar o GitHub, GitHub CLI autenticado. Não exige pacotes Python adicionais.

Instale pelo marketplace deste repositório:

```powershell
claude plugin marketplace add smendesj/frontlights
claude plugin install frontlights@frontlights
```

Para testar um clone sem instalar, rode no projeto de destino
`claude --plugin-dir <pasta-do-clone>`.

### Invocar com `/frontlights`

O plugin tem uma única skill, oculta do menu (`user-invocable: false`), para não
aparecer como `/frontlights:frontlights`. A entrada é um comando de usuário: copie
`templates/frontlights-command.md` para `~/.claude/commands/frontlights.md`.

```powershell
Copy-Item templates/frontlights-command.md "$HOME/.claude/commands/frontlights.md"
```

Depois, em qualquer sessão:

```text
/frontlights
```

O comando localiza a skill instalada e a segue. A skill define a ordem das etapas
e lê o arquivo de cada etapa em `skills/frontlights/references/` só quando chega
nela. O `CLAUDE.md` da raiz orienta quem trabalha neste repositório; o Claude não
o carrega nos projetos que usam o plugin.

Esse carregamento de desenvolvimento não instala o plugin permanentemente. Para
parar de usá-lo, encerre a sessão e inicie outra sem `--plugin-dir`. Preserve os
pontos de retomada e as cópias de trabalho isoladas do Git (worktrees). Nenhuma
configuração ou rotina automática de interceptação (hook) é instalada.

## Acompanhar pelo celular (máquina fixa no app)

O `/frontlights` começa verificando, só por leitura, se há um host do
Remote Control rodando. Não faz pergunta sobre celular. Sem host, ele apenas
orienta, em passos curtos:

1. Abra um PowerShell, fora do app desktop, e entre na pasta do projeto.
2. Rode `claude rc` nessa pasta. Se pedir para confiar na pasta, aceite. Se não
   pedir e o comando não iniciar, rode `claude` na pasta para confiar, saia com
   `/exit` e rode `claude rc` de novo.
3. Deixe a janela aberta, uma por projeto.
4. No celular, inicie uma sessão pelo dispositivo e escolha o repositório. A
   sessão fica sincronizada entre o celular e o desktop.

```powershell
cd "C:\caminho\do\projeto"
claude rc
```

Fechar a janela tira o dispositivo do ar. Depois de reiniciar o PC ou o Claude,
repita os passos 1 e 2 em cada pasta: as sessões voltam sincronizadas (observado
em um teste em uma máquina, sem garantia).

Para continuar pelo celular com a tampa fechada ou o PC bloqueado, peça a
configuração guiada durante o `/frontlights`. Ela lê os ajustes de energia,
mostra os comandos `powercfg` exatos, com os valores anteriores para desfazer, e
você os roda: o plugin nunca altera o esquema de energia sozinho. Ela também faz
o teste no celular e segue `skills/frontlights/references/remote-control.md`.

## Configurar um projeto

Copie `examples/config.json` para `.frontlights/config.json` no projeto de destino,
defina o repositório exato no formato `proprietario/repositorio` e mantenha
`.frontlights/` ignorado pelo Git desse projeto. Um projeto sem remoto usa
`"repository": null`: o GitHub aparece como `unconfigured`, o plano usa
`url: null` e as issues ficam em `.frontlights/issues/<n>/`, com `source: "local"`.
As branches de trabalho usam o `branch_prefix` da autorização, com padrão `claude/`. Configure o RoadS somente quando o
endereço real de consulta autenticada e o formato da resposta forem conhecidos:

```json
{
  "repository": "PROPRIETARIO/REPOSITORIO",
  "roads": {
    "observations_url": "https://SEU-APLICATIVO.example/api/SUA-ROTA-DE-CONSULTA",
    "token_env": "FRONTLIGHTS_ROADS_TOKEN"
  }
}
```

O endereço acima é ilustrativo; não representa uma API garantida do RoadS.

### Quadro de projeto do GitHub

Quando as issues do repositório são controladas num quadro (GitHub Projects), configure
`project`. Com ele definido, **toda** issue criada pelo Frontlights entra no quadro, com
responsável, tipo e campos preenchidos, e é relida para conferir:

```json
{
  "repository": "PROPRIETARIO/REPOSITORIO",
  "project": {
    "owner": "PROPRIETARIO",
    "number": 1,
    "assignee": "@me",
    "issue_type": "Task",
    "fields": {"Status": "Open", "Area": null}
  }
}
```

- `owner` e `number` identificam o quadro (`github.com/orgs/<owner>/projects/<number>`).
- `fields` associa cada campo do quadro a um valor padrão; `null` obriga a escolher o valor
  de cada issue no plano aprovado (`project_fields` da issue no `plan.json`).
- `issue_type` é o tipo nativo da issue (`gh issue edit --type`); uma issue do plano pode
  trocá-lo com `issue_type`. `assignee` usa `@me` para quem está autenticado no `gh`.
- `python scripts/frontlights.py validate-plan --plan plan.json --config .frontlights/config.json`
  recusa o plano enquanto faltar valor para algum campo.
- O token do `gh` precisa do escopo `project` (`gh auth refresh -s project`).

Sem `project`, ou com `"project": null`, as issues não entram em quadro nenhum; num
repositório de organização, o Frontlights pergunta qual quadro usar antes de publicar.
O utilitário envia somente GET, obtém o token da variável de ambiente indicada,
recusa redirecionamentos e não confirma nem consome filas. Verifique se o endereço
real permite apenas leitura. Nenhum endereço ou credencial do RoadS foi presumido
a partir de outro plugin. A consulta ao quadro e ao planejamento de entregas exige
ferramentas configuradas separadamente; o utilitário consulta issues do GitHub e
uma rota JSON de observações. Integrações ausentes são informadas explicitamente.

### Sincronizar sprint e roadmap com o RoadS

Toda sessão `/frontlights` começa perguntando se a sprint e o roadmap devem ser atualizados
de acordo com o RoadS. Com a resposta "sim", o Frontlights:

1. busca as mudanças que um Scrum Master fez no Roadmap do RoadS;
2. escreve a prosa em cópias temporárias do `ROADMAP.md` do ano e do `SPRINT_*.md` da semana;
3. mostra o diff para aprovação;
4. grava, com backup ao lado de cada arquivo;
5. confere as marcas ocultas de cada mudança;
6. só então confirma ao RoadS.

Quem escreve os arquivos é sempre esta máquina: o RoadS não alcança a pasta onde eles ficam.

Configure o bloco `roadmapSync` no `.frontlights/config.json` do projeto (veja
`examples/config.json`):

- `endpoint` é a base `/api/frontlights` do RoadS, que responde `GET pending-changes` e
  `POST ack`;
- `secretEnvVar` nomeia a variável do segredo, que precisa começar por `FRONTLIGHTS_`. Defina o
  valor no seu próprio terminal, com
  `setx FRONTLIGHTS_API_SECRET "<valor emitido pelo RoadS>"`, e reinicie o Claude;
- `scrumRoot` é a pasta onde ficam os arquivos, e aceita `%USERPROFILE%`;
- `roadmapFile`, `weekFolderPattern` e `sprintFilePattern` montam os caminhos. `{yyyy}` vem da
  segunda-feira da sprint. Em cada padrão, o primeiro `{dd_MM}` é a segunda e o seguinte, a sexta;
- `maxSprintItems` limita a sprint a 4 itens;
- `issueTargets` diz, para cada produto do RoadS, o repositório e o quadro onde a issue nasce.
  Produto sem destino não gera issue.

Na primeira vez, o envio do segredo para aquele endereço precisa da sua aprovação. O estado da
sincronização (aprovação, marca, plano e cópias temporárias) fica em `.frontlights/roadmap-sync/`,
fora do Git. O utilitário é `python scripts/roadmap_sync.py status|approve|fetch|apply|ack|rotate-markers --root <projeto>`.

### Resumo para a diretoria (opcional)

Se o projeto tiver o bloco `roadmapSync.progress` habilitado, logo depois da pergunta do roadmap
(inclusive quando não havia nada pendente, ou quando você respondeu "não") o `/frontlights`
pergunta: "Atualizar também o Resumo para a diretoria no RoadS?". Sem o bloco, nada muda e a
pergunta não aparece. Com a resposta "sim", o Frontlights:

1. pergunta ao RoadS qual período coletar;
2. roda os coletores que o próprio projeto configurou e mostra a tabela de conferência dos números;
3. só segue depois da sua confirmação desses números;
4. redige o arquivo de textos em linguagem simples, seguindo o guia que o projeto indica
   (`draftGuide`) e a partir dos fatos coletados, dos registros locais e, quando a sincronização do
   roadmap está configurada, do roadmap e da sprint da semana (que alimentam os próximos passos); o
   próprio projeto monta o rascunho final com fatos, uso, dados de acesso locais e prints;
5. pergunta se o resumo leva prints e, para cada entrega escolhida, oferece capturar a tela do produto
   rodando neste computador (só dados de teste, nunca dados reais nem produção, sem a barra do
   navegador nem a identidade de quem está logado; JPEG ou PNG, até 256 KB e cerca de 1280 px de
   largura, no máximo 10), grava as imagens em `shotsDir` e reescreve o `captions.json` a cada
   execução (imagens que não estão nele são ignoradas);
6. mostra o rascunho completo, com cada print e sua legenda, e, com a sua aprovação, envia ao RoadS
   com `push --draft <arquivo>`; o comando do projeto escolhe os prints em `shotsDir` e monta a
   linha de acesso do e-mail a partir dos arquivos locais do projeto, nunca da conversa, e a skill
   informa o link para revisar.

Revisar, editar, conferir os números, copiar para o e-mail e marcar como enviado acontece no
RoadS. O Frontlights não envia e-mail. Reenviar o rascunho atualiza o conteúdo coletado e mantém as
edições feitas no RoadS.

O bloco fica dentro de `roadmapSync` (veja `examples/config.json`) e reaproveita `endpoint` e
`secretEnvVar`:

- `enabled` liga o passo; `path` é a rota sob o `endpoint` (só segmentos simples, nunca `..`);
- `collectors` é a lista de comandos, cada um com `name`, `command` (lista de argumentos, nunca uma
  linha de shell; `{from}` e `{to}` viram `AAAA-MM-DD`) e `timeoutSeconds` (padrão 300);
- `pushCommand` é o comando de envio (lista de argumentos; `{draft}` vira o caminho do rascunho),
  com `pushTimeoutSeconds` opcional;
- `factsFile` e `usageFile` são opcionais: apontam os arquivos que os coletores gravam, para a
  skill ler;
- `draftGuide` e `shotsDir` são opcionais e devem ser caminhos relativos dentro do projeto (sem caminho
  absoluto, letra de unidade, `..` nem `~` no início; até 200 caracteres): `draftGuide` é o guia em
  que o projeto descreve o arquivo de textos, e `shotsDir` é a pasta dos prints e do `captions.json`.
  Os dois entram na aprovação do bloco: mudar qualquer um pede nova aprovação.

Como esse bloco faz o plugin executar comandos lidos de um arquivo de configuração, nada roda e
nenhuma chamada é feita antes de você aprovar o bloco exato (endereço, variável, rota e cada
comando). Qualquer mudança nele pede nova aprovação. Os coletores não recebem o segredo; só o
comando de envio o recebe, pela variável de ambiente. Este repositório não traz coletor nenhum. O
utilitário é `python scripts/progress_report.py status|approve|window|collect|push --root <projeto>`.

## Fluxo de trabalho e utilitários

Verificação do host do Remote Control (sem pergunta; orienta `claude rc` quando falta)
e de atualização do plugin (sem pergunta; mostra os dois comandos de atualização
quando há versão nova no GitHub) →
pedido e dimensionamento → inspeção → entrevista de decisões → PRD mostrado na
íntegra e aprovado →
plano de issues verticais aprovado e publicado → autorização delimitada de
implementação → desenvolvimento orientado a testes (TDD), revisão e evidências.
Uma issue vertical entrega um resultado observável de ponta a ponta, incluindo as
camadas necessárias, em vez de separar tickets apenas por banco de dados, API ou tela.
Na entrevista, depois de confirmar o problema (numa só pergunta quando a issue já
o define), a skill levanta as convenções do código afetado e propõe ao menos três
abordagens de implementação realmente diferentes. Cada uma informa se segue ou
quebra o padrão do projeto e o custo disso. Em seguida, a skill percorre os ramos
de decisão da abordagem escolhida, e é o usuário quem decide quando parar. O
padrão existente é o preferido; ele só é quebrado quando for comprovadamente pior
para o caso.
A mesma pergunta que define a profundidade oferece o modo aprendizado, desligado
por padrão, para quem ainda está aprendendo a programar e quer entender as
decisões enquanto as toma. Ligado, antes de cada decisão técnica a skill explica
em texto o conceito em linguagem simples, por que o projeto faz assim, um trecho
comentado do próprio código e o que cada opção muda, e fecha cada rodada com um
"Ficou claro?". Desligado, a última opção de cada pergunta técnica é "Explicar
antes de decidir". Depois de duas reexplicações da mesma rodada, aparece "Seguir
a recomendação": a decisão fica marcada como "a revisar" e entra no PRD como
suposição, não como decisão aprovada. O modo pode ser ligado ou desligado a
qualquer momento, e os conceitos explicados ficam registrados no `discovery.md`.
Toda pergunta ao usuário usa `AskUserQuestion`, sempre com opções. A conversa
segue no idioma do usuário. Nada é aprovado sem que o texto completo tenha
sido mostrado antes. O GitHub é a fonte oficial;
os arquivos Markdown e JSON locais são registros e pontos de retomada, não um
segundo sistema de tickets.

Execute estes comandos no diretório do plugin:

```powershell
python scripts/frontlights.py validate-plan --plan examples/plan.json
python scripts/frontlights.py schedule --plan examples/plan.json --limit 2
python scripts/frontlights.py context --used 85000 --reserve 15000
python scripts/frontlights.py monitoring --root .
python scripts/frontlights.py update-check
python -m unittest discover -s tests -v
python -m compileall -q scripts tests
claude plugin validate . --json
claude --plugin-dir . plugin details frontlights
```

O exemplo seleciona `[1, 2]`; a issue 3 depende da issue 1. O planejador escolhe o
maior conjunto seguro de issues prontas, respeita o trabalho em andamento e verifica
sobreposição de caminhos sem distinguir maiúsculas de minúsculas, para compatibilidade
com Windows. Ele recomenda um lote; a skill distribui o trabalho entre os agentes
ou sessões disponíveis do Claude, respeitando a autorização e a capacidade reais.
A validação estrutural não determina se o texto descreve uma entrega realmente
vertical. Isso exige a revisão de conteúdo da skill e a avaliação fornecida.

Leia o [protocolo](docs/protocol.md), os [limites de segurança](docs/security.md),
as [verificações manuais do piloto](evals/pilot.md) e a [recuperação](docs/recovery.md).

## Fontes da documentação

O formato e os comandos seguem as referências oficiais de
[plugins do Claude Code](https://code.claude.com/docs/en/plugins) e
[skills](https://code.claude.com/docs/en/skills). O monitoramento segue a documentação
de [Remote Control](https://code.claude.com/docs/en/remote-control). O desenho de
permissões foi conferido na [referência de hooks](https://code.claude.com/docs/en/hooks).
Essas fontes externas estão em inglês. Consulta realizada em 24/09/2026; versão da
CLI testada localmente: 2.1.278. A validação nativa avisa que o CLAUDE.md da raiz não
é carregado nos projetos consumidores; isso é tratado pela skill principal.
