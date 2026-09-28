# Recuperação e reversão

1. Pare de distribuir novas tarefas; permita que operações seguras em andamento
   salvem seus pontos de retomada. Não encerre processos alheios nem remova a
   worktree de outra sessão.
2. Preserve alterações não salvas em commits, evidências, branches e `.frontlights/`.
   Confira o estado real do Git e a responsabilidade pela worktree antes de retomar.
   Não execute limpeza ou restauração automática.
3. Inicie uma nova sessão do Claude com o mesmo projeto e plugin. Leia a issue,
   a autorização e o documento de passagem de contexto. Consulte novamente o GitHub
   e compare a saída de `resume`.
4. Trate as divergências na sessão coordenadora. Autorização vencida, critérios de
   aceitação alterados, novas permissões ou credenciais ausentes exigem decisão humana.
5. Se uma publicação no GitHub exceder o tempo de espera, confira o conteúdo e os
   links das issues existentes antes de tentar novamente. Registre planos publicados
   parcialmente e atualize os identificadores somente com resultados verificados.
   Não duplique tickets para contornar respostas incertas.
6. Restabeleça o Remote Control na própria sessão atual e obtenha confirmação pelo
   celular ou pela alternativa escolhida. Não presuma que a conexão anterior continua ativa.

Antes de restabelecer o monitoramento, rode a verificação prévia: sem host
detectado, ofereça novamente a configuração ancorada em vez de presumir que a
conexão anterior continua ativa. Onde houver sessão registrada para o diretório
ou uma de suas worktrees, `claude remote-control -c` reancora nela e falha se nada
tiver sido registrado ali nas últimas quatro horas aproximadamente; essa janela é
documentada pelo CLI e ainda não foi exercitada neste piloto.

Para deixar de usar o plugin carregado localmente, encerre a sessão e omita
`--plugin-dir` na próxima inicialização. Se ele for instalado posteriormente por
um catálogo de plugins, use `claude plugin uninstall` com o identificador exato,
após conferir a lista de instalações. Preserve worktrees, registros e issues do
usuário. Este repositório não possui instalador ou desinstalador que altere o GuardianS.

A troca do fluxo padrão e a desativação do GuardianS são decisões humanas separadas,
tomadas após a revisão completa das evidências do piloto. O piloto não as executa.
