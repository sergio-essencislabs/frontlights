#!/usr/bin/env python3
"""Verificações do Frontlights executadas a partir do bloco `checks` do config.

Subcomandos (cada um imprime um objeto JSON em stdout):
  regression  roda a suíte padrão na base e depois na branch, grava um registro por execução em
              <root>/.frontlights/issues/<n>/checks/regression-<base|branch>.json e separa falha
              nova (só na branch, ou mesmo nome com outra causa em `changed_failures`, ou sem nome;
              bloqueia), falha existente (já na base, não bloqueia) e falha corrigida.

  integration roda o `checks.integration.argv` contra o backend da branch. A URL vem SOMENTE do
              registro do `serve` da issue (<root>/.frontlights/serve/<n>.json), com todos os
              processos vivos, e é entregue na variável FRONTLIGHTS_BACKEND_URL e no texto
              `{backend_url}` de cada elemento do argv. O backend é o processo de
              `browserTest.processes` com o nome de `checks.backend` (sem o campo, o primeiro). A URL
              é a origem (`esquema://host:porta`) da `health` do processo e o host precisa ser
              127.0.0.1, localhost ou ::1; outro host é recusado como infraestrutura (nunca ambiente
              real). Registro: .../checks/integration.json.
  smoke       sobe o serve da issue sozinho quando não há registro ou todos os processos morreram
              (usa o `serve start` com o mesmo --config, --root e --issue), pede cada caminho de
              `checks.smoke.paths` (relativo à origem do processo `checks.smoke.target`, sem o campo
              o primeiro) e confere o status: `checks.smoke.expectStatus` (inteiro ou lista) ou, por
              padrão, 2xx/3xx; redirecionamentos não são seguidos. Registro: .../checks/smoke.json.
              Se o smoke subiu o serve, derruba SOMENTE o que subiu (também quando um caminho falha
              ou o comando é interrompido); processos já vivos, de outra pessoa ou sessão, são usados
              e nunca derrubados. Falha de subida é infraestrutura (kind `serve_start_failed`, erro
              do serve mascarado); se a falha deixou processos vivos, o smoke tenta um `serve stop` e,
              se não conseguir, avisa em `warnings` ("Rode `serve stop`"). Saída e registro trazem
              `serve_iniciado_pelo_smoke` e `serve_encerrado` (também quando uma recusa, código 2,
              acontece depois da subida).
              Antes de subir, o host de cada `health` e da `baseUrl` de `browserTest` precisa ser
              local (senão `non_local_url`, sem nenhuma requisição); o texto `{port}` dessas URLs
              (processo com `port: "auto"`) é trocado por uma porta fictícia só para essa conferência.
              O health da subida ignora o proxy do ambiente.
              Antes de derrubar, o smoke confere que o registro ainda traz os mesmos pids e
              identidades que ele iniciou (outra sessão pode ter feito stop e start: aviso, nada é
              derrubado). Se o retorno do `serve.start` não trouxer pids e identidades (forma inesperada),
              a prova vem do registro: sem registro antes da subida, o que existe logo depois é do smoke, e
              a conferência e o stop seguem normais; com registro prévio (mesmo de processos mortos) ou
              leitura impossível, o smoke não sabe de quem é o serve, não derruba nada, mantém
              `serve_encerrado` false e avisa que o serve pode seguir no ar (conferir e rodar `serve stop`).
              Uma recusa do `serve.stop` por trava ocupada (outro start ou stop da issue em andamento) é
              repetida com pausa (STOP_RETRY_PAUSE) até STOP_RETRY_SECONDS no total, reconferindo o registro
              a cada tentativa; nenhuma outra falha é repetida e, esgotado o prazo, vale o que segue.
              Processo que o smoke subiu e não conseguiu derrubar é órfão: `serve_encerrado` false,
              `warnings` com "Rode `serve stop`" e, se tudo mais passou, infraestrutura `serve_stop_failed`
              (código 3); com caminho falho o código segue 1. Registro parcial (processo declarado
              no config que o `serve start` em andamento ainda não gravou) é `serve_registry_partial`;
              pid fora de 1..2^31-1, não inteiro ou bool é `serve_registry_invalid`. O serve mascara
              o próprio registro (uma senha igual à porta, ao host, ao esquema ou ao nome de um
              processo oculta esse pedaço): só o nome e a URL de fato ocultados são refeitos, a partir do
              processo declarado na mesma posição do config (a URL não ocultada do registro vale mais que o
              config), e a porta do registro vale quando a URL declarada usa `{port}`. Se, havendo algo a
              refazer, os nomes do registro não baterem com os do config nas mesmas posições (config
              alterado depois do `serve start`), é `serve_registry_invalid`. `health` vazio no config é
              erro de configuração (código 2), com a mensagem apontando o campo.
              Limite: um smoke morto à força (kill -9, queda da máquina) não passa pelo `finally` e
              deixa o serve e o registro no ar; o smoke seguinte os usa como serve "de outra sessão"
              e avisa em `warnings` para conferir e rodar `serve stop`.
              A `integration` NÃO sobe nada: exige o `serve start` já feito. Ambos aceitam `--base`
              (hash do diff contra o merge-base) e marcam `simulacao: false`.
              `login` e `password` de `browserTest.users` são ocultados de tudo que é impresso ou
              gravado: o valor bruto e qualquer mistura, caractere a caractere, de literal e codificação
              percent (`%XX` com hex maiúsculo ou minúsculo, `+` para espaço, até duas camadas), o que cobre
              `quote`, `quote_plus` e `encodeURIComponent` com qualquer `safe`. O texto literal respeita a
              caixa; a máscara é uma passada e idempotente; três ou mais camadas não são cobertas. Valor com
              menos de 4 caracteres ou com o texto do marcador faz
              `integration` e `smoke` recusarem o comando (código 2) antes de executar qualquer coisa.

Códigos de saída de `integration` e `smoke`: 0 passou/saudável; 1 falha de produto (argv sai com
código diferente de zero, caminho com status inesperado ou 5xx); 2 config ou uso recusado; 3
infraestrutura (serve sem registro ou com processo morto, host fora do local, conexão recusada,
timeout, executável ausente).

Códigos de saída de `regression`: 0 sem falha nova; 1 falha nova; 2 config ou uso recusado, nada
foi executado; 3 falha de infraestrutura (suíte não inicia, timeout, executável ausente, nenhum
teste rodou), que nunca conta como "passou" e sempre bloqueia.

O `argv` do config é sempre uma lista executada sem shell; texto com byte NUL é recusado (código 2).
Shell embutido é recusado por um filtro de erro de configuração, não por prova de inocuidade. O filtro
olha só o argv: não enxerga o que um script ou executável faz por dentro (`python script.py`, `npm test`
ou uma ferramenta qualquer ainda podem chamar shell), não reconhece shells com nome incomum ou
renomeados, nem wrappers fora da lista (`env`, `xargs`, `nohup`, `busybox`, `wsl`, `sudo`). Os limites
de aplicação estão em docs/security.md. Falha instável não é reexecutada: `flaky_check` fica
`nao_realizado` quando há falha nova. O resultado e os registros não trazem a saída bruta da suíte, e o
valor de variáveis de ambiente com nome de segredo é ocultado de tudo que é impresso ou gravado. A
impressão de cada causa de falha é calculada sobre o texto já mascarado, para que o hash não sirva de
oráculo para adivinhar uma senha curta (a análise das falhas continua sobre o texto bruto).

No timeout a suíte inteira é encerrada: grupo de processos próprio no POSIX e Job Object no Windows
(alcança também netos cujo pai já morreu). Se o Job Object não puder ser criado, a execução segue só
com `taskkill /T` e o resultado traz `warnings` em português.

Novos subcomandos entram em SUBCOMMANDS com uma função `configure(parser)` e uma função
`run(args)` que devolve (resultado, código de saída).
"""

import argparse
import contextlib
import datetime as dt
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

import serve

