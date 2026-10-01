#!/usr/bin/env python3
"""Verificações do Frontlights executadas a partir do bloco `checks` do config.

Subcomandos (cada um imprime um objeto JSON em stdout):
  regression  roda a suíte padrão na base e depois na branch, grava um registro por execução em
              <root>/.frontlights/issues/<n>/checks/regression-<base|branch>.json e separa falha
              nova (só na branch, bloqueia), falha existente (já na base, não bloqueia) e falha
              corrigida (falhava na base e passa agora).

Códigos de saída de `regression`: 0 sem falha nova; 1 falha nova; 2 config ou uso recusado, nada
foi executado; 3 falha de infraestrutura (suíte não inicia, timeout, executável ausente, nenhum
teste rodou), que nunca conta como "passou" e sempre bloqueia.

O `argv` do config é sempre uma lista executada sem shell. Shell embutido é recusado. O resultado
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
import subprocess
import sys

SHELL_PROGRAMS = {'cmd', 'sh', 'bash', 'zsh', 'dash', 'ksh', 'fish', 'csh', 'tcsh', 'powershell', 'pwsh'}
SHELL_OPERATORS = {'&&', '||', '|', ';', '&', '<', '>', '>>', '2>&1', '|&'}
REDIRECT = re.compile(r'^\d*[<>]')
DEFAULT_TIMEOUT = 600
SECRET_NAME = re.compile(r'SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|API_?KEY|PRIVATE|^FRONTLIGHTS_', re.I)
UNITTEST_FAILURE = re.compile(r'^(?:FAIL|ERROR): (.+?)\s*$')
PYTEST_FAILURE = re.compile(r'^(?:FAILED|ERROR) (\S*(?:::|\.py)\S*)')
PYTEST_SUMMARY = re.compile(r'^=*\s*((?:\d+ \w+(?:, )?)+) in [\d.]+s')
UNITTEST_RAN = re.compile(r'^Ran (\d+) tests? in ', re.M)
UNNAMED_FAILURE = '<suite exit code {}>'


class Refused(Exception):
    """Configuração ou uso recusado; nada foi executado."""


def redact(text):
    for name, value in os.environ.items():
        if len(value) >= 4 and SECRET_NAME.search(name):
            text = text.replace(value, '[oculto]')
    return text


def program_name(argument):
    name = re.split(r'[/\\]', argument)[-1].lower()
    return name[:-4] if name.endswith('.exe') else name


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
    if program_name(argv[0]) in SHELL_PROGRAMS or any(a in SHELL_OPERATORS or REDIRECT.match(a) for a in argv):
        raise Refused('checks.regression.argv não aceita shell embutido (cmd, sh, bash, powershell, '
                      '&&, |, ;, redirecionamentos); declare o executável e seus argumentos.')
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


def parse_failures(output):
    names = []
    for line in output.splitlines():
        match = UNITTEST_FAILURE.match(line) or PYTEST_FAILURE.match(line)
        if match and match.group(1) not in names:
            names.append(match.group(1))
    return names


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
        raise Refused(f'{root} não é uma worktree Git legível.')
    return done.stdout


def tree_evidence(root):
    """HEAD e hashes da árvore (mesma ideia de git_evidence em frontlights.py, sem importá-lo)."""
    root = Path(root).resolve(strict=True)
    head = git_bytes(root, 'rev-parse', 'HEAD').decode().strip()
    diff = git_bytes(root, 'diff', '--binary', '--no-ext-diff', '--no-textconv', 'HEAD', '--', '.', ':!.frontlights')
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
    return {'head': head, 'diff_sha256': hashlib.sha256(diff).hexdigest(),
            'files_sha256': hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()}


def infrastructure(kind, message):
    return {'classification': 'infrastructure', 'exit_code': None, 'failures': [], 'tests_run': None,
            'infrastructure': {'kind': kind, 'message': message}}


def run_suite(argv, cwd, timeout):
    try:
        done = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, errors='replace',
                              stdin=subprocess.DEVNULL, timeout=timeout)
    except FileNotFoundError:
        return infrastructure('executable_missing', 'Executável da suíte não encontrado.')
    except subprocess.TimeoutExpired:
        return infrastructure('timeout', f'A suíte passou de {timeout} s e foi interrompida.')
    except OSError:
        return infrastructure('cannot_start', 'A suíte não pôde ser iniciada.')
    output = redact(done.stdout + done.stderr)
    failures, tests_run = parse_failures(output), count_tests(output)
    result = {'exit_code': done.returncode, 'failures': failures, 'tests_run': tests_run}
    if tests_run is None and not failures:
        result.update(infrastructure('no_result', f'A suíte terminou (código {done.returncode}) sem relatar nenhum teste.'))
        result['exit_code'] = done.returncode
    elif tests_run == 0 and not failures:
        result.update(infrastructure('no_result', 'A suíte terminou sem executar nenhum teste.'))
        result['exit_code'] = done.returncode
    else:
        if done.returncode != 0 and not failures:
            result['failures'] = [UNNAMED_FAILURE.format(done.returncode)]
        result['classification'] = 'product_failure' if result['failures'] else 'passed'
    return result


def execute(label, settings, tree, record_dir):
    """Roda a suíte em `tree` e grava o registro `regression-<label>.json`."""
    cwd = run_directory(tree, settings['cwd'])
    record = {'label': label, 'argv': settings['argv'], 'timestamp': dt.datetime.now(dt.timezone.utc).isoformat()}
    record.update(tree_evidence(tree))
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


def run_regression(args):
    settings = regression_settings(args.config)
    root, base_tree = Path(args.root).resolve(strict=True), Path(args.base).resolve(strict=True)
    if root == base_tree:
        raise Refused('--base e --root precisam ser diretórios diferentes.')
    run_directory(base_tree, settings['cwd'])
    run_directory(root, settings['cwd'])
    record_dir = root / '.frontlights' / 'issues' / str(args.issue) / 'checks'
    base = execute('base', settings, base_tree, record_dir)
    branch = execute('branch', settings, root, record_dir)
    problems = [{'side': side, **record['infrastructure']}
                for side, record in (('base', base), ('branch', branch)) if 'infrastructure' in record]
    new = existing = fixed = []
    if not problems:
        new = [n for n in branch['failures'] if n not in base['failures']]
        existing = [n for n in branch['failures'] if n in base['failures']]
        fixed = [n for n in base['failures'] if n not in branch['failures']]
    summary = lambda r: {k: r[k] for k in ('head', 'diff_sha256', 'classification', 'exit_code', 'tests_run', 'record')}
    result = {'ok': not new and not problems, 'blocking': bool(new or problems), 'new_failures': new,
              'existing_failures': existing, 'fixed': fixed, 'infrastructure': problems,
              'base': summary(base), 'branch': summary(branch)}
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
