#!/usr/bin/env python3
"""Verificações do Frontlights executadas a partir do bloco `checks` do config.

Subcomandos (cada um imprime um objeto JSON em stdout):
  regression  roda a suíte padrão na base e depois na branch, grava um registro por execução em
              <root>/.frontlights/issues/<n>/checks/regression-<base|branch>.json e separa falha
              nova (só na branch, ou mesmo nome com outra causa em `changed_failures`, ou sem nome;
              bloqueia), falha existente (já na base, não bloqueia) e falha corrigida.

Códigos de saída de `regression`: 0 sem falha nova; 1 falha nova; 2 config ou uso recusado, nada
foi executado; 3 falha de infraestrutura (suíte não inicia, timeout, executável ausente, nenhum
teste rodou), que nunca conta como "passou" e sempre bloqueia.

O `argv` do config é sempre uma lista executada sem shell. Shell embutido é recusado (filtro de
erro de configuração, não prova de inocuidade). Falha instável não é reexecutada: `flaky_check` fica
`nao_realizado` quando há falha nova. O resultado
e os registros não trazem a saída bruta da suíte, e o valor de variáveis de ambiente com nome de
segredo é ocultado de tudo que é impresso ou gravado.

Novos subcomandos entram em SUBCOMMANDS com uma função `configure(parser)` e uma função
`run(args)` que devolve (resultado, código de saída).
"""

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile

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
UNITTEST_FAILURE = re.compile(r'^(?:FAIL|ERROR): (.+?)\s*$')
PYTEST_FAILURE = re.compile(r'^(?:FAILED|ERROR) (\S*(?:::|\.py)\S*)')
PYTEST_SUMMARY = re.compile(r'^=*\s*((?:\d+ \w+(?:, )?)+) in [\d.]+s')
UNITTEST_RAN = re.compile(r'^Ran (\d+) tests? in ', re.M)
UNITTEST_FAILED_COUNT = re.compile(r'^FAILED \(([^)]*)\)', re.M)
UNNAMED_FAILURE = '<suite exit code {}>'


class Refused(Exception):
    """Configuração ou uso recusado; nada foi executado."""


def redact(text):
    for name, value in os.environ.items():
        if len(value) >= MIN_SECRET_LENGTH and SECRET_NAME.search(name):
            text = text.replace(value, '[oculto]')
    return text


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
    text = re.sub(r'0x[0-9a-fA-F]+', '0xADDR', text)
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


def regression_settings(config_path):
    try:
        config = json.loads(Path(config_path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise Refused('Não foi possível ler o config como JSON.') from None
    block = (config.get('checks') or {}).get('regression') if isinstance(config, dict) else None
    if not isinstance(block, dict):
        raise Refused('O config não declara checks.regression.')
    argv = block.get('argv')
    if not isinstance(argv, list):
        raise Refused('checks.regression.argv precisa ser uma lista de argumentos, não uma string.')
    if not argv or not all(isinstance(a, str) and a for a in argv):
        raise Refused('checks.regression.argv precisa ter só textos não vazios.')
    reason = embedded_shell(argv)
    if reason:
        raise Refused(f'checks.regression.argv não aceita shell embutido ({reason}; cmd, sh, bash, powershell, '
                      '.bat, wrappers, &&, |, ;, redirecionamentos, python -c); declare o executável e '
                      'seus argumentos.')
    timeout = block.get('timeoutSeconds', DEFAULT_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        raise Refused('checks.regression.timeoutSeconds precisa ser um inteiro positivo.')
    cwd = block.get('cwd')
    if cwd is not None and (not isinstance(cwd, str) or not cwd):
        raise Refused('checks.regression.cwd precisa ser um caminho relativo à worktree.')
    return {'argv': argv, 'cwd': cwd, 'timeout': timeout}


def run_directory(tree, cwd):
    """Diretório de execução dentro de `tree`; recusa cwd absoluto ou que escape da árvore."""
    tree = Path(tree).resolve(strict=True)
    if cwd is None:
        return tree
    target = (tree / cwd).resolve()
    if Path(cwd).is_absolute() or not target.is_relative_to(tree) or not target.is_dir():
        raise Refused('checks.regression.cwd precisa ser um diretório existente dentro da worktree.')
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
    """Falhas na ordem em que aparecem: lista de (nome, impressão da causa ou None)."""
    lines = output.splitlines()
    found = {}
    for index, line in enumerate(lines):
        match = UNITTEST_FAILURE.match(line)
        if match:
            found.setdefault(match.group(1), fingerprint(unittest_cause(lines, index)))
            continue
        match = PYTEST_FAILURE.match(line)
        if match:
            found.setdefault(match.group(1), fingerprint(line.partition(' - ')[2]))
    return list(found.items())


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


def kill_tree(process):
    """Encerra o processo da suíte e todos os descendentes (grupo/sessão próprios)."""
    if os.name == 'nt':
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


def run_process(argv, cwd, timeout):
    """Roda a suíte em grupo/sessão novos, com a saída em arquivo (um neto vivo não prende a leitura).

    Devolve (código de saída, saída). No prazo estourado mata a árvore inteira e levanta TimeoutExpired.
    """
    options = ({'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt'
               else {'start_new_session': True})
    with tempfile.TemporaryFile() as sink:
        process = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=sink,
                                   stderr=subprocess.STDOUT, **options)
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_tree(process)
            process.wait()
            raise
        if os.name != 'nt':
            kill_tree(process)  # sobras de netos depois de uma execução normal
        sink.seek(0)
        return process.returncode, sink.read().decode(errors='replace')


def run_suite(argv, cwd, timeout):
    try:
        returncode, raw = run_process(argv, cwd, timeout)
    except FileNotFoundError:
        return infrastructure('executable_missing', 'Executável da suíte não encontrado.')
    except subprocess.TimeoutExpired:
        return infrastructure('timeout', f'A suíte passou de {timeout} s e foi interrompida.')
    except OSError:
        return infrastructure('cannot_start', 'A suíte não pôde ser iniciada.')
    output = redact(raw)
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
    path.write_text(json.dumps(record, indent=2, ensure_ascii=True), encoding='utf-8')
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
            before, after = base_prints.get(name), branch['failure_fingerprints'].get(name)
            if before and after and before != after:
                new.append(name)
                changed.append(name)
            else:
                existing.append(name)
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
    return result, 3 if problems else 1 if new else 0


SUBCOMMANDS = {'regression': (configure_regression, run_regression)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    for name, (configure, _) in SUBCOMMANDS.items():
        configure(sub.add_parser(name))
    args = parser.parse_args(argv)
    try:
        result, code = SUBCOMMANDS[args.command][1](args)
    except (Refused, OSError) as error:
        result, code = {'ok': False, 'blocking': True, 'error': redact(str(error))}, 2
    print(json.dumps(result, indent=2, ensure_ascii=True))
    return code


if __name__ == '__main__':
    sys.exit(main())