SHELL_PROGRAMS = {'cmd', 'sh', 'bash', 'zsh', 'dash', 'ksh', 'fish', 'csh', 'tcsh', 'powershell', 'pwsh'}
# Executam outro comando por conta própria: recusados sempre (`sudo` só quando o próximo é shell).
WRAPPER_PROGRAMS = {'env', 'xargs', 'nohup', 'busybox', 'wsl'}
SCRIPT_EXTENSIONS = ('.bat', '.cmd', '.com')
PYTHON_PROGRAM = re.compile(r'^(?:python[\d.]*|pythonw|py)$')
SHELL_OPERATORS = {'&&', '||', '|', ';', '&', '<', '>', '>>', '2>&1', '|&'}
DEFAULT_TIMEOUT = 600
SECRET_NAME = re.compile(r'SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|API_?KEY|PRIVATE|KEY', re.I)
SECRET_OPTION = re.compile(r'^--?[\w-]*(?:password|passwd|pwd|token|secret|credential|key|auth)[\w-]*$', re.I)
CREDENTIAL_OPTION = re.compile(r'^--?(?:u|user|username|login|basic|auth)$', re.I)
MIN_SECRET_LENGTH = 8
MIN_USER_SECRET_LENGTH = 4
MASK = '[oculto]'
USER_SECRETS = []
LOCAL_HOSTS = {'127.0.0.1', 'localhost', '::1'}
# Campos que a máscara nunca altera (como REGISTRY_EXACT no serve): evidência técnica e rótulos fixos gerados
# pelo código (`label`, `alvo`). Nenhum campo com texto vindo do usuário, do config ou da saída de um comando
# (argv, paths, erros, mensagens, avisos, nomes de falha) pode entrar aqui: só texto livre é ocultado.
EVIDENCE_EXACT = ('head', 'diff_sha256', 'diff_sha256_vs_base', 'files_sha256', 'timestamp', 'pid', 'port',
                  'classification', 'kind', 'backend_url', 'base_url', 'label', 'alvo')
PORT_PLACEHOLDER = '{port}'
STAND_IN_PORT = '1'  # porta fictícia (válida) no lugar de `{port}` quando só o host da URL importa
MAX_PID = 2 ** 31 - 1
SMOKE_TIMEOUT = 10
# Trava momentânea de outra sessão (start ou stop em andamento) ao derrubar o serve: o stop espera e repete só
# por essa recusa, dentro de um prazo total; esgotado o prazo, vale o comportamento de falha ao derrubar.
STOP_RETRY_SECONDS = 10
STOP_RETRY_PAUSE = 0.2
LOCK_BUSY = re.compile(r'^(?:Outro start|Não foi possível obter a trava)')
TARGETS = {'integration': 'backend da branch (local)', 'smoke': 'aplicação da branch (local)'}
EXIT_CODES = {'passed': 0, 'healthy': 0, 'product_failure': 1, 'refused': 2, 'infrastructure': 3}
UNITTEST_FAILURE = re.compile(r'^(?:FAIL|ERROR): (.+?)\s*$')
# O id vai até o separador ` - ` da mensagem ou o fim da linha: ids parametrizados podem ter espaço.
PYTEST_FAILURE = re.compile(r'^(?:FAILED|ERROR) (\S*(?:::|\.py).*?)(?: - |\s*$)')
PYTEST_SUMMARY = re.compile(r'^=*\s*((?:\d+ \w+(?:, )?)+) in [\d.]+s')
UNITTEST_RAN = re.compile(r'^Ran (\d+) tests? in ', re.M)
UNITTEST_FAILED_COUNT = re.compile(r'^FAILED \(([^)]*)\)', re.M)
UNNAMED_FAILURE = '<suite exit code {}>'
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_WARNING = ('Não foi possível usar um Job Object do Windows; netos órfãos podem sobreviver a um '
               'timeout (encerramento só por taskkill).')


class Refused(Exception):
    """Configuração ou uso recusado; nada foi executado."""


def secret_pattern(value):
    """Expressão que casa o valor bruto ou qualquer codificação percent dele, em um único passo.

    Cada caractere aceita a forma literal, `%XX` (hexadecimal em maiúsculas ou minúsculas) e a mesma
    sequência codificada de novo (`%25XX`, duas camadas); o espaço aceita também `+` (e `%2B`). A mistura vale
    dentro do mesmo valor, então qualquer `safe` de `quote`/`quote_plus` e o `encodeURIComponent` casam. O
    texto literal respeita a caixa: o valor bruto não ganha variante em minúsculas.
    """
    def any_case(text):
        return ''.join(f'[{c.lower()}{c.upper()}]' if c.isalpha() else c for c in text)

    parts = []
    for char in value:
        encoded = ''.join(f'%{byte:02X}' for byte in char.encode())
        options = [re.escape(char), any_case(encoded), any_case(encoded.replace('%', '%25'))]
        if char == ' ':
            options += [r'\+', '%2[Bb]']
        parts.append('(?:' + '|'.join(options) + ')')
    return ''.join(parts)


def redact(text):
    """Oculta todo segredo conhecido em uma única passada, do mais longo ao mais curto.

    O próprio marcador entra primeiro na expressão e é trocado por ele mesmo: ocultar duas vezes não muda
    nada, mesmo para um segredo que seja pedaço do marcador (`ocul`). Login e senha de teste saem também nas
    formas codificadas (`%40`, `%20`, `+`, `%2f`), como em um caminho ou em uma URL do argv.
    """
    values = {value for name, value in os.environ.items()
              if len(value) >= MIN_SECRET_LENGTH and SECRET_NAME.search(name)}
    patterns = {re.escape(value): len(value) for value in values}
    patterns.update({secret_pattern(value): len(value) for value in USER_SECRETS if value})
    if not patterns:
        return text
    pattern = '|'.join([re.escape(MASK)] + sorted(patterns, key=patterns.get, reverse=True))
    return re.sub(pattern, lambda found: MASK, text)


def scrub(value, skip=EVIDENCE_EXACT):
    """Oculta os VALORES texto de um valor JSON; chaves e estrutura ficam intactas.

    `skip` lista as chaves de evidência (head, hashes, horário, porta, classificação, URLs de origem local)
    cujo valor fica exato: uma senha de teste como `2026` não pode corromper o horário nem o hash do diff.
    """
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [scrub(item, skip) for item in value]
    if isinstance(value, dict):
        return {key: item if key in skip else scrub(item, skip) for key, item in value.items()}
    return value


def load_user_secrets(config_path):
    """Login e senha de browserTest.users entram na máscara de tudo que é impresso ou gravado."""
    USER_SECRETS.clear()
    try:
        users = (read_config(config_path).get('browserTest') or {}).get('users') or []
    except (Refused, AttributeError):
        return
    for user in users if isinstance(users, list) else []:
        for key in ('login', 'password'):
            value = user.get(key) if isinstance(user, dict) else None
            if isinstance(value, str) and len(value) >= MIN_USER_SECRET_LENGTH:
                USER_SECRETS.append(value)


def require_maskable_user_secrets(config_path):
    """Recusa (código 2) login ou senha de teste que a máscara não consegue ocultar com segurança.

    Vale para `integration` e `smoke`: valor com menos de 4 caracteres ou que contenha o texto do marcador
    não pode ser ocultado por inteiro nem sem ambiguidade. A mensagem nunca repete o valor.
    """
    users = read_config(config_path).get('browserTest') or {}
    users = users.get('users') if isinstance(users, dict) else None
    for user in users if isinstance(users, list) else []:
        for key in ('login', 'password'):
            value = user.get(key) if isinstance(user, dict) else None
            if not isinstance(value, str) or not value:
                continue
            if MASK in value:
                raise Refused(f'O {key} de um usuário em browserTest.users contém o texto {MASK}, que colide com '
                              'o marcador de máscara e impediria ocultá-lo por inteiro. Use um valor de teste '
                              'sem esse texto.')
            if len(value) < MIN_USER_SECRET_LENGTH:
                raise Refused(f'O {key} de um usuário em browserTest.users tem menos de {MIN_USER_SECRET_LENGTH} '
                              'caracteres: um segredo curto não pode ser ocultado com segurança na saída e nos '
                              'registros. Use um valor de teste mais longo.')


