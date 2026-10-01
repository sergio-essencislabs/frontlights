#!/usr/bin/env python3
"""Verificações do Frontlights executadas a partir do bloco `checks` do config.

Subcomandos (cada um imprime um objeto JSON em stdout):
  regression  roda a suíte padrão na base e depois na branch, grava um registro por execução em
              <root>/.frontlights/issues/<n>/checks/regression-<base|branch>.json e separa falha
              nova (só na branch, ou mesmo nome com outra causa em `changed_failures`, ou sem nome;
              bloqueia), falha existente (já na base, não bloqueia) e falha corrigida.

  integration roda o `checks.integration.argv` contra o backend da branch. A URL vem SOMENTE do registro
              do `serve` da issue (<root>/.frontlights/serve/<n>.json), com todos os processos vivos, e
              é entregue na variável FRONTLIGHTS_BACKEND_URL e no texto `{backend_url}` de cada elemento
              do argv. O backend é o processo de `browserTest.processes` com o nome de `checks.backend`
              (sem o campo, o primeiro). A URL é a origem (`esquema://host:porta`) da `health` do
              processo e o host precisa ser 127.0.0.1, localhost ou ::1; outro host é recusado como
              infraestrutura (nunca ambiente real). Registro: .../checks/integration.json.
  smoke       sobe o serve da issue sozinho quando não há registro ou todos os processos morreram (usa o
              `serve start` com o mesmo --config, --root e --issue), pede cada caminho de `checks.smoke.paths` (relativo à origem do processo `checks.smoke.target`,
              sem o campo o primeiro) e confere o status: `checks.smoke.expectStatus` (inteiro ou lista)
              ou, por padrão, 2xx/3xx; redirecionamentos não são seguidos. Registro: .../checks/smoke.json.
              Se o smoke subiu o serve, derruba SOMENTE o que subiu (também quando um caminho falha ou o comando
              é interrompido); processos já vivos, de outra pessoa ou sessão, são usados e nunca derrubados.
              Falha de subida é infraestrutura (kind `serve_start_failed`, erro do serve mascarado). Saída e
              registro trazem `serve_iniciado_pelo_smoke` e `serve_encerrado`. A `integration` NÃO sobe nada:
              exige o `serve start` já feito. Ambos aceitam `--base` (hash do diff contra o merge-base) e marcam `simulacao: false`.
              `login` e `password` de `browserTest.users` são ocultados (só valores, uma passada, idempotente)
              de tudo que é impresso ou gravado; valor com menos de 4 caracteres ou com o texto do marcador
              faz `integration` e `smoke` recusarem o comando (código 2) antes de executar qualquer coisa.

Códigos de saída de `integration` e `smoke`: 0 passou/saudável; 1 falha de produto (argv sai com código
diferente de zero, caminho com status inesperado ou 5xx); 2 config ou uso recusado; 3 infraestrutura (serve
sem registro ou com processo morto, host fora do local, conexão recusada, timeout, executável ausente).

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
valor de variáveis de ambiente com nome de segredo é ocultado de tudo que é impresso ou gravado.

No timeout a suíte inteira é encerrada: grupo de processos próprio no POSIX e Job Object no Windows
(alcança também netos cujo pai já morreu). Se o Job Object não puder ser criado, a execução segue só
com `taskkill /T` e o resultado traz `warnings` em português.

Novos subcomandos entram em SUBCOMMANDS com uma função `configure(parser)` e uma função
`run(args)` que devolve (resultado, código de saída).
"""

import argparse
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
# Registro malformado (campo ausente, tipo errado, URL ilegível): infraestrutura, nunca traceback.
MALFORMED = (KeyError, AttributeError, TypeError, ValueError, UnicodeError, IndexError)
SMOKE_TIMEOUT = 10
TARGETS = {'integration': 'backend da branch (local)', 'smoke': 'aplicação da branch (local)'}
EXIT_CODES = {'passed': 0, 'healthy': 0, 'product_failure': 1, 'infrastructure': 3}
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


def redact(text):
    """Oculta todo segredo conhecido em uma única passada, do mais longo ao mais curto.

    O próprio marcador entra primeiro na expressão e é trocado por ele mesmo: ocultar duas vezes não muda
    nada, mesmo para um segredo que seja pedaço do marcador (`ocul`).
    """
    values = {value for name, value in os.environ.items()
              if len(value) >= MIN_SECRET_LENGTH and SECRET_NAME.search(name)}
    values.update(value for value in USER_SECRETS if value)
    if not values:
        return text
    pattern = '|'.join([re.escape(MASK)] + [re.escape(value) for value in sorted(values, key=len, reverse=True)])
    return re.sub(pattern, lambda found: MASK, text)


