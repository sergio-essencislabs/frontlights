#!/usr/bin/env python3
"""Roda a spec Playwright do app de exemplo contra uma URL já servida e diz se passou.

Uso: python run_spec.py --config config.json --base-url http://127.0.0.1:<porta>/
A URL vem do `serve start` (a porta reservada do processo `app`). A conta é o primeiro item de
`browserTest.users` do config, entregue à spec em FRONTLIGHTS_LOGIN e FRONTLIGHTS_PASSWORD.

Imprime um objeto JSON em stdout com `resultado`:
  passou         toda a spec rodou e passou (nenhum teste pulado, falho ou instável; `.only` é proibido) e o
                 Playwright saiu com 0;
  falhou         algum teste falhou ou o Playwright saiu com código diferente de zero;
  nao_executado  a spec não pôde rodar ou não rodou inteira: --base-url fora de 127.0.0.1, localhost ou ::1 (a
                 conta nem é entregue), sem Node, sem @playwright/test, sem navegador baixado, app fora do ar
                 (ou sem rede), tempo esgotado, relatório ilegível, teste pulado ou nenhum teste executado.
                 `motivo` explica.
Só `passou` traz `aprovado: true`; a ausência de qualquer coisa nunca conta como aprovada.
Códigos de saída: 0 passou, 1 falhou, 3 não executado.

Nada é instalado aqui: Playwright e o navegador são instalados à mão (veja o README desta pasta). Login e senha
são mascarados em toda a saída.
"""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

HERE = Path(__file__).resolve().parent
EXIT_CODES = {'passou': 0, 'falhou': 1, 'nao_executado': 3}
MASK = '[redacted]'
DEFAULT_TIMEOUT = 300
BROWSER_MISSING = ("Executable doesn't exist", 'playwright install')
ANSI = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')
LOCAL_HOSTS = {'127.0.0.1', 'localhost', '::1'}


def find_node():
    return shutil.which('node')