def redact_argv(argv):
    """argv para registro e saída: valores de opções de segredo, `login:senha` e URL com credencial."""
    shown, hide_next = [], False
    for argument in argv:
        if hide_next:
            shown.append('[oculto]')
            hide_next = False
            continue
        name, equals, value = argument.partition('=')
        if equals and value and SECRET_OPTION.match(name):
            argument = f'{name}=[oculto]'
        elif SECRET_OPTION.match(argument):
            hide_next = True
        elif CREDENTIAL_OPTION.match(argument):
            hide_next = True
        else:
            # Heurística conservadora: pode ocultar também um `modulo:atributo` inofensivo.
            argument = re.sub(r'^([\w.@-]+):([^\s:/\\]+)$', r'\1:[oculto]', argument)
            argument = re.sub(r'(://[^/\s:@]+):[^/\s@]+@', r'\1:[oculto]@', argument)
        shown.append(redact(argument))
    return shown


def normalize_message(text):
    """Impressão estável da causa: sem caminhos de máquina, endereços de memória e números de linha."""
    # Só endereços de memória típicos (`at 0x...` de repr de objeto, ou 12+ dígitos); outros valores hex ficam.
    text = re.sub(r'\bat 0x[0-9a-fA-F]+', 'at 0xADDR', text)
    text = re.sub(r'0x[0-9a-fA-F]{12,}', '0xADDR', text)
    text = re.sub(r'[A-Za-z]:[\\/][^\s\'"]*', '<caminho>', text)
    text = re.sub(r'(?:/[\w.\-]+){2,}', '<caminho>', text)
    text = re.sub(r'\bline \d+', 'line N', text)
    return re.sub(r'\s+', ' ', text).strip()


def fingerprint(message):
    """Impressão da causa sobre o texto JÁ mascarado: o hash nunca depende de uma senha (nem serve de oráculo)."""
    message = normalize_message(redact(message or ''))
    return hashlib.sha256(message.encode()).hexdigest()[:16] if message else None


def program_name(argument):
    name = re.split(r'[/\\]', clean_argument(argument))[-1].lower()
    return name[:-4] if name.endswith('.exe') else name


def clean_argument(argument):
    return argument.strip().strip('"\'').strip()


def embedded_shell(argv):
    """Motivo da recusa quando o argv embute shell; None se parecer um executável direto.

    É uma barreira contra erro de configuração, não prova de inocuidade: `python script.py` ou um
    executável qualquer ainda podem chamar shell por dentro (ver docs/security.md).
    """
    program = program_name(argv[0])
    basename = re.split(r'[/\\]', clean_argument(argv[0]))[-1].lower()
    if program in SHELL_PROGRAMS or basename.endswith(SCRIPT_EXTENSIONS):
        return 'shell ou script de shell como executável'
    if program in WRAPPER_PROGRAMS:
        return 'wrapper que executa outro comando por shell'
    if program == 'sudo' and len(argv) > 1 and program_name(argv[1]) in SHELL_PROGRAMS | WRAPPER_PROGRAMS:
        return 'sudo seguido de shell'
    if any(a.strip() in SHELL_OPERATORS for a in argv):
        return 'operador de shell'
    if PYTHON_PROGRAM.match(program):
        for argument in argv[1:]:
            if argument == '-m' or argument.endswith(('.py', '.pyw')):
                break
            if argument == '-c' or re.fullmatch(r'-[A-Za-z]*c', argument):
                return 'código embutido com -c'
    return None


def read_config(config_path):
    try:
        config = json.loads(Path(config_path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        raise Refused('Não foi possível ler o config como JSON.') from None
    return config if isinstance(config, dict) else {}


def command_settings(config_path, name):
    """argv, cwd e timeout do bloco `checks.<name>`; as mesmas recusas valem para regressão e integração."""
    config = read_config(config_path)
    block = (config.get('checks') or {}).get(name) if isinstance(config.get('checks') or {}, dict) else None
    if not isinstance(block, dict):
        raise Refused(f'O config não declara checks.{name}.')
    argv = block.get('argv')
    if not isinstance(argv, list):
        raise Refused(f'checks.{name}.argv precisa ser uma lista de argumentos, não uma string.')
    if not argv or not all(isinstance(a, str) and a for a in argv):
        raise Refused(f'checks.{name}.argv precisa ter só textos não vazios.')
    if any('\0' in a for a in argv):
        raise Refused(f'checks.{name}.argv não aceita byte NUL em nenhum argumento.')
    reason = embedded_shell(argv)
    if reason:
        raise Refused(f'checks.{name}.argv não aceita shell embutido ({reason}; cmd, sh, bash, powershell, '
                      '.bat, wrappers, &&, |, ;, redirecionamentos, python -c); declare o executável e '
                      'seus argumentos.')
    timeout = block.get('timeoutSeconds', DEFAULT_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        raise Refused(f'checks.{name}.timeoutSeconds precisa ser um inteiro positivo.')
    cwd = block.get('cwd')
    if cwd is not None and (not isinstance(cwd, str) or not cwd):
        raise Refused(f'checks.{name}.cwd precisa ser um caminho relativo à worktree.')
    return {'argv': argv, 'cwd': cwd, 'timeout': timeout}


def regression_settings(config_path):
    return command_settings(config_path, 'regression')


def run_directory(tree, cwd, name='regression'):
    """Diretório de execução dentro de `tree`; recusa cwd absoluto ou que escape da árvore."""
    tree = Path(tree).resolve(strict=True)
    if cwd is None:
        return tree
    target = (tree / cwd).resolve()
    if Path(cwd).is_absolute() or not target.is_relative_to(tree) or not target.is_dir():
        raise Refused(f'checks.{name}.cwd precisa ser um diretório existente dentro da worktree.')
    return target


def unittest_cause(lines, start):
    """Causa de uma falha do unittest: texto do bloco depois dos quadros do traceback."""
    index = start + 1
    if index < len(lines) and lines[index].startswith('-----'):
        index += 1
    cause = []
    while index < len(lines) and not lines[index].startswith(('=====', '-----')) and not UNITTEST_RAN.match(lines[index]):
        line = lines[index]
        if line.strip() and not line[0].isspace() and not line.startswith('Traceback'):
            cause.append(line.strip())
        index += 1
    return ' '.join(cause)


def parse_failures(output):
    """Falhas na ordem em que aparecem: lista de (nome, impressões de causa, ordenadas e sem repetição).

    O mesmo nome pode aparecer mais de uma vez (falha do teste e erro de tearDown); guarda todas as
    causas. Causa não extraível vira texto vazio.
    """
    lines = output.splitlines()
    found = {}
    for index, line in enumerate(lines):
        match = UNITTEST_FAILURE.match(line)
        if match:
            found.setdefault(match.group(1), set()).add(fingerprint(unittest_cause(lines, index)) or '')
            continue
        match = PYTEST_FAILURE.match(line)
        if match:
            found.setdefault(match.group(1), set()).add(fingerprint(line[match.end():]) or '')
    return [(name, sorted(prints)) for name, prints in found.items()]


def count_failures(output, names):
    match = UNITTEST_FAILED_COUNT.search(output)
    if match:
        return sum(int(n) for n in re.findall(r'(?:failures|errors)=(\d+)', match.group(1)))
    for line in reversed(output.splitlines()):
        summary = PYTEST_SUMMARY.match(line.strip())
        if summary:
            return sum(int(n) for n, kind in re.findall(r'(\d+) (\w+)', summary.group(1))
                       if kind in ('failed', 'error', 'errors'))
    return len(names) or None


def count_tests(output):
    match = UNITTEST_RAN.search(output)
    if match:
        return int(match.group(1))
    for line in reversed(output.splitlines()):
        summary = PYTEST_SUMMARY.match(line.strip())
        if summary:
            return sum(int(n) for n in re.findall(r'(\d+) \w+', summary.group(1)))
    return None


def git_bytes(root, *args):
    done = subprocess.run(['git', '-C', str(root), *args], capture_output=True)
    if done.returncode != 0:
        raise Refused(f'A pasta {Path(root).name} não é uma worktree Git legível.')
    return done.stdout


def merge_base(root, base_tree):
    """Merge-base entre o HEAD da branch e o HEAD da base; None se não houver (repositórios sem relação)."""
    try:
        base_head = git_bytes(base_tree, 'rev-parse', 'HEAD').decode().strip()
        return git_bytes(root, 'merge-base', 'HEAD', base_head).decode().strip()
    except Refused:
        return None


def diff_hash(root, *revision):
    diff = git_bytes(root, 'diff', '--binary', '--no-ext-diff', '--no-textconv', *revision, '--', '.', ':!.frontlights')
    return hashlib.sha256(diff).hexdigest()


def tree_evidence(root, ancestor=None):
    """HEAD e hashes da árvore (mesma ideia de git_evidence em frontlights.py, sem importá-lo).

    `diff_sha256` é o diff contra o HEAD (vazio em branch já commitada); `diff_sha256_vs_base` é o diff
    contra o merge-base com a base, que inclui os commits da fatia e as alterações não commitadas.
    """
    root = Path(root).resolve(strict=True)
    head = git_bytes(root, 'rev-parse', 'HEAD').decode().strip()
    files = {}
    for raw in sorted(set(git_bytes(root, 'ls-files', '-z', '--cached', '--others', '--exclude-standard').split(b'\0'))):
        name = os.fsdecode(raw)
        if not name or name.replace('\\', '/').startswith('.frontlights/'):
            continue
        path = root / name
        if path.is_symlink():
            files[name] = hashlib.sha256(os.readlink(path).encode()).hexdigest()
        elif path.is_file():
            files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            files[name] = 'missing-or-submodule'
    return {'head': head, 'diff_sha256': diff_hash(root, 'HEAD'),
            'diff_sha256_vs_base': diff_hash(root, ancestor) if ancestor else None,
            'files_sha256': hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()}


def infrastructure(kind, message):
    return {'classification': 'infrastructure', 'exit_code': None, 'failures': [], 'failure_fingerprints': {},
            'failure_count': None, 'tests_run': None,
            'infrastructure': {'kind': kind, 'message': message}}


def create_job():
    """Job Object do Windows que mata todos os processos dele ao ser fechado; OSError se não puder."""
    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64), ('PerJobUserTimeLimit', ctypes.c_int64),
                    ('LimitFlags', wintypes.DWORD), ('MinimumWorkingSetSize', ctypes.c_size_t),
                    ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', wintypes.DWORD),
                    ('Affinity', ctypes.c_size_t), ('PriorityClass', wintypes.DWORD),
                    ('SchedulingClass', wintypes.DWORD)]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in ('Read', 'Write', 'Other', 'ReadBytes', 'WriteBytes',
                                                         'OtherBytes')]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [('Basic', BasicLimits), ('Io', IoCounters), ('ProcessMemoryLimit', ctypes.c_size_t),
                    ('JobMemoryLimit', ctypes.c_size_t), ('PeakProcessMemoryUsed', ctypes.c_size_t),
                    ('PeakJobMemoryUsed', ctypes.c_size_t)]

    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = ExtendedLimits()
    limits.Basic.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits),
                                            ctypes.sizeof(limits)):
        error = ctypes.WinError(ctypes.get_last_error())
        kernel32.CloseHandle(job)
        raise error
    return job


