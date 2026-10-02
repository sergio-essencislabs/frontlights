# App de exemplo do teste de navegador

Um app mínimo, só com a biblioteca padrão do Python, para exercitar o fluxo do teste de
navegador de ponta a ponta: o `serve` sobe o app numa porta reservada e uma spec
Playwright entra com uma conta fictícia.

| Arquivo | Papel |
| --- | --- |
| `app.py` | Servidor HTTP: formulário de login em `/`, `POST /entrar`, `/painel` e `/health`. |
| `config.json` | Config de exemplo com `browserTest.processes` (`port: "auto"`, `{port}` no health). |
| `login.spec.js` | Spec Playwright: login certo abre o painel; senha errada é recusada. |
| `playwright.config.js` | Só Chromium; a URL vem de `FRONTLIGHTS_BASE_URL`. |
| `run_spec.py` | Roda a spec e diz `passou`, `falhou` ou `nao_executado` (com o motivo), em JSON. |

A conta (`usuario1@exemplo.test` com `senha-ficticia`) é fictícia e fixa, igual no app e
no config. Nunca coloque aqui uma conta, senha ou URL real.

## Instalar o Playwright (à mão, uma vez)

O plugin não instala nada disso: o Playwright e o navegador ficam só nesta pasta, na sua
máquina. É preciso Node 20 ou mais novo e rede para o download.

```bash
cd examples/browser-app
npm install
npx playwright install chromium
```

`node_modules/` e `test-results/` ficam fora do Git (`.gitignore` desta pasta).

## Rodar

Da raiz do repositório:

```bash
python scripts/serve.py start --config examples/browser-app/config.json --root . --issue 13
python examples/browser-app/run_spec.py --config examples/browser-app/config.json --base-url http://127.0.0.1:<porta>/
python scripts/serve.py stop --config examples/browser-app/config.json --root . --issue 13
```

A `<porta>` é a `port` do processo `app` na saída do `serve start`.

## Resultado

O `run_spec.py` imprime um objeto JSON e sai com 0 (`passou`), 1 (`falhou`) ou 3
(`nao_executado`). Só `passou` traz `aprovado: true`, e só quando a spec inteira rodou e
passou: um teste pulado (`skip`, `fixme`) deixa o resultado `nao_executado`, um teste marcado
como esperado falhar (`test.fail`) deixa `falhou`, e um `.only` na spec é proibido
(`--forbid-only`). A `--base-url` precisa ser de host local (`127.0.0.1`, `localhost` ou `::1`,
sem `@` nem `\`); outra é recusada como `nao_executado`, antes de qualquer requisição e
sem entregar a conta. Sem Node, sem `@playwright/test`, sem o navegador baixado, com o app fora
do ar (ou sem rede), com tempo esgotado ou sem nenhum teste executado, o resultado também é
`nao_executado` e o `motivo` diz o que faltou: isso nunca conta como aprovado. Login e senha
aparecem como `[redacted]` na saída.

Os testes em `tests/test_browser_app_example.py` sobem o app pelo `serve` e conferem o login
por HTTP sem navegador; a spec real só roda quando o Playwright e o navegador estão
instalados, e é pulada com o motivo quando não estão.