def resolve_cli(node):
    """Caminho do cli.js do @playwright/test visto desta pasta (node_modules local ou NODE_PATH), ou None."""
    try:
        done = subprocess.run([node, '-e', "process.stdout.write(require.resolve('@playwright/test/cli'))"],
                              cwd=HERE, capture_output=True, text=True, encoding='utf-8', errors='replace',
                              timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 and done.stdout.strip() else None


def app_answers(url):
    """True quando algo responde HTTP na URL (qualquer status); o proxy do ambiente é ignorado."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=5):
            return True
    except urllib.error.HTTPError as error:
        error.close()
        return True
    except (OSError, ValueError):
        return False


def kill_tree(process):
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True, timeout=60)
    else:
        import signal
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
    process.kill()


def run_playwright(node, cli, cwd, env, timeout):
    """Roda a spec; devolve (código, stdout, stderr, relatório JSON em texto ou None). Código None: tempo esgotado."""
    options = {'start_new_session': True} if os.name != 'nt' else {
        'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    process = subprocess.Popen([node, cli, 'test', '--config', 'playwright.config.js', '--reporter=json',
                                '--forbid-only'],
                               cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace', **options)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(process)
        stdout, stderr = process.communicate()
        return None, stdout, stderr, None
    report = Path(env['PLAYWRIGHT_JSON_OUTPUT_NAME'])
    text = report.read_text(encoding='utf-8', errors='replace') if report.is_file() else None
    return process.returncode, stdout, stderr, text


def read_account(config):
    try:
        users = json.loads(Path(config).read_text(encoding='utf-8-sig'))['browserTest']['users']
        login, password = users[0]['login'], users[0]['password']
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return None
    if isinstance(login, str) and login and isinstance(password, str) and password:
        return login, password
    return None


def masker(secrets):
    variants = set()
    for secret in secrets:
        variants.update({secret, urllib.parse.quote(secret, safe=''), urllib.parse.quote_plus(secret)})
    pattern = re.compile('|'.join(re.escape(item) for item in sorted(variants, key=len, reverse=True)))

    def mask(value):
        if isinstance(value, str):
            return pattern.sub(MASK, value)
        if isinstance(value, list):
            return [mask(item) for item in value]
        if isinstance(value, dict):
            return {key: mask(item) for key, item in value.items()}
        return value
    return mask


def outcome(result, reason=None, **extra):
    return dict({'resultado': result, 'aprovado': result == 'passou', 'motivo': reason}, **extra)


def error_messages(report, mask):
    found = []

    def walk(suite):
        for spec in suite.get('specs', []):
            for test in spec.get('tests', []):
                for attempt in test.get('results', []):
                    message = (attempt.get('error') or {}).get('message')
                    if message:
                        found.append(mask(ANSI.sub('', message))[:1000])
        for child in suite.get('suites', []):
            walk(child)

    for suite in report.get('suites', []):
        walk(suite)
    found += [mask(ANSI.sub('', (error or {}).get('message', '')))[:1000] for error in report.get('errors', [])]
    return [message for message in found if message][:5]


def judge(code, stdout, stderr, report_text, timeout, mask):
    output = mask(ANSI.sub('', (stdout or '') + (stderr or '')))[-2000:]
    if code is None:
        return outcome('nao_executado', f'Tempo esgotado: a spec passou de {timeout} s e foi interrompida.',
                       saida=output)
    if any(marker in (report_text or '') + output for marker in BROWSER_MISSING):
        return outcome('nao_executado', 'O navegador do Playwright não está instalado: rode '
                       '`npx playwright install chromium` nesta pasta (veja o README).', saida=output)
    try:
        report = json.loads(report_text) if report_text else None
        stats = report['stats']
        counts = {'passaram': int(stats.get('expected', 0)), 'falharam': int(stats.get('unexpected', 0)),
                  'pulados': int(stats.get('skipped', 0)), 'instaveis': int(stats.get('flaky', 0))}
    except (ValueError, TypeError, KeyError):
        return outcome('nao_executado', f'O Playwright terminou (código {code}) sem um relatório legível.',
                       saida=output)
    details = {'testes': counts, 'erros': error_messages(report, mask), 'saida': output}
    if counts['falharam'] == 0 and (counts['pulados'] or counts['passaram'] + counts['instaveis'] == 0):
        return outcome('nao_executado', f'A spec não rodou inteira ({counts["pulados"]} teste(s) pulado(s), '
                       f'{counts["passaram"] + counts["instaveis"]} executado(s)): ela não comprovou o login.',
                       **details)
    if code == 0 and counts['falharam'] == 0 and counts['instaveis'] == 0:
        return outcome('passou', **details)
    return outcome('falhou', f'A spec falhou (código {code}).', **details)


def run(config, base_url, timeout=DEFAULT_TIMEOUT):
    try:
        host = urllib.parse.urlsplit(base_url).hostname
    except ValueError:
        host = None
    if host not in LOCAL_HOSTS:
        return outcome('nao_executado', f'A --base-url {base_url} foi recusada: o teste só roda em host local '
                       '(127.0.0.1, localhost ou ::1), e a conta do config não foi entregue.')
    account = read_account(config)
    if account is None:
        return outcome('nao_executado', 'O config não traz login e senha em browserTest.users[0].')
    mask = masker(account)
    node = find_node()
    if node is None:
        return mask(outcome('nao_executado', 'Node não encontrado no PATH: instale o Node para rodar a spec.'))
    cli = resolve_cli(node)
    if cli is None:
        return mask(outcome('nao_executado', 'Playwright (@playwright/test) não está instalado para esta pasta: '
                            'rode `npm install` nela (veja o README).'))
    if not app_answers(base_url):
        return mask(outcome('nao_executado', f'O app não responde em {base_url} (fora do ar ou sem rede): suba-o '
                            'com `serve start` e use a porta reservada.'))
    with tempfile.TemporaryDirectory(prefix='frontlights-spec-', ignore_cleanup_errors=True) as scratch:
        env = dict(os.environ, FRONTLIGHTS_BASE_URL=base_url, FRONTLIGHTS_LOGIN=account[0],
                   FRONTLIGHTS_PASSWORD=account[1], FRONTLIGHTS_RESULTS_DIR=str(Path(scratch) / 'resultados'),
                   PLAYWRIGHT_JSON_OUTPUT_NAME=str(Path(scratch) / 'relatorio.json'))
        code, stdout, stderr, report = run_playwright(node, cli, HERE, env, timeout)
    return judge(code, stdout, stderr, report, timeout, mask)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--config', required=True)
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--timeout', type=float, default=DEFAULT_TIMEOUT)
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    try:
        result = run(args.config, args.base_url, args.timeout)
    except OSError as error:
        result = outcome('nao_executado', f'Falha de entrada e saída: {error.strerror or type(error).__name__}.')
    print(json.dumps(result, ensure_ascii=False))
    return EXIT_CODES[result['resultado']]


if __name__ == '__main__':
    sys.exit(main())