def job_call(name, argtypes, *args):
    import ctypes
    function = getattr(ctypes.WinDLL('kernel32', use_last_error=True), name)
    function.argtypes = argtypes
    if not function(*args):
        raise ctypes.WinError(ctypes.get_last_error())


def assign_to_job(job, process):
    from ctypes import wintypes
    job_call('AssignProcessToJobObject', [wintypes.HANDLE, wintypes.HANDLE], job, int(process._handle))


def terminate_job(job):
    from ctypes import wintypes
    job_call('TerminateJobObject', [wintypes.HANDLE, wintypes.UINT], job, 1)


def close_job(job):
    from ctypes import wintypes
    job_call('CloseHandle', [wintypes.HANDLE], job)


def kill_tree(process, job=None):
    """Encerra o processo da suíte e todos os descendentes (Job Object no Windows, grupo no POSIX)."""
    if os.name == 'nt':
        if job is not None:
            try:
                terminate_job(job)
            except OSError:
                pass
        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        process.kill()
    except OSError:
        pass


def run_process(argv, cwd, timeout, warnings, env=None):
    """Roda a suíte em grupo/sessão novos, com a saída em arquivo (um neto vivo não prende a leitura).

    Devolve (código de saída, saída). No prazo estourado mata a árvore inteira e levanta TimeoutExpired.
    No Windows a suíte inteira roda dentro de um Job Object; sem ele, `warnings` recebe um aviso.
    """
    options = ({'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt'
               else {'start_new_session': True})
    job = None
    if os.name == 'nt':
        try:
            job = create_job()
        except OSError:
            warnings.append(JOB_WARNING)
    try:
        with tempfile.TemporaryFile() as sink:
            process = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=sink,
                                       stderr=subprocess.STDOUT, env=env, **options)
            if job is not None:
                try:
                    assign_to_job(job, process)
                except OSError:
                    warnings.append(JOB_WARNING)
                    close_job(job)
                    job = None
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                kill_tree(process, job)
                process.wait()
                raise
            if os.name != 'nt':
                kill_tree(process)  # sobras de netos depois de uma execução normal
            sink.seek(0)
            return process.returncode, sink.read().decode(errors='replace')
    finally:
        if job is not None:
            try:
                close_job(job)  # KILL_ON_JOB_CLOSE: nada da suíte sobrevive à execução
            except OSError:
                pass


def run_suite(argv, cwd, timeout):
    warnings = []
    result = run_suite_inner(argv, cwd, timeout, warnings)
    if warnings:
        result['warnings'] = warnings
    return result


def run_suite_inner(argv, cwd, timeout, warnings):
    try:
        returncode, raw = run_process(argv, cwd, timeout, warnings)
    except FileNotFoundError:
        return infrastructure('executable_missing', 'Executável da suíte não encontrado.')
    except subprocess.TimeoutExpired:
        return infrastructure('timeout', f'A suíte passou de {timeout} s e foi interrompida.')
    except OSError:
        return infrastructure('cannot_start', 'A suíte não pôde ser iniciada.')
    output = raw  # o parser lê o texto bruto; a máscara vale só para o que é impresso ou gravado
    parsed = parse_failures(output)
    names = [name for name, _ in parsed]
    tests_run = count_tests(output)
    result = {'exit_code': returncode, 'failures': names, 'failure_fingerprints': dict(parsed),
              'failure_count': count_failures(output, names), 'tests_run': tests_run}
    if tests_run is None and not names:
        result.update(infrastructure('no_result', f'A suíte terminou (código {returncode}) sem relatar nenhum teste.'))
        result['exit_code'] = returncode
    elif tests_run == 0 and not names:
        result.update(infrastructure('no_result', 'A suíte terminou sem executar nenhum teste.'))
        result['exit_code'] = returncode
    else:
        if returncode != 0 and not names:
            result['failures'] = [UNNAMED_FAILURE.format(returncode)]
        result['classification'] = 'product_failure' if result['failures'] else 'passed'
    return result


def execute(label, settings, tree, record_dir, ancestor=None):
    """Roda a suíte em `tree` e grava o registro `regression-<label>.json`."""
    cwd = run_directory(tree, settings['cwd'])
    record = {'label': label, 'argv': redact_argv(settings['argv']), 'timestamp': dt.datetime.now(dt.timezone.utc).isoformat()}
    record.update(tree_evidence(tree, ancestor))
    record.update(run_suite(settings['argv'], cwd, settings['timeout']))
    record_dir.mkdir(parents=True, exist_ok=True)
    path = record_dir / f'regression-{label}.json'
    record['record'] = str(path)
    shown = scrub(record)
    # nomes de falha vêm da saída da suíte e são chaves aqui: única exceção à regra de não mexer em chaves
    shown['failure_fingerprints'] = {redact(name): prints for name, prints in record['failure_fingerprints'].items()}
    path.write_text(json.dumps(shown, indent=2, ensure_ascii=True), encoding='utf-8')
    return record


