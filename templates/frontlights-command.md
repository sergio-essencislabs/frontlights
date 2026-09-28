---
description: Inicia a sessão Frontlights
---

Você vai conduzir esta sessão com as instruções da skill única do plugin
**frontlights**. Essa skill tem `disable-model-invocation: true` e
`user-invocable: false`, então você **não** consegue chamá-la pela ferramenta
Skill — é preciso ler o arquivo dela e segui-lo.

Localize `skills/frontlights/SKILL.md` do plugin frontlights. Procure com Glob,
nesta ordem, e pare no primeiro que existir:

1. `**/skills/frontlights/SKILL.md` dentro de um diretório passado em
   `--plugin-dir` nesta sessão, se houver;
2. `~/.claude/plugins/cache/frontlights/frontlights/*/skills/frontlights/SKILL.md`,
   escolhendo a versão mais alta;
3. `~/.claude/plugins/**/skills/frontlights/SKILL.md`.

Ignore qualquer resultado sob `.trash/`.

Leia o arquivo **inteiro** e siga-o como as suas instruções para esta sessão,
exatamente como se ele tivesse sido invocado como skill, incluindo a etapa 0 e a
regra de fazer toda pergunta por `AskUserQuestion`. Os arquivos de cada etapa
ficam em `references/`, ao lado desse arquivo; leia cada um só ao entrar na etapa
correspondente. `${CLAUDE_PLUGIN_ROOT}` nas instruções é a pasta dois níveis
acima do `SKILL.md` encontrado.

Se nenhum dos caminhos existir, não invente um caminho nem tente reconstruir o
fluxo de memória: diga que o plugin frontlights não foi encontrado, mostre onde
procurou e peça que o usuário instale o plugin ou informe o diretório.

O pedido do usuário, se houver, segue abaixo. Trate-o como o ponto de partida e a
autoridade sobre o escopo:

$ARGUMENTS
