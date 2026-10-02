import http.cookiejar
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
import serve  # noqa: E402

EXAMPLE = REPO / 'examples' / 'browser-app'
CONFIG = EXAMPLE / 'config.json'
SERVE = REPO / 'scripts' / 'serve.py'
RUNNER = EXAMPLE / 'run_spec.py'
ISSUE = 13
LOCAL_HOSTS = {'127.0.0.1', 'localhost', '::1'}
GENERIC_DOMAIN = 'exemplo.test'


def load_runner():
    spec = importlib.util.spec_from_file_location('run_spec', RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def account():
    user = json.loads(CONFIG.read_text(encoding='utf-8'))['browserTest']['users'][0]
    return user['login'], user['password']


def utf8_env(**extra):
    return dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUTF8='1', **extra)


def wait_until(condition, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.1)
    return condition()


def reachable(url):
    try:
        with urllib.request.urlopen(url, timeout=2):
            return True
    except urllib.error.HTTPError as error:
        error.close()
        return True
    except OSError:
        return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class ExampleConfigTest(unittest.TestCase):
    def test_the_example_config_loads_through_serve_with_an_auto_port_in_the_health_host(self):
        processes = serve.load_block(CONFIG)
        self.assertTrue(processes)
        for item in processes:
            self.assertEqual(item.get('port'), 'auto', item['name'])
            self.assertTrue(urllib.parse.urlsplit(item['health']).netloc.endswith(':{port}'), item['health'])
            self.assertIn(urllib.parse.urlsplit(item['health']).hostname, LOCAL_HOSTS)

    def test_the_example_config_uses_only_generic_markers(self):
        config = json.loads(CONFIG.read_text(encoding='utf-8'))
        self.assertEqual(config['repository'], 'OWNER/REPOSITORY')
        self.assertIsNone(config['roads'])
        block = config['browserTest']
        self.assertIn(urllib.parse.urlsplit(block['baseUrl']).hostname, LOCAL_HOSTS)
        for user in block['users']:
            self.assertTrue(user['login'].endswith('@' + GENERIC_DOMAIN), user['login'])

    def test_the_app_knows_the_fictitious_account_of_the_config(self):
        source = (EXAMPLE / 'app.py').read_text(encoding='utf-8')
        login, password = account()
        self.assertIn(repr(login), source)
        self.assertIn(repr(password), source)


class GenericMarkersScanTest(unittest.TestCase):
    """Nothing real in the example: only local or exemplo.test URLs, fictitious e-mails, no machine paths."""

    def files(self):
        skipped = {'node_modules', 'test-results', '__pycache__'}
        found = [path for path in EXAMPLE.rglob('*')
                 if path.is_file() and not skipped & set(path.relative_to(EXAMPLE).parts)]
        self.assertTrue(found)
        return found

    def test_every_url_points_to_a_local_host_or_the_generic_domain(self):
        for path in self.files():
            text = path.read_text(encoding='utf-8')
            for url in re.findall(r'https?://[^\s\'"`)<>]+', text):
                host = urllib.parse.urlsplit(url.replace('{port}', '1')).hostname or ''
                self.assertTrue(host in LOCAL_HOSTS or host == GENERIC_DOMAIN or host.endswith('.' + GENERIC_DOMAIN)
                                or host in ('playwright.dev', 'nodejs.org'),
                                f'{path.name}: {url}')

    def test_every_e_mail_is_fictitious_and_no_machine_path_appears(self):
        for path in self.files():
            text = path.read_text(encoding='utf-8')
            for mail in re.findall(r'[\w.+-]+@[\w-]+(?:\.[\w-]+)+', text):
                if mail.startswith('@'):
                    continue
                self.assertTrue(mail.endswith('@' + GENERIC_DOMAIN), f'{path.name}: {mail}')
            self.assertIsNone(re.search(r'[A-Za-z]:\\|/home/|/Users/|AppData', text), path.name)

    def test_the_only_password_is_the_fictitious_one_of_the_config(self):
        _, password = account()
        for path in self.files():
            text = path.read_text(encoding='utf-8')
            for found in re.findall(r'(?i)"?(?:password|senha)"?\s*[:=]\s*[\'"]([^\'"]+)[\'"]', text):
                self.assertEqual(found, password, path.name)