def configure_regression(parser):
    parser.add_argument('--config', required=True, help='.frontlights/config.json com o bloco checks.regression')
    parser.add_argument('--root', required=True, help='worktree da issue (branch)')
    parser.add_argument('--base', required=True, help='worktree ou checkout da base')
    parser.add_argument('--issue', required=True, type=int)


def compare(base, branch):
    """Separa falha nova, mudada (mesmo nome, outra causa), existente e corrigida."""
    new, changed, existing = [], [], []
    base_prints = base['failure_fingerprints']
    count = lambda r: r['failure_count'] if r['failure_count'] is not None else '?'
    for name in branch['failures']:
        if name.startswith('<suite exit code'):
            new.append(f'{name}: sem nome, não comparável ({count(branch)} falha(s) na branch, {count(base)} na base)')
        elif name not in base['failures']:
            new.append(name)
        else:
            # Compara o conjunto de causas: causa só de um lado, ou extra, também é mudança.
            if set(base_prints.get(name) or ['']) != set(branch['failure_fingerprints'].get(name) or ['']):
                new.append(name)
                changed.append(name)
            else:
                existing.append(name)
    before, after = base['failure_count'], branch['failure_count']
    if not new and branch['failures'] and before is not None and after is not None and after > before:
        entry = f'<contagem maior com os mesmos nomes: {after} falha(s) na branch, {before} na base>'
        new.append(entry)
        changed.append(entry)
    fixed = [n for n in base['failures'] if n not in branch['failures']]
    return new, changed, existing, fixed


def run_regression(args):
    settings = regression_settings(args.config)
    if args.issue <= 0:
        raise Refused('--issue precisa ser um número positivo.')
    root, base_tree = Path(args.root).resolve(strict=True), Path(args.base).resolve(strict=True)
    if root == base_tree or root.is_relative_to(base_tree) or base_tree.is_relative_to(root):
        raise Refused('--base e --root precisam ser diretórios diferentes e um não pode ficar dentro do outro.')
    run_directory(base_tree, settings['cwd'])
    run_directory(root, settings['cwd'])
    record_dir = root / '.frontlights' / 'issues' / str(args.issue) / 'checks'
    ancestor = merge_base(root, base_tree)
    base = execute('base', settings, base_tree, record_dir, ancestor)
    branch = execute('branch', settings, root, record_dir, ancestor)
    problems = [{'side': side, **record['infrastructure']}
                for side, record in (('base', base), ('branch', branch)) if 'infrastructure' in record]
    new = changed = existing = fixed = []
    if not problems:
        new, changed, existing, fixed = compare(base, branch)
    summary = lambda r: {k: r[k] for k in ('head', 'diff_sha256', 'diff_sha256_vs_base', 'classification',
                                           'exit_code', 'tests_run', 'record')}
    result = {'ok': not new and not problems, 'blocking': bool(new or problems), 'argv': redact_argv(settings['argv']),
              'new_failures': new, 'changed_failures': changed, 'existing_failures': existing, 'fixed': fixed,
              'infrastructure': problems, 'base': summary(base), 'branch': summary(branch)}
    if new:
        result['flaky_check'] = 'nao_realizado'
    warnings = list(dict.fromkeys(w for record in (base, branch) for w in record.get('warnings', [])))
    if warnings:
        result['warnings'] = warnings
    return result, 3 if problems else 1 if new else 0


class Infra(Exception):
    """Falha de infraestrutura prevista (serve fora do ar, URL fora do local, conexão): nunca conta como passou."""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind
        self.message = message


def configure_environment_check(parser):
    parser.add_argument('--config', required=True, help='.frontlights/config.json com o bloco checks')
    parser.add_argument('--root', required=True, help='worktree da issue (branch), onde o serve foi iniciado')
    parser.add_argument('--issue', required=True, type=int)
    parser.add_argument('--base', help='worktree ou checkout da base, para o hash do diff contra o merge-base')


def pick_process(processes, name):
    """Processo do serve pelo nome; sem nome, o primeiro de browserTest.processes."""
    if name is None:
        return processes[0]
    for entry in processes:
        if entry.get('name') == name:
            return entry
    raise Refused(f'O processo {name} não está em browserTest.processes do serve desta issue.')


def local_origin(entry, label=None):
    """`esquema://host[:porta]` do processo; só http/https e só a própria máquina.

    `localhost` é entregue como o literal 127.0.0.1, para não depender do resolvedor. `label` descreve a
    origem da URL nas mensagens (por padrão, o processo do registro do serve).
    """
    name = entry.get('name')
    label = label or f'o processo {name} do serve'
    of_label = 'd' + label  # todo rótulo começa com artigo (o, a): a contração sai "do" ou "da"
    url = entry.get('url')
    if not isinstance(url, str) or not url:
        raise Infra('serve_registry_invalid', f'O registro do serve não traz a URL do processo {name}.')
    try:
        # só esquema, host e porta importam: um pedaço mascarado fora deles (senha na query) não estraga a leitura
        parts = urllib.parse.urlsplit(url.replace(serve.MASK, 'x'))
        host = (parts.hostname or '').lower()
        port = f':{parts.port}' if parts.port else ''
    except ValueError:
        raise Infra('invalid_url', f'A URL {of_label} é inválida.') from None
    if parts.scheme not in ('http', 'https'):
        raise Infra('invalid_url', f'A URL {of_label} não usa http nem https.')
    if host not in LOCAL_HOSTS:
        raise Infra('non_local_url', f'{label[0].upper() + label[1:]} declara um host que não é local; '
                    'a integração e o smoke só falam com o backend da própria branch em 127.0.0.1, localhost '
                    'ou ::1, nunca com um ambiente real.')
    host = '127.0.0.1' if host == 'localhost' else host
    shown = f'[{host}]' if ':' in host else host
    return f'{parts.scheme}://{shown}{port}'


def valid_pid(pid):
    """Pid de registro utilizável: inteiro (não bool) entre 1 e MAX_PID; o resto nem chega ao sistema."""
    return isinstance(pid, int) and not isinstance(pid, bool) and 0 < pid <= MAX_PID


def read_status(root, issue):
    """Situação do serve da issue; registro ilegível, malformado ou com pid fora da faixa é infraestrutura.

    Os pids são conferidos ANTES de `serve.status`: um pid enorme ou negativo chegaria ao sistema operacional
    truncado (podendo apontar para outro processo) ou estouraria em ctypes. Qualquer exceção do serve ou da
    leitura vira `serve_registry_invalid`, nunca traceback.
    """
    try:
        for entry in serve.read_registry(root, issue)['processes']:
            if not valid_pid(entry['pid']):
                raise Infra('serve_registry_invalid', f'O registro do serve da issue {issue} traz um pid inválido '
                            f'(esperado um inteiro entre 1 e {MAX_PID}).')
        return serve.status(root, issue)
    except Infra:
        raise
    except serve.Refusal as refusal:
        raise Infra('serve_registry_invalid', serve.protect(str(refusal))) from None
    except Exception:
        raise Infra('serve_registry_invalid', f'O registro do serve da issue {issue} tem campos inesperados.') from None


def masked_authority(url):
    """True se a máscara do serve alcançou o esquema, o host ou a porta da URL do registro."""
    scheme, _, rest = url.partition('://')
    host_port = re.split(r'[/?#]', rest, maxsplit=1)[0].rpartition('@')[2]
    return serve.MASK in scheme or serve.MASK in host_port


def valid_port(port):
    return isinstance(port, int) and not isinstance(port, bool) and 0 < port < 65536


def entry_masked(entry):
    """True se a máscara do serve alcançou o nome ou a autoridade (esquema, host, porta) do processo no registro."""
    url = entry.get('url')
    return serve.MASK in str(entry.get('name')) or (isinstance(url, str) and masked_authority(url))


def name_fits(name, declared_name):
    """O nome do registro (liso ou com trechos ocultados) corresponde ao nome declarado na mesma posição do config."""
    if not isinstance(name, str) or not isinstance(declared_name, str):
        return False
    if serve.MASK not in name:
        return name == declared_name
    pieces = (re.escape(piece) for piece in name.split(serve.MASK))
    return re.fullmatch('.+'.join(pieces), declared_name, re.S) is not None