def scrub(value):
    """Oculta os VALORES texto de um valor JSON; chaves e estrutura ficam intactas."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [scrub(item) for item in value]
    if isinstance(value, dict):
        return {key: scrub(item) for key, item in value.items()}
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
    message = normalize_message(message or '')
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


def local_origin(entry):
    """`esquema://host[:porta]` do processo; só http/https e só a própria máquina.

    `localhost` é entregue como o literal 127.0.0.1, para não depender do resolvedor.
    """
    name = entry.get('name')
    url = entry.get('url')
    if not isinstance(url, str) or not url:
        raise Infra('serve_registry_invalid', f'O registro do serve não traz a URL do processo {name}.')
    try:
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or '').lower()
        port = f':{parts.port}' if parts.port else ''
    except ValueError:
        raise Infra('invalid_url', f'A URL do processo {name} no registro do serve é inválida.') from None
    if parts.scheme not in ('http', 'https'):
        raise Infra('invalid_url', f'A URL do processo {name} no registro do serve não usa http nem https.')
    if host not in LOCAL_HOSTS:
        raise Infra('non_local_url', f'O processo {name} do serve declara um host que não é local; '
                    'a integração e o smoke só falam com o backend da própria branch em 127.0.0.1, localhost '
                    'ou ::1, nunca com um ambiente real.')
    host = '127.0.0.1' if host == 'localhost' else host
    shown = f'[{host}]' if ':' in host else host
    return f'{parts.scheme}://{shown}{port}'


def read_status(root, issue):
    """Situação do serve da issue; registro ilegível ou malformado é infraestrutura."""
    try:
        return serve.status(root, issue)
    except serve.Refusal as refusal:
        raise Infra('serve_registry_invalid', serve.protect(str(refusal))) from None
    except MALFORMED:
        raise Infra('serve_registry_invalid', f'O registro do serve da issue {issue} tem campos inesperados.') from None


def locate(root, issue, name):
    """Origem local do processo `name` do serve da issue, exigindo todos os processos vivos."""
    if not serve.registry_path(root, issue).is_file():
        raise Infra('serve_registry_missing', f'Não há registro do serve para a issue {issue} nesta worktree: '
                    'rode `serve start` antes (scripts/serve.py start --config ... --root ... --issue ...).')
    status = read_status(root, issue)
    try:
        if not status['processes']:
            raise Infra('serve_registry_invalid', f'O registro do serve da issue {issue} não lista processos.')
        origin = local_origin(pick_process(status['processes'], name))
        dead = [str(entry.get('name')) for entry in status['processes'] if not entry['alive']]
    except MALFORMED:
        raise Infra('serve_registry_invalid', f'O registro do serve da issue {issue} tem campos inesperados.') from None
    if dead:
        raise Infra('serve_down', f'Processo(s) do serve fora do ar: {", ".join(dead)}. Rode `serve start` de novo '
                    '(ou `serve stop` e depois `serve start`) antes da verificação.')
    return origin


def optional_name(value, label):
    if value is not None and (not isinstance(value, str) or not value):
        raise Refused(f'{label} precisa ser o nome de um processo de browserTest.processes.')
    return value


def environment_outcome(args, label, work, extra=None):
    """Fluxo comum de integração e smoke: evidência, execução, classificação, registro e código de saída.

    `work()` devolve o resultado da verificação ou levanta Infra.
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
    if extra:
        record.update(extra)
    path = root / '.frontlights' / 'issues' / str(args.issue) / 'checks' / f'{label}.json'
    record['record'] = str(path)
    record['ok'] = record['classification'] in ('passed', 'healthy')
    record['blocking'] = not record['ok']
    write_record(path, record)
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
        url = locate(root, args.issue, name)
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
    processes = read_status(root, issue)['processes']
    return not processes or any(entry['alive'] for entry in processes)


def start_serve(config, root, issue, flags):
    """Sobe o serve da issue; falha de subida é infraestrutura e não deixa processos para trás."""
    try:
        serve.start(config, root, issue)
    except serve.Refusal as refusal:
        if 'left' in refusal.extra:  # a subida falhou e a limpeza do serve não derrubou tudo: tenta uma vez mais
            try:
                serve.stop(root, issue)
            except (serve.Refusal, OSError, *MALFORMED):
                pass
        raise Infra('serve_start_failed', f'O serve não subiu: {serve.protect(str(refusal))}') from None
    except OSError as error:
        raise Infra('serve_start_failed', f'O serve não subiu: {serve.protect(str(error))}') from None
    flags['serve_iniciado_pelo_smoke'] = True


def stop_started_serve(root, issue, flags):
    """Derruba o serve só se o smoke o iniciou; falha ao derrubar vira aviso no resultado."""
    if not flags['serve_iniciado_pelo_smoke']:
        return
    try:
        serve.stop(root, issue)
        flags['serve_encerrado'] = True
    except (serve.Refusal, OSError, *MALFORMED) as error:
        flags.setdefault('warnings', []).append(
            f'O smoke iniciou o serve, mas não conseguiu derrubá-lo: {serve.protect(str(error))} '
            'Rode `serve stop` para encerrar o que sobrou.')


def run_smoke(args):
    require_maskable_user_secrets(args.config)
    settings = smoke_settings(args.config)
    flags = {'serve_iniciado_pelo_smoke': False, 'serve_encerrado': False}

    def work(root):
        try:
            if not serve_is_up(root, args.issue):
                start_serve(args.config, root, args.issue, flags)
            origin = locate(root, args.issue, settings['target'])
            results = []
            for path in settings['paths']:
                status = fetch_status(origin + path, settings['timeout'])
                ok = status in settings['expect'] if settings['expect'] else 200 <= status < 400
                results.append({'path': path, 'status': status, 'ok': ok})
            failed = [r['path'] for r in results if not r['ok']]
            return {'base_url': origin, 'paths': results, 'failures': failed,
                    'classification': 'product_failure' if failed else 'healthy'}
        finally:
            stop_started_serve(root, args.issue, flags)

    return environment_outcome(args, 'smoke', work, flags)


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