class ServeCase(unittest.TestCase):
    """The real example app, brought up by `serve start` from a temporary root (never this repository)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'projeto'
        target = self.root / 'examples' / 'browser-app'
        target.mkdir(parents=True)
        shutil.copy2(EXAMPLE / 'app.py', target / 'app.py')
        config = json.loads(CONFIG.read_text(encoding='utf-8'))
        if shutil.which(config['browserTest']['processes'][0]['argv'][0]) is None:
            for item in config['browserTest']['processes']:
                item['argv'][0] = sys.executable
        self.config = Path(self.tmp.name) / 'config.json'
        self.config.write_text(json.dumps(config), encoding='utf-8')
        self.addCleanup(self.serve, 'stop')

    def serve(self, operation):
        done = subprocess.run([sys.executable, str(SERVE), operation, '--config', str(self.config),
                               '--root', str(self.root), '--issue', str(ISSUE)], capture_output=True, text=True,
                              encoding='utf-8', errors='replace', timeout=120, env=utf8_env())
        return done

    def start(self):
        done = self.serve('start')
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        entry = json.loads(done.stdout)['processes'][0]
        return entry, f'http://127.0.0.1:{entry["port"]}'


class ServeStartTest(ServeCase):
    def test_serve_start_brings_the_app_up_on_the_reserved_port_and_stop_brings_it_down(self):
        entry, base = self.start()
        port = entry['port']
        self.assertIsInstance(port, int)
        self.assertEqual(entry['url'], f'{base}/health')
        with urllib.request.urlopen(entry['url'], timeout=5) as answer:
            self.assertEqual(answer.status, 200)
        reservation = self.root / '.frontlights' / 'serve' / 'ports' / f'{port}.json'
        self.assertTrue(reservation.is_file())
        stopped = self.serve('stop')
        self.assertEqual(stopped.returncode, 0, stopped.stdout)
        self.assertEqual(json.loads(stopped.stdout)['reservas_liberadas'], [port])
        self.assertTrue(wait_until(lambda: not reachable(entry['url'])), 'o app ficou no ar depois do stop')
        self.assertFalse(reservation.exists())

    def test_the_login_flow_works_over_http_with_the_fictitious_account(self):
        _, base = self.start()
        login, password = account()
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), _NoRedirect)

        with opener.open(f'{base}/', timeout=5) as answer:
            page = answer.read().decode('utf-8')
        self.assertIn('<form', page)
        self.assertIn('name="login"', page)
        self.assertIn('name="senha"', page)

        def post(senha):
            data = urllib.parse.urlencode({'login': login, 'senha': senha}).encode()
            try:
                with opener.open(f'{base}/entrar', data=data, timeout=5) as answer:
                    return answer.status, answer.headers, answer.read().decode('utf-8')
            except urllib.error.HTTPError as error:
                with error:
                    return error.code, error.headers, error.read().decode('utf-8')

        def painel():
            try:
                with opener.open(f'{base}/painel', timeout=5) as answer:
                    return answer.status, answer.headers, answer.read().decode('utf-8')
            except urllib.error.HTTPError as error:
                with error:
                    return error.code, error.headers, error.read().decode('utf-8')

        self.assertEqual(painel()[0], 303, 'o painel abriu sem login')
        code, _, body = post(password + '-errada')
        self.assertEqual(code, 401)
        self.assertIn('inválidos', body)
        self.assertEqual(painel()[0], 303, 'senha errada abriu sessão')

        code, headers, _ = post(password)
        self.assertEqual(code, 303)
        self.assertEqual(headers['Location'], '/painel')
        code, _, body = painel()
        self.assertEqual(code, 200)
        self.assertIn(login, body)
        self.assertNotIn(password, body)


class RunnerOutcomeTest(unittest.TestCase):
    """The spec runner reports passou, falhou or nao_executado; only passou counts as approved."""

    def setUp(self):
        self.runner = load_runner()
        self.login, self.password = account()

    def run_with(self, base_url='http://127.0.0.1:1/', **patches):
        defaults = {'find_node': lambda: 'node', 'resolve_cli': lambda node: 'cli.js',
                    'app_answers': lambda url: True}
        defaults.update(patches)
        with mock.patch.multiple(self.runner, **defaults):
            return self.runner.run(CONFIG, base_url)

    def report(self, expected=0, unexpected=0, skipped=0, flaky=0, message=''):
        tests = [{'results': [{'status': 'failed', 'error': {'message': message}}]}] if message else []
        return json.dumps({'stats': {'expected': expected, 'unexpected': unexpected, 'skipped': skipped,
                                     'flaky': flaky},
                           'suites': [{'specs': [{'title': 'login', 'tests': tests}]}], 'errors': []})

    def playwright(self, code, report, stdout='', stderr=''):
        return lambda *args, **kwargs: (code, stdout, stderr, report)

    def assert_not_run(self, result, *words):
        self.assertEqual(result['resultado'], 'nao_executado', result)
        self.assertIs(result['aprovado'], False)
        self.assertEqual(self.runner.EXIT_CODES[result['resultado']], 3)
        for word in words:
            self.assertIn(word.lower(), result['motivo'].lower())

    def test_without_node_the_spec_is_not_run_and_the_reason_is_given(self):
        self.assert_not_run(self.run_with(find_node=lambda: None), 'Node')

    def test_without_playwright_installed_the_spec_is_not_run(self):
        self.assert_not_run(self.run_with(resolve_cli=lambda node: None), 'Playwright')

    def test_without_the_app_answering_the_spec_is_not_run(self):
        self.assert_not_run(self.run_with(app_answers=lambda url: False), 'não responde')

    def test_without_the_browser_downloaded_the_spec_is_not_run_even_if_playwright_says_failed(self):
        missing = ("browserType.launch: Executable doesn't exist at /x/chrome\n"
                   'Please run the following command to download new browsers: npx playwright install')
        result = self.run_with(run_playwright=self.playwright(1, self.report(unexpected=2, message=missing)))
        self.assert_not_run(result, 'navegador')

    def test_a_run_where_no_test_executed_is_not_approved(self):
        result = self.run_with(run_playwright=self.playwright(0, self.report(expected=0, skipped=2)))
        self.assert_not_run(result, 'pulado')
        result = self.run_with(run_playwright=self.playwright(1, self.report()))
        self.assert_not_run(result)

    def test_a_run_with_a_skipped_test_is_not_approved_even_if_the_others_passed(self):
        for passed in (1, 2, 5):
            result = self.run_with(run_playwright=self.playwright(0, self.report(expected=passed, skipped=1)))
            self.assert_not_run(result, 'pulado')

    def test_a_failure_next_to_a_skipped_test_is_still_reported_as_failed(self):
        result = self.run_with(run_playwright=self.playwright(1, self.report(unexpected=1, skipped=1,
                                                                              message='expect falhou')))
        self.assertEqual(result['resultado'], 'falhou', result)

    def test_playwright_is_called_so_that_a_focused_test_only_makes_the_run_fail(self):
        seen = []

        class Process:
            returncode = 1

            def __init__(self, argv, **kwargs):
                seen.append(argv)

            def communicate(self, timeout=None):
                return '', ''

        with tempfile.TemporaryDirectory() as scratch, mock.patch.object(self.runner.subprocess, 'Popen', Process):
            env = {'PLAYWRIGHT_JSON_OUTPUT_NAME': str(Path(scratch) / 'relatorio.json')}
            self.runner.run_playwright('node', 'cli.js', scratch, env, 10)
        self.assertEqual(len(seen), 1)
        self.assertIn('--forbid-only', seen[0])

    def test_a_base_url_outside_the_local_hosts_is_refused_before_any_request_or_handing_the_account(self):
        calls = []

        def spy(name):
            return lambda *args, **kwargs: calls.append(name)

        for url in ('https://homologacao.exemplo.test/', 'http://localhost.exemplo.test:8080/',
                    'http://127.0.0.1.exemplo.test/', 'http://10.0.0.5:3000/', 'http://[::1'):
            result = self.run_with(base_url=url, app_answers=spy('app_answers'), run_playwright=spy('playwright'))
            self.assert_not_run(result, 'local')
            self.assertEqual(calls, [], url)

    def test_a_base_url_on_a_local_host_reaches_the_spec(self):
        for url in ('http://127.0.0.1:4321/', 'http://localhost:4321/', 'http://LOCALHOST:4321/',
                    'http://[::1]:4321/'):
            seen = {}

            def fake(node, cli, cwd, env, timeout):
                seen.update(env)
                return 0, '', '', self.report(expected=2)

            result = self.run_with(base_url=url, run_playwright=fake)
            self.assertEqual(result['resultado'], 'passou', (url, result))
            self.assertEqual(seen['FRONTLIGHTS_BASE_URL'], url)

    def test_a_secret_cut_at_any_point_of_the_output_or_of_an_error_never_leaks_a_fragment(self):
        fragments = {secret[start:start + 5] for secret in (self.login, self.password)
                     for start in range(len(secret) - 4)}
        for padding in range(0, 2600, 3):
            text = 'x' * padding + self.password + ' ' + self.login + 'y' * padding

            def fake(node, cli, cwd, env, timeout, text=text):
                report = json.loads(self.report(unexpected=1, message=text))
                report['errors'] = [{'message': text}]
                return 1, '', text, json.dumps(report)

            result = self.run_with(run_playwright=fake)
            shown = json.dumps([result['saida'], result['erros']], ensure_ascii=False)
            leaked = [fragment for fragment in fragments if fragment in shown]
            self.assertEqual(leaked, [], padding)

    def test_an_unreadable_report_is_not_approved(self):
        result = self.run_with(run_playwright=self.playwright(0, None))
        self.assert_not_run(result)
        result = self.run_with(run_playwright=self.playwright(0, '{lixo'))
        self.assert_not_run(result)

    def test_a_timeout_is_not_approved(self):
        result = self.run_with(run_playwright=self.playwright(None, None))
        self.assert_not_run(result, 'tempo')

    def test_a_failing_spec_is_reported_as_failed_with_exit_code_one(self):
        result = self.run_with(run_playwright=self.playwright(1, self.report(expected=1, unexpected=1,
                                                                              message='expect falhou')))
        self.assertEqual(result['resultado'], 'falhou', result)
        self.assertIs(result['aprovado'], False)
        self.assertEqual(self.runner.EXIT_CODES['falhou'], 1)

    def test_a_nonzero_exit_with_a_clean_report_is_not_approved(self):
        result = self.run_with(run_playwright=self.playwright(1, self.report(expected=2)))
        self.assertFalse(result['aprovado'], result)

    def test_a_flaky_spec_is_not_approved(self):
        result = self.run_with(run_playwright=self.playwright(0, self.report(expected=1, flaky=1)))
        self.assertFalse(result['aprovado'], result)

    def test_only_a_clean_run_with_executed_tests_passes(self):
        result = self.run_with(run_playwright=self.playwright(0, self.report(expected=2)))
        self.assertEqual(result['resultado'], 'passou', result)
        self.assertIs(result['aprovado'], True)
        self.assertEqual(self.runner.EXIT_CODES['passou'], 0)
        self.assertEqual(result['testes'], {'passaram': 2, 'falharam': 0, 'pulados': 0, 'instaveis': 0})

    def test_the_spec_receives_the_account_and_the_base_url_and_the_output_masks_them(self):
        seen = {}

        def fake(node, cli, cwd, env, timeout):
            seen.update(env)
            text = f'login {self.login} senha {self.password}'
            return 1, text, text, self.report(expected=1, unexpected=1, message=text)

        result = self.run_with(base_url='http://127.0.0.1:4321/', run_playwright=fake)
        self.assertEqual(seen['FRONTLIGHTS_BASE_URL'], 'http://127.0.0.1:4321/')
        self.assertEqual((seen['FRONTLIGHTS_LOGIN'], seen['FRONTLIGHTS_PASSWORD']), (self.login, self.password))
        text = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(self.password, text)
        self.assertNotIn(self.login, text)
        self.assertIn('[redacted]', text)


class RunnerCommandLineTest(unittest.TestCase):
    def test_with_no_node_on_the_path_the_command_prints_json_not_run_and_exits_three(self):
        empty = tempfile.TemporaryDirectory()
        self.addCleanup(empty.cleanup)
        done = subprocess.run([sys.executable, str(RUNNER), '--config', str(CONFIG), '--base-url',
                               'http://127.0.0.1:1/'], capture_output=True, text=True, encoding='utf-8',
                              errors='replace', timeout=120, env=utf8_env(PATH=empty.name))
        self.assertEqual(done.returncode, 3, done.stdout + done.stderr)
        result = json.loads(done.stdout)
        self.assertEqual(result['resultado'], 'nao_executado')
        self.assertIs(result['aprovado'], False)
        self.assertIn('Node', result['motivo'])


class RealSpecTest(ServeCase):
    """The real Playwright spec against the app brought up by serve; skipped, with the reason, when not available."""

    def require_playwright(self):
        runner = load_runner()
        node = runner.find_node()
        if node is None:
            self.skipTest('Node não encontrado: a spec Playwright real não foi executada')
        if runner.resolve_cli(node) is None:
            self.skipTest('@playwright/test não instalado para examples/browser-app (veja o README): '
                          'a spec real não foi executada')

    def run_spec(self, base, **env):
        done = subprocess.run([sys.executable, str(RUNNER), '--config', str(CONFIG), '--base-url', f'{base}/'],
                              capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=600,
                              env=utf8_env(**env))
        return done, json.loads(done.stdout)

    def test_with_real_playwright_but_no_browser_downloaded_the_spec_is_not_run_and_not_approved(self):
        self.require_playwright()
        _, base = self.start()
        empty = tempfile.TemporaryDirectory()
        self.addCleanup(empty.cleanup)
        done, result = self.run_spec(base, PLAYWRIGHT_BROWSERS_PATH=empty.name)
        self.assertEqual((result['resultado'], result['aprovado'], done.returncode), ('nao_executado', False, 3),
                         result)
        self.assertIn('navegador', result['motivo'])

    def test_the_real_playwright_spec_logs_in_with_the_fictitious_account_and_passes(self):
        self.require_playwright()
        _, base = self.start()
        done, result = self.run_spec(base)
        if result['resultado'] == 'nao_executado':
            self.skipTest(f'spec real não executada: {result["motivo"]}')
        self.assertEqual(result['resultado'], 'passou', result)
        self.assertEqual(done.returncode, 0)
        self.assertGreaterEqual(result['testes']['passaram'], 1)

    def test_a_real_spec_that_does_not_run_the_login_test_is_never_approved(self):
        self.require_playwright()
        _, base = self.start()
        original = (EXAMPLE / 'login.spec.js').read_text(encoding='utf-8')
        login_test, wrong_test = "test('entra com", "test('recusa a"
        self.assertIn(login_test, original)
        self.assertIn(wrong_test, original)
        mutants = {
            'test.skip': original.replace(login_test, "test.skip('entra com"),
            'test.fixme': original.replace(login_test, "test.fixme('entra com"),
            'test.skip(true) no corpo': original.replace("async ({ page }) => {", "async ({ page }) => { test.skip(true);", 1),
            'test.describe.skip': original.replace(login_test, "test.describe.skip('grupo', () => {\n" + login_test)
                                          .replace(wrong_test, "});\n" + wrong_test),
            'test.only na senha errada': original.replace(wrong_test, "test.only('recusa a"),
        }
        node_path = os.pathsep.join(item for item in (str(EXAMPLE / 'node_modules'), os.environ.get('NODE_PATH'))
                                    if item)
        def run_copy(source):
            with tempfile.TemporaryDirectory() as copy:
                for item in ('run_spec.py', 'playwright.config.js'):
                    shutil.copy2(EXAMPLE / item, Path(copy) / item)
                (Path(copy) / 'login.spec.js').write_text(source, encoding='utf-8')
                done = subprocess.run([sys.executable, str(Path(copy) / 'run_spec.py'), '--config', str(CONFIG),
                                       '--base-url', f'{base}/'], capture_output=True, text=True, encoding='utf-8',
                                      errors='replace', timeout=600, env=utf8_env(NODE_PATH=node_path))
                return done, json.loads(done.stdout)

        control, result = run_copy(original)
        if result['resultado'] == 'nao_executado':
            self.skipTest(f'spec real não executada: {result["motivo"]}')
        self.assertEqual((result['resultado'], control.returncode), ('passou', 0), result)
        for name, source in mutants.items():
            with self.subTest(name):
                self.assertNotEqual(source, original)
                done, result = run_copy(source)
                self.assertNotEqual(result['resultado'], 'passou', result)
                self.assertIs(result['aprovado'], False)
                self.assertNotEqual(done.returncode, 0)


if __name__ == '__main__':
    unittest.main()