def empty_health_refusal(name):
    """Recusa de configuração (código 2): o campo `health` do processo está vazio no config."""
    return Refused(f'O campo health do processo {name} em browserTest.processes do config está vazio: declare a '
                   'URL de verificação do processo (por exemplo http://127.0.0.1:PORTA/health).')


def restore_masked(processes, config_path, issue):
    """Refaz nome e URL que a máscara do serve ocultou no registro (senha igual à porta, ao host, ao nome...).

    O serve grava o registro já mascarado e o texto ocultado não volta. Só o que a máscara tocou é refeito, a
    partir do processo declarado na mesma posição do config (o registro segue a ordem do config): o nome
    ocultado volta pelo nome declarado e a autoridade ocultada da URL pelo `health`, com `{port}` trocado pela
    porta do registro. O que a máscara NÃO tocou fica como o serve gravou (a URL do registro vale mais que o
    config). Como a posição é a única pista, quando há algo a refazer todos os nomes do registro precisam bater
    com os do config nas mesmas posições (liso: igual; ocultado: casando com o nome declarado); se divergirem,
    o registro é de outra configuração e vira infraestrutura `serve_registry_invalid`, nunca a URL de outro
    processo. `health` vazio no config é erro de configuração (`Refused`, código 2).
    """
    if not any(entry_masked(entry) for entry in processes):
        return [dict(entry) for entry in processes]
    declared = declared_processes(config_path)
    for index, entry in enumerate(processes):
        if not name_fits(entry.get('name'), declared[index].get('name') if index < len(declared) else None):
            raise Infra('serve_registry_invalid', f'O registro do serve da issue {issue} não corresponde ao config '
                        'atual: os nomes dos processos não batem com a ordem de browserTest.processes (o serve foi '
                        'iniciado com outro config?). Como a máscara de segredos ocultou parte do registro, ele só '
                        'pode ser refeito com o mesmo config; rode `serve stop` e `serve start` de novo.')
    restored = []
    for index, entry in enumerate(processes):
        entry, item = dict(entry), declared[index]
        if serve.MASK in str(entry.get('name')):
            entry['name'] = item['name']
        url = entry.get('url')
        if isinstance(url, str) and masked_authority(url):
            health = item.get('health')
            if not isinstance(health, str):
                raise Infra('serve_registry_invalid', f'O registro do serve da issue {issue} traz nome ou URL '
                            'ocultados pela máscara de segredos (uma senha igual à porta, ao host ou ao nome de um '
                            'processo) e o config não declara o mesmo processo para refazê-los.')
            if not health.strip():
                raise empty_health_refusal(entry['name'])
            port = entry.get('port')
            entry['url'] = health.replace(PORT_PLACEHOLDER, str(port) if valid_port(port) else STAND_IN_PORT)
        restored.append(entry)
    return restored


def read_processes(root, issue, config_path=None):
    """Processos do registro (cada um com `alive`), já conferidos; forma inesperada é infraestrutura.

    Com `config_path`, nome e URL que a máscara do serve ocultou no registro são refeitos pelo config.
    """
    status = read_status(root, issue)
    try:
        processes = status['processes']
        if not processes:
            raise Infra('serve_registry_invalid', f'O registro do serve da issue {issue} não lista processos.')
        for entry in processes:
            entry.get('name'), entry['alive']
    except Infra:
        raise
    except Exception:
        raise Infra('serve_registry_invalid', f'O registro do serve da issue {issue} tem campos inesperados.') from None
    return restore_masked(processes, config_path, issue) if config_path else processes


def declared_processes(config_path):
    """Itens de browserTest.processes no config; lista vazia se o bloco não puder ser lido."""
    try:
        processes = (read_config(config_path).get('browserTest') or {}).get('processes')
    except (Refused, AttributeError):
        return []
    return [item for item in processes if isinstance(item, dict)] if isinstance(processes, list) else []


def declared_names(config_path):
    """Nomes de browserTest.processes no config; lista vazia se o bloco não puder ser lido."""
    return [item.get('name') for item in declared_processes(config_path)]


def locate(root, issue, name, config_path):
    """Origem local do processo `name` do serve da issue, exigindo todos os processos vivos.

    Um nome que o config declara mas o registro ainda não traz é um `serve start` em andamento
    (`serve_registry_partial`), não erro de configuração.
    """
    if not serve.registry_path(root, issue).is_file():
        raise Infra('serve_registry_missing', f'Não há registro do serve para a issue {issue} nesta worktree: '
                    'rode `serve start` antes (scripts/serve.py start --config ... --root ... --issue ...).')
    processes = read_processes(root, issue, config_path)
    if name is not None and name not in [entry.get('name') for entry in processes] \
            and name in declared_names(config_path):
        raise Infra('serve_registry_partial', f'O registro do serve da issue {issue} ainda não traz o processo '
                    f'{name}: há um `serve start` em andamento (ou ele foi interrompido). Espere terminar e rode '
                    'de novo; se nenhum start estiver rodando, rode `serve stop` e `serve start`.')
    origin = local_origin(pick_process(processes, name))
    dead = [str(entry.get('name')) for entry in processes if not entry['alive']]
    if dead:
        raise Infra('serve_down', f'Processo(s) do serve fora do ar: {", ".join(dead)}. Rode `serve start` de novo '
                    '(ou `serve stop` e depois `serve start`) antes da verificação.')
    return origin


def optional_name(value, label):
    if value is not None and (not isinstance(value, str) or not value):
        raise Refused(f'{label} precisa ser o nome de um processo de browserTest.processes.')
    return value


def environment_outcome(args, label, work, extra=None, settle=None):
    """Fluxo comum de integração e smoke: evidência, execução, classificação, registro e código de saída.

    `work()` devolve o resultado da verificação ou levanta Infra. `extra` (preenchido durante o trabalho)
    entra no resultado; se `extra` indica que o smoke subiu o serve, uma recusa (Refused) depois da subida
    também é gravada, com as chaves `serve_*` e os avisos, e sai com o código 2. `settle(record)` ajusta a
    classificação com o que só se sabe depois do encerramento.
    """
    if args.issue <= 0:
        raise Refused('--issue precisa ser um número positivo.')
    root = Path(args.root).resolve(strict=True)
    ancestor = merge_base(root, Path(args.base).resolve(strict=True)) if args.base else None
    record = {'label': label, 'simulacao': False, 'alvo': TARGETS[label],
              'timestamp': dt.datetime.now(dt.timezone.utc).isoformat()}
    record.update(tree_evidence(root, ancestor))
    try:
        record.update(work(root))
    except Infra as problem:
        record.update(infrastructure(problem.kind, problem.message))
    except Refused as refusal:
        if not (extra and extra.get('serve_iniciado_pelo_smoke')):
            raise
        record.update({'classification': 'refused', 'error': str(refusal)})
    if extra:
        record.update(extra)
    if settle:
        settle(record)
    path = root / '.frontlights' / 'issues' / str(args.issue) / 'checks' / f'{label}.json'
    record['record'] = str(path)
    record['ok'] = record['classification'] in ('passed', 'healthy')
    record['blocking'] = not record['ok']
    try:
        write_record(path, record)
    except OSError:
        record['classification'] = 'infrastructure'
        record['infrastructure'] = {'kind': 'record_write_failed',
                                    'message': 'Não foi possível gravar o registro da verificação.'}
        record['ok'], record['blocking'] = False, True
    return record, EXIT_CODES[record['classification']]


def write_record(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(scrub(record), indent=2, ensure_ascii=True), encoding='utf-8')


def run_integration(args):
    require_maskable_user_secrets(args.config)
    settings = command_settings(args.config, 'integration')
    name = optional_name((read_config(args.config).get('checks') or {}).get('backend'), 'checks.backend')

    def work(root):
        cwd = run_directory(root, settings['cwd'], 'integration')
        url = locate(root, args.issue, name, args.config)
        argv = [part.replace('{backend_url}', url) for part in settings['argv']]
        warnings = []
        outcome = {'argv': redact_argv(settings['argv']), 'backend_url': url}
        try:
            code, _ = run_process(argv, cwd, settings['timeout'], warnings,
                                  dict(os.environ, FRONTLIGHTS_BACKEND_URL=url))
        except FileNotFoundError:
            raise Infra('executable_missing', 'Executável da integração não encontrado.')
        except subprocess.TimeoutExpired:
            raise Infra('timeout', f'A integração passou de {settings["timeout"]} s e foi interrompida.')
        except OSError:
            raise Infra('cannot_start', 'A integração não pôde ser iniciada.')
        finally:
            if warnings:
                outcome['warnings'] = warnings
        outcome.update({'exit_code': code, 'classification': 'passed' if code == 0 else 'product_failure'})
        return outcome

    return environment_outcome(args, 'integration', work)


def smoke_settings(config_path):
    config = read_config(config_path)
    block = (config.get('checks') or {}).get('smoke')
    if not isinstance(block, dict):
        raise Refused('O config não declara checks.smoke.')
    paths = block.get('paths')
    if not isinstance(paths, list) or not paths or not all(isinstance(p, str) for p in paths):
        raise Refused('checks.smoke.paths precisa ser uma lista não vazia de caminhos como "/" ou "/health".')
    for path in paths:
        if (not path.startswith('/') or path.startswith('//') or '\\' in path or '://' in path
                or not path.isascii() or any(ord(c) <= 32 or ord(c) == 127 for c in path)):
            raise Refused('checks.smoke.paths só aceita caminhos relativos à URL base, começando com uma barra '
                          '(por exemplo "/health"), só com caracteres ASCII visíveis, sem espaços, "//" inicial '
                          'ou URL completa.')
    expected = block.get('expectStatus')
    codes = [expected] if isinstance(expected, int) and not isinstance(expected, bool) else expected
    if codes is not None and (not isinstance(codes, list) or not codes or not all(
            isinstance(c, int) and not isinstance(c, bool) and 100 <= c <= 599 for c in codes)):
        raise Refused('checks.smoke.expectStatus precisa ser um status HTTP ou uma lista de status HTTP.')
    timeout = block.get('timeoutSeconds', SMOKE_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        raise Refused('checks.smoke.timeoutSeconds precisa ser um inteiro positivo.')
    return {'paths': paths, 'expect': codes, 'timeout': timeout,
            'target': optional_name(block.get('target'), 'checks.smoke.target')}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def fetch_status(url, timeout):
    """Status HTTP da resposta (sem seguir redirecionamentos e sem proxy); Infra se não houver resposta."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
    try:
        with opener.open(url, timeout=timeout) as answer:
            return answer.status
    except urllib.error.HTTPError as error:
        error.close()
        return error.code
    except ValueError:  # inclui UnicodeError: a URL não pôde ser montada
        raise Infra('invalid_url', f'A URL {url} não pôde ser montada para a requisição.') from None
    except (urllib.error.URLError, OSError, http.client.HTTPException) as error:
        reason = error.reason if isinstance(error, urllib.error.URLError) else error
        if isinstance(reason, TimeoutError):
            raise Infra('timeout', f'Sem resposta em {timeout} s de {url}.')
        raise Infra('connection_failed', f'Não foi possível conectar a {url}: o processo do serve não está '
                    'atendendo (conexão recusada ou interrompida).')


def serve_is_up(root, issue):
    """True se há registro com algum processo vivo (do usuário ou de outra sessão); False se o smoke deve subir."""
    if not serve.registry_path(root, issue).is_file():
        return False
    return any(entry['alive'] for entry in read_processes(root, issue))


def require_local_declarations(config_path):
    """Recusa (non_local_url) health ou baseUrl declarados fora da própria máquina, ANTES de subir qualquer coisa.

    O `serve start` faz GET no `health` de cada processo; um host não local receberia requisições do smoke.
    Config sem `browserTest` legível não é assunto daqui: quem reclama é o próprio `serve start`.
    """
    try:
        block = read_config(config_path).get('browserTest')
    except Refused:
        return
    if not isinstance(block, dict):
        return
    processes = block.get('processes')
    for item in processes if isinstance(processes, list) else []:
        if isinstance(item, dict) and isinstance(item.get('health'), str):
            if not item['health'].strip():
                raise empty_health_refusal(item.get('name'))
            # `{port}` (processo com port "auto") ainda não tem valor: a conferência é só do host
            local_origin({'name': item.get('name'), 'url': item['health'].replace(PORT_PLACEHOLDER, STAND_IN_PORT)},
                         f'o health do processo {item.get("name")} em browserTest.processes')
    if isinstance(block.get('baseUrl'), str) and block['baseUrl']:
        local_origin({'name': 'baseUrl', 'url': block['baseUrl'].replace(PORT_PLACEHOLDER, STAND_IN_PORT)},
                     'a baseUrl de browserTest')


def direct_healthy(url):
    """Mesma regra do `serve.healthy` (2xx/3xx, sem seguir redirecionamento), mas sem proxy do ambiente."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
    try:
        with opener.open(url, timeout=2) as answer:
            return 200 <= answer.status < 400
    except urllib.error.HTTPError as error:
        error.close()
        return 300 <= error.code < 400
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException):
        return False


@contextlib.contextmanager
def direct_health():
    """O health do autostart não passa por proxy do ambiente (o `serve.healthy` usa o proxy do processo).

    Troca `serve.healthy` só durante a chamada ao serve (e restaura depois), sem mexer em serve.py e sem
    alterar o ambiente herdado pelos processos que o serve inicia.
    """
    original = serve.healthy
    serve.healthy = direct_healthy
    try:
        yield
    finally:
        serve.healthy = original


def warn(flags, text):
    warnings = flags.setdefault('warnings', [])
    if text not in warnings:
        warnings.append(text)


def stop_succeeded(result):
    """O retorno de `serve.stop` só vale como sucesso com ok verdadeiro e nenhum processo em `left`."""
    return isinstance(result, dict) and result.get('ok') is True and not result.get('left')


def started_pids(started):
    """(pid, identidade) de cada processo que o `serve.start` devolveu; None se a forma não for a esperada."""
    try:
        return {(entry['pid'], entry['identity']) for entry in started['processes']}
    except Exception:
        return None


def registry_pids(root, issue):
    """(pid, identidade) de cada processo do registro da issue agora; None se ele não existe ou não pode ser lido."""
    try:
        return {(entry['pid'], entry.get('identity')) for entry in serve.read_registry(root, issue)['processes']}
    except Exception:
        return None


def serve_has_live_processes(root, issue):
    """True se o registro da issue lista algum processo vivo; sem registro, ou ilegível, não há o que provar."""
    if not serve.registry_path(root, issue).is_file():
        return False
    try:
        return any(entry['alive'] for entry in serve.status(root, issue)['processes'])
    except Exception:
        return False


def clean_failed_start(root, issue, flags):
    """Uma tentativa de `serve stop` depois de uma subida que falhou e deixou processos; falha vira aviso."""
    try:
        if not stop_succeeded(serve.stop(root, issue)):
            raise RuntimeError('o serve devolveu processos sem encerrar')
    except Exception as error:
        warn(flags, f'O serve não subiu e a limpeza dos processos parciais falhou: '
             f'{serve.protect(str(error))} Rode `serve stop` para encerrar o que sobrou.')


def start_serve(config, root, issue, flags, ledger, registry_existed=True):
    """Sobe o serve da issue; falha de subida é infraestrutura e não deixa processos para trás.

    A intenção (`serve_iniciado_pelo_smoke`) é registrada ANTES de chamar o serve: uma interrupção em qualquer
    ponto, inclusive logo depois do retorno, cai no `finally` do smoke, que derruba o que subiu. Se a subida
    falha, o `serve.start` já derruba o que levantou; sobras (`left` na recusa, ou processo vivo no registro
    depois de qualquer outra exceção, já que antes da subida não havia nenhum) levam a uma tentativa de stop.
    Uma recusa sem `left` não mexe no registro: pode ser o de outra sessão (ex.: "já há processos").

    Quando o retorno do `serve.start` tem forma inesperada, ele não traz os pids e identidades a provar. Se NÃO
    havia registro antes da subida (`registry_existed` falso) e o `serve.start` terminou sem recusa, o registro
    que existe logo depois é o dele: o smoke o lê na hora (`registry_pids`) e usa essa leitura como prova, com a
    mesma conferência de antes de derrubar. Se já havia um registro (mesmo de processos mortos), ou se a leitura
    falha, nada prova de quem é o registro: nada é derrubado e o aviso diz isso.
    """
    flags['serve_iniciado_pelo_smoke'] = True
    try:
        with direct_health():
            started = serve.start(config, root, issue)
    except serve.Refusal as refusal:
        flags['serve_iniciado_pelo_smoke'] = False
        if 'left' in refusal.extra:
            clean_failed_start(root, issue, flags)
        raise Infra('serve_start_failed', f'O serve não subiu: {serve.protect(str(refusal))}') from None
    except Exception as error:
        flags['serve_iniciado_pelo_smoke'] = False
        if serve_has_live_processes(root, issue):
            clean_failed_start(root, issue, flags)
        raise Infra('serve_start_failed', f'O serve não subiu: {serve.protect(str(error))}') from None
    ledger['started'] = started_pids(started)
    if ledger['started'] is None and not registry_existed:
        ledger['started'] = registry_pids(root, issue)
    ledger['unproven'] = ledger['started'] is None  # sem pids e identidades não há como provar de quem é o registro


def lock_busy(error):
    """True para a recusa do serve por trava ocupada (outro start ou stop em andamento); nenhuma outra falha."""
    return (isinstance(error, serve.Refusal) and error.category == serve.USAGE and not error.extra.get('left')
            and LOCK_BUSY.match(str(error)) is not None)


def stop_started_serve(root, issue, flags, ledger):
    """Derruba o serve só se o smoke o iniciou e o registro ainda é o dele; falha vira aviso e bloqueio.

    O `serve.stop` é por issue, não por pid: antes de parar, confere que o registro atual traz os mesmos
    pids e identidades que o smoke iniciou. Se outra sessão fez stop e start no meio, não derruba nada e
    avisa. Falha ao derrubar (processos órfãos do próprio smoke) marca `ledger['orphans']`.

    Uma recusa por trava ocupada (outro start ou stop da issue em andamento) é repetida com pausa de
    STOP_RETRY_PAUSE até STOP_RETRY_SECONDS no total, e a conferência do registro roda de novo a cada
    tentativa: se a outra sessão terminou o stop, o registro sumiu e vale o aviso de "outra sessão". O prazo é
    medido DEPOIS de cada tentativa: uma tentativa longa que o estoura não é repetida, então o total é no máximo
    o prazo mais uma tentativa. Uma trava que o serve não consegue usar (E/S) tem o mesmo texto da ocupada e só
    é repetida se a tentativa terminar dentro do prazo; o serve gasta até 20 s por tentativa nesse caso, então
    ela custa uma tentativa, não duas. Nenhuma outra falha é repetida; esgotado o prazo, a recusa vale como
    falha ao derrubar.
    """
    deadline = time.monotonic() + STOP_RETRY_SECONDS
    while stop_attempt(root, issue, flags, ledger, deadline):
        time.sleep(STOP_RETRY_PAUSE)


def stop_attempt(root, issue, flags, ledger, deadline):
    """Uma tentativa de derrubar o serve do smoke; True só se a trava estava ocupada e ainda cabe outra tentativa."""
    if not flags['serve_iniciado_pelo_smoke']:
        return False
    if not serve.registry_path(root, issue).is_file():
        if ledger['started'] is None:  # interrompido durante a subida: o próprio serve já limpou tudo
            flags['serve_iniciado_pelo_smoke'] = False
        else:
            warn(flags, 'O registro do serve desapareceu antes do encerramento (outra sessão fez `serve stop`?): '
                 'o smoke não derrubou nada.')
        return False
    if ledger['unproven']:
        warn(flags, 'O `serve.start` devolveu uma resposta em forma inesperada e já havia um registro antes da '
             'subida (ou ele não pôde ser lido), então o smoke não sabe se o serve atual é o que ele iniciou e '
             'NÃO derrubou nada (serve_encerrado false). Confira o serve atual. Rode `serve stop` se ele for seu: '
             'ele continua no ar.')
        return False
    try:
        if ledger['started'] is not None:
            current = {(entry['pid'], entry.get('identity'))
                       for entry in serve.read_registry(root, issue)['processes']}
            if current != ledger['started']:
                warn(flags, 'O registro do serve da issue mudou depois da subida (outra sessão fez stop e start): '
                     'o smoke NÃO derrubou o serve atual, que não é o que ele iniciou.')
                return False
        if not stop_succeeded(serve.stop(root, issue)):
            raise RuntimeError('o serve devolveu processos sem encerrar.')
        flags['serve_encerrado'] = True
    except Exception as error:
        if lock_busy(error) and time.monotonic() + STOP_RETRY_PAUSE < deadline:
            return True
        ledger['orphans'] = True
        warn(flags, f'O smoke iniciou o serve, mas não conseguiu derrubá-lo: {serve.protect(str(error))} '
             'Rode `serve stop` para encerrar o que sobrou.')
    return False


SHARED_SERVE_WARNING = ('O smoke usou um serve que já estava no ar (do usuário ou de outra sessão) e não o derruba. '
                        'Limite: um smoke anterior encerrado à força (processo morto sem passar pelo encerramento) '
                        'deixa o serve e o registro no ar; se esse serve não for seu, confira e rode `serve stop`.')
ORPHANS_MESSAGE = ('O smoke iniciou o serve e não conseguiu derrubá-lo: podem restar processos órfãos. '
                   'Rode `serve stop` e repita o smoke.')


def run_smoke(args):
    """Sobe o serve se preciso, confere cada caminho e derruba só o que subiu.

    Limite: se o próprio smoke for morto à força (kill -9, queda da máquina), o `finally` não roda e o serve
    que ele subiu e o registro ficam; o smoke seguinte os encontra como serve "de outra sessão", usa e
    avisa em `warnings` para rodar `serve stop`.
    """
    require_maskable_user_secrets(args.config)
    settings = smoke_settings(args.config)
    flags = {'serve_iniciado_pelo_smoke': False, 'serve_encerrado': False}
    ledger = {'started': None, 'unproven': False, 'orphans': False}

    def settle(record):
        if ledger['orphans'] and record['classification'] in ('passed', 'healthy'):
            record['classification'] = 'infrastructure'
            record['infrastructure'] = {'kind': 'serve_stop_failed', 'message': ORPHANS_MESSAGE}

    def work(root):
        try:
            require_local_declarations(args.config)
            registry_existed = serve.registry_path(root, args.issue).is_file()
            if serve_is_up(root, args.issue):
                warn(flags, SHARED_SERVE_WARNING)
            else:
                start_serve(args.config, root, args.issue, flags, ledger, registry_existed)
            origin = locate(root, args.issue, settings['target'], args.config)
            results = []
            for path in settings['paths']:
                status = fetch_status(origin + path, settings['timeout'])
                ok = status in settings['expect'] if settings['expect'] else 200 <= status < 400
                results.append({'path': path, 'status': status, 'ok': ok})
            failed = [r['path'] for r in results if not r['ok']]
            return {'base_url': origin, 'paths': results, 'failures': failed,
                    'classification': 'product_failure' if failed else 'healthy'}
        finally:
            stop_started_serve(root, args.issue, flags, ledger)

    return environment_outcome(args, 'smoke', work, flags, settle)


SUBCOMMANDS = {'regression': (configure_regression, run_regression),
               'integration': (configure_environment_check, run_integration),
               'smoke': (configure_environment_check, run_smoke)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    for name, (configure, _) in SUBCOMMANDS.items():
        configure(sub.add_parser(name))
    args = parser.parse_args(argv)
    load_user_secrets(args.config)
    try:
        result, code = SUBCOMMANDS[args.command][1](args)
    except (Refused, OSError) as error:
        result, code = {'ok': False, 'blocking': True, 'error': str(error)}, 2
    print(json.dumps(scrub(result), indent=2, ensure_ascii=True))  # a única passada de máscara da saída
    return code


if __name__ == '__main__':
    sys.exit(main())
