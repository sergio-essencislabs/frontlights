import contextlib
import http.server
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import checks  # noqa: E402
import serve  # noqa: E402

SERVE = SCRIPTS / 'serve.py'
PASSWORD = 'senha-fake-123'
LOGIN = 'usuario1@exemplo.test'
GIT_ENV = {**os.environ, 'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.invalid',
           'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@example.invalid'}

# Servidor falso: `/broken` responde 500, `/moved` responde 302, o resto responde 200.
FAKE_SERVER = r"""import http.server
import sys


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        code = {'/broken': 500, '/moved': 302}.get(self.path, 200)
        self.send_response(code)
        if code == 302:
            self.send_header('Location', '/')
        self.send_header('Content-Length', '0')
        self.end_headers()

    def log_message(self, *args):
        pass


http.server.HTTPServer(('127.0.0.1', int(sys.argv[1])), Handler).serve_forever()
"""

# Argv de integração falso: grava o que recebeu (env e argumento) e sai com o código pedido.
RECORDER = r"""import json
import os
import sys

with open(sys.argv[1], 'w', encoding='utf-8') as out:
    json.dump({'env': os.environ.get('FRONTLIGHTS_BACKEND_URL'), 'arg': sys.argv[2]}, out)
sys.exit(int(sys.argv[3]))
"""

CWD_RECORDER = r"""import os
import sys

with open(sys.argv[1], 'w', encoding='utf-8') as out:
    out.write(os.getcwd())
"""

# Suíte falsa no formato unittest: falha se existir `fail.flag` no diretório de trabalho.
FAKE_SUITE = r"""import os

if os.path.exists('fail.flag'):
    print('FAIL: test_secretword (modulo.Caso.test_secretword)')
    print('-' * 70)
    print('AssertionError: quebrou')
    print()
    print('Ran 1 test in 0.001s')
    print()
    print('FAILED (failures=1)')
    raise SystemExit(1)
print('Ran 1 test in 0.001s')
print()
print('OK')
"""


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class IntegrationTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.root = self.tmp / 'branch'
        self.root.mkdir()
        subprocess.run(['git', '-C', str(self.root), 'init', '-q'], check=True, env=GIT_ENV)
        (self.root / 'file.txt').write_text('um', encoding='utf-8')
        subprocess.run(['git', '-C', str(self.root), 'add', '-A'], check=True, env=GIT_ENV)
        subprocess.run(['git', '-C', str(self.root), 'commit', '-q', '-m', 'inicial'], check=True, env=GIT_ENV)
        (self.tmp / 'server.py').write_text(FAKE_SERVER, encoding='utf-8')
        (self.tmp / 'recorder.py').write_text(RECORDER, encoding='utf-8')
        self.config = self.tmp / 'config.json'
        self.ports = {'api': free_port(), 'web': free_port()}
        self.addCleanup(self.serve, 'stop')

    def process(self, name):
        port = self.ports[name]
        return {'name': name, 'argv': [sys.executable, str(self.tmp / 'server.py'), str(port)], 'port': port,
                'health': f'http://127.0.0.1:{port}/health', 'timeoutSeconds': 20}

    def write_config(self, checks_block, processes=None, users=None):
        browser = {'processes': processes or [self.process('api'), self.process('web')],
                   'baseUrl': 'http://127.0.0.1:1',
                   'users': users if users is not None else [{'login': LOGIN, 'password': PASSWORD}]}
        self.config.write_text(json.dumps({'repository': 'OWNER/REPOSITORY', 'roads': None,
                                           'browserTest': browser, 'checks': checks_block}), encoding='utf-8')

    def serve(self, command, issue=12):
        return subprocess.run([sys.executable, str(SERVE), command, '--config', str(self.config),
                               '--root', str(self.root), '--issue', str(issue)],
                              capture_output=True, text=True, encoding='utf-8', timeout=120)

    def start_serve(self):
        done = self.serve('start')
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def run_check(self, command, issue=12, extra=()):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = checks.main([command, '--config', str(self.config), '--root', str(self.root),
                                '--issue', str(issue), *extra])
        self.last_output = out.getvalue()
        return code, json.loads(self.last_output)

    def record(self, name, issue=12):
        path = self.root / '.frontlights' / 'issues' / str(issue) / 'checks' / f'{name}.json'
        return json.loads(path.read_text(encoding='utf-8'))

    def recorder_argv(self, exit_code=0):
        return [sys.executable, str(self.tmp / 'recorder.py'), str(self.tmp / 'seen.json'), '{backend_url}',
                str(exit_code)]

    def write_registry(self, url, issue=12, pid=None):
        path = self.root / '.frontlights' / 'serve' / f'{issue}.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        if pid is None:  # um processo vivo qualquer; nunca o do próprio teste, que o `stop` do cleanup mataria
            sleeper = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
            self.addCleanup(sleeper.wait)
            self.addCleanup(sleeper.kill)
            pid = sleeper.pid
        entry = {'name': 'api', 'pid': pid, 'port': 1, 'url': url, 'startedAt': 'agora',
                 'identity': serve.process_identity(pid) or serve.UNKNOWN_IDENTITY}
        path.write_text(json.dumps({'issue': issue, 'root': str(self.root), 'processes': [entry]}),
                        encoding='utf-8')

    def write_raw_registry(self, processes, issue=12):
        path = self.root / '.frontlights' / 'serve' / f'{issue}.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'issue': issue, 'root': str(self.root), 'processes': processes}),
                        encoding='utf-8')

    def live_pid(self):
        sleeper = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
        self.addCleanup(sleeper.wait)
        self.addCleanup(sleeper.kill)
        return sleeper.pid

    def seen(self):
        return json.loads((self.tmp / 'seen.json').read_text(encoding='utf-8'))


class IntegrationUrlTest(IntegrationTestCase):
    def test_argv_receives_the_branch_backend_url_by_env_and_placeholder(self):
        self.write_config({'backend': 'api', 'integration': {'argv': self.recorder_argv(), 'timeoutSeconds': 60}})
        self.start_serve()
        code, result = self.run_check('integration')
        expected = f'http://127.0.0.1:{self.ports["api"]}'
        self.assertEqual(code, 0, result)
        self.assertEqual(self.seen(), {'env': expected, 'arg': expected})

    def test_backend_setting_picks_the_named_process_instead_of_the_first(self):
        self.write_config({'backend': 'web', 'integration': {'argv': self.recorder_argv()}})
        self.start_serve()
        code, _ = self.run_check('integration')
        self.assertEqual(code, 0)
        self.assertEqual(self.seen()['env'], f'http://127.0.0.1:{self.ports["web"]}')

    def test_backend_setting_naming_an_unknown_process_is_refused_before_running(self):
        self.write_config({'backend': 'inexistente', 'integration': {'argv': self.recorder_argv()}})
        self.start_serve()
        code, result = self.run_check('integration')
        self.assertEqual(code, 2)
        self.assertIn('inexistente', result['error'])
        self.assertFalse((self.tmp / 'seen.json').exists())

    def test_url_whose_host_is_not_local_is_refused_as_infrastructure_without_running(self):
        self.write_config({'integration': {'argv': self.recorder_argv()}})
        self.write_registry('http://api.exemplo.test:8080/health')
        code, result = self.run_check('integration')
        self.assertEqual(code, 3)
        self.assertEqual(result['classification'], 'infrastructure')
        self.assertEqual(result['infrastructure']['kind'], 'non_local_url')
        self.assertIn('local', result['infrastructure']['message'])
        self.assertFalse((self.tmp / 'seen.json').exists())

    def test_loopback_hosts_are_accepted(self):
        self.write_config({'integration': {'argv': self.recorder_argv()}})
        for host in ('localhost', '[::1]', '127.0.0.1'):
            self.write_registry(f'http://{host}:8080/health')
            code, _ = self.run_check('integration')
            self.assertEqual(code, 0, host)
            expected = 'http://127.0.0.1:8080' if host == 'localhost' else f'http://{host}:8080'
            self.assertEqual(self.seen()['env'], expected)


class IntegrationClassificationTest(IntegrationTestCase):
    def test_without_serve_registry_it_is_infrastructure_and_says_to_run_serve_start(self):
        self.write_config({'integration': {'argv': self.recorder_argv()}})
        code, result = self.run_check('integration')
        self.assertEqual(code, 3)
        self.assertEqual(result['classification'], 'infrastructure')
        self.assertEqual(result['infrastructure']['kind'], 'serve_registry_missing')
        self.assertIn('serve start', result['infrastructure']['message'])
        self.assertFalse((self.tmp / 'seen.json').exists())

    def test_backend_taken_down_is_infrastructure_not_product_failure(self):
        self.write_config({'backend': 'api', 'integration': {'argv': self.recorder_argv()}})
        self.start_serve()
        registry = json.loads((self.root / '.frontlights' / 'serve' / '12.json').read_text(encoding='utf-8'))
        entry = next(e for e in registry['processes'] if e['name'] == 'api')
        self.assertTrue(serve.kill_tree(entry['pid']))
        code, result = self.run_check('integration')
        self.assertEqual(code, 3)
        self.assertEqual(result['classification'], 'infrastructure')
        self.assertEqual(result['infrastructure']['kind'], 'serve_down')
        self.assertIn('api', result['infrastructure']['message'])
        self.assertFalse((self.tmp / 'seen.json').exists())

    def test_argv_exiting_nonzero_is_product_failure(self):
        self.write_config({'integration': {'argv': self.recorder_argv(exit_code=4)}})
        self.start_serve()
        code, result = self.run_check('integration')
        self.assertEqual(code, 1)
        self.assertEqual(result['classification'], 'product_failure')
        self.assertTrue(result['blocking'])
        self.assertEqual(result['exit_code'], 4)
        self.assertNotIn('infrastructure', result)

    def test_missing_executable_and_timeout_are_infrastructure(self):
        self.write_config({'integration': {'argv': ['executavel-que-nao-existe-xyz']}})
        self.start_serve()
        code, result = self.run_check('integration')
        self.assertEqual((code, result['infrastructure']['kind']), (3, 'executable_missing'))
        sleeper = self.tmp / 'sleeper.py'
        sleeper.write_text('import time\ntime.sleep(60)\n', encoding='utf-8')
        self.write_config({'integration': {'argv': [sys.executable, str(sleeper)], 'timeoutSeconds': 1}})
        code, result = self.run_check('integration')
        self.assertEqual((code, result['infrastructure']['kind']), (3, 'timeout'))


class IntegrationOutputTest(IntegrationTestCase):
    def test_output_marks_a_real_run_against_the_local_branch_backend(self):
        self.write_config({'integration': {'argv': self.recorder_argv()}})
        self.start_serve()
        _, result = self.run_check('integration')
        self.assertIs(result['simulacao'], False)
        self.assertEqual(result['alvo'], 'backend da branch (local)')

    def test_record_has_head_hashes_classification_and_time(self):
        self.write_config({'integration': {'argv': self.recorder_argv()}})
        self.start_serve()
        _, result = self.run_check('integration')
        head = subprocess.run(['git', '-C', str(self.root), 'rev-parse', 'HEAD'], capture_output=True, text=True,
                              check=True).stdout.strip()
        record = self.record('integration')
        self.assertEqual(record['head'], head)
        self.assertRegex(record['diff_sha256'], '^[0-9a-f]{64}$')
        self.assertIn('diff_sha256_vs_base', record)
        self.assertEqual(record['classification'], 'passed')
        self.assertIs(record['simulacao'], False)
        self.assertTrue(record['timestamp'])
        self.assertEqual(result['head'], head)

    def test_infrastructure_failure_is_also_recorded(self):
        self.write_config({'integration': {'argv': self.recorder_argv()}})
        code, _ = self.run_check('integration')
        self.assertEqual(code, 3)
        self.assertEqual(self.record('integration')['classification'], 'infrastructure')

    def test_diff_hash_vs_base_is_present_when_a_base_is_given(self):
        base = self.tmp / 'base'
        subprocess.run(['git', 'clone', '-q', str(self.root), str(base)], check=True, env=GIT_ENV)
        (self.root / 'file.txt').write_text('dois', encoding='utf-8')
        subprocess.run(['git', '-C', str(self.root), 'commit', '-q', '-am', 'fatia'], check=True, env=GIT_ENV)
        self.write_config({'integration': {'argv': self.recorder_argv()}})
        self.start_serve()
        self.run_check('integration', extra=['--base', str(base)])
        record = self.record('integration')
        self.assertRegex(record['diff_sha256_vs_base'], '^[0-9a-f]{64}$')
        self.assertNotEqual(record['diff_sha256'], record['diff_sha256_vs_base'])

    def test_login_and_password_are_absent_from_output_record_and_errors(self):
        leaky = self.tmp / 'leaky.py'
        leaky.write_text(f'print({PASSWORD!r}, {LOGIN!r})\nraise SystemExit(1)\n', encoding='utf-8')
        self.write_config({'integration': {'argv': [sys.executable, str(leaky), PASSWORD, f'--user={LOGIN}']}})
        self.start_serve()
        self.run_check('integration')
        record_path = self.root / '.frontlights' / 'issues' / '12' / 'checks' / 'integration.json'
        for text in (self.last_output, record_path.read_text(encoding='utf-8')):
            self.assertNotIn(PASSWORD, text)
            self.assertNotIn(LOGIN, text)
        self.write_config({'integration': {'argv': [sys.executable, str(leaky), 'a'], 'cwd': PASSWORD}})
        code, _ = self.run_check('integration')
        self.assertEqual(code, 2)
        self.assertNotIn(PASSWORD, self.last_output)

    def test_shell_embedded_in_the_argv_is_refused_like_the_regression(self):
        self.write_config({'integration': {'argv': ['bash', '-c', 'echo oi']}})
        code, result = self.run_check('integration')
        self.assertEqual(code, 2)
        self.assertIn('checks.integration.argv', result['error'])


class SmokeTest(IntegrationTestCase):
    def smoke_config(self, **smoke):
        self.write_config({'smoke': {'paths': ['/', '/health'], 'target': 'web', **smoke}})

    def test_healthy_fake_server_passes_and_reports_each_path(self):
        self.smoke_config()
        self.start_serve()
        code, result = self.run_check('smoke')
        self.assertEqual(code, 0, result)
        self.assertEqual(result['classification'], 'healthy')
        self.assertEqual([(p['path'], p['status']) for p in result['paths']], [('/', 200), ('/health', 200)])
        self.assertIs(result['simulacao'], False)
        self.assertEqual(result['base_url'], f'http://127.0.0.1:{self.ports["web"]}')

    def test_path_answering_5xx_is_product_failure(self):
        self.smoke_config(paths=['/', '/broken'])
        self.start_serve()
        code, result = self.run_check('smoke')
        self.assertEqual(code, 1)
        self.assertEqual(result['classification'], 'product_failure')
        self.assertEqual(result['failures'], ['/broken'])
        self.assertTrue(result['blocking'])

    def test_expect_status_overrides_the_default_range(self):
        self.smoke_config(paths=['/broken'], expectStatus=500)
        self.start_serve()
        self.assertEqual(self.run_check('smoke')[0], 0)
        self.smoke_config(paths=['/moved'], expectStatus=[200])
        code, result = self.run_check('smoke')
        self.assertEqual((code, result['failures']), (1, ['/moved']))
        self.smoke_config(paths=['/moved'])
        self.assertEqual(self.run_check('smoke')[0], 0)  # 3xx é aceito por padrão

    def test_target_process_taken_down_is_infrastructure(self):
        self.smoke_config()
        self.start_serve()
        registry = json.loads((self.root / '.frontlights' / 'serve' / '12.json').read_text(encoding='utf-8'))
        entry = next(e for e in registry['processes'] if e['name'] == 'web')
        self.assertTrue(serve.kill_tree(entry['pid']))
        code, result = self.run_check('smoke')
        self.assertEqual(code, 3)
        self.assertEqual(result['classification'], 'infrastructure')
        self.assertEqual(result['infrastructure']['kind'], 'serve_down')

    def test_connection_refused_with_a_live_pid_is_infrastructure(self):
        self.smoke_config()
        self.write_registry(f'http://127.0.0.1:{free_port()}/health')
        self.write_config({'smoke': {'paths': ['/'], 'target': 'api'}})
        code, result = self.run_check('smoke')
        self.assertEqual(code, 3)
        self.assertEqual(result['infrastructure']['kind'], 'connection_failed')

    def test_url_whose_host_is_not_local_is_refused_without_any_request(self):
        self.write_config({'smoke': {'paths': ['/']}})
        self.write_registry('http://app.exemplo.test/health')
        code, result = self.run_check('smoke')
        self.assertEqual(code, 3)
        self.assertEqual(result['infrastructure']['kind'], 'non_local_url')

    def test_record_has_head_hashes_and_classification(self):
        self.smoke_config()
        self.start_serve()
        self.run_check('smoke')
        head = subprocess.run(['git', '-C', str(self.root), 'rev-parse', 'HEAD'], capture_output=True, text=True,
                              check=True).stdout.strip()
        record = self.record('smoke')
        self.assertEqual(record['head'], head)
        self.assertRegex(record['diff_sha256'], '^[0-9a-f]{64}$')
        self.assertIn('diff_sha256_vs_base', record)
        self.assertEqual(record['classification'], 'healthy')
        self.assertIs(record['simulacao'], False)
        self.assertTrue(record['timestamp'])

    def test_login_and_password_are_absent_from_output_and_record(self):
        self.smoke_config()
        self.start_serve()
        self.run_check('smoke')
        record = (self.root / '.frontlights' / 'issues' / '12' / 'checks' / 'smoke.json').read_text(encoding='utf-8')
        for text in (self.last_output, record):
            self.assertNotIn(PASSWORD, text)
            self.assertNotIn(LOGIN, text)
        self.write_config({'smoke': {'paths': ['/'], 'target': PASSWORD}})
        code, _ = self.run_check('smoke')
        self.assertEqual(code, 2)
        self.assertNotIn(PASSWORD, self.last_output)

    def test_invalid_paths_are_refused_with_exit_2(self):
        for paths in (['health'], ['//outro.exemplo.test/'], ['http://outro.exemplo.test/'], [], 'x', ['/a b']):
            self.write_config({'smoke': {'paths': paths}})
            code, result = self.run_check('smoke')
            self.assertEqual(code, 2, paths)
            self.assertIn('checks.smoke.paths', result['error'])


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)


def key_names(value):
    if isinstance(value, list):
        return set().union(*[key_names(item) for item in value]) if value else set()
    if isinstance(value, dict):
        return set(value) | (set().union(*[key_names(item) for item in value.values()]) if value else set())
    return set()


class RegressionWithUserSecretsTest(IntegrationTestCase):
    """A senha de teste só é ocultada na saída e nos registros; o parser lê o texto bruto da suíte."""

    def setUp(self):
        super().setUp()
        self.base = self.tmp / 'base'
        subprocess.run(['git', 'clone', '-q', str(self.root), str(self.base)], check=True, env=GIT_ENV)
        (self.tmp / 'suite.py').write_text(FAKE_SUITE, encoding='utf-8')

    def regress(self, password, fail=False):
        users = [{'login': LOGIN, 'password': password}]
        self.write_config({'regression': {'argv': [sys.executable, str(self.tmp / 'suite.py')]}}, users=users)
        flag = self.root / 'fail.flag'
        if fail:
            flag.write_text('x', encoding='utf-8')
        elif flag.exists():
            flag.unlink()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = checks.main(['regression', '--config', str(self.config), '--root', str(self.root),
                                '--base', str(self.base), '--issue', '12'])
        self.last_output = out.getvalue()
        return code, json.loads(self.last_output)

    def test_passing_suite_still_passes_when_the_password_is_text_the_suite_prints(self):
        for password in ('test', 'Ran', 'FAIL', 'OK'):
            code, result = self.regress(password)
            self.assertEqual(code, 0, (password, result))
            self.assertEqual(result['infrastructure'], [], password)

    def test_new_failure_is_detected_when_the_password_is_text_the_suite_prints(self):
        for password in ('test', 'Ran', 'FAIL', 'failures'):
            code, result = self.regress(password, fail=True)
            self.assertEqual(code, 1, (password, result))
            self.assertEqual(len(result['new_failures']), 1, password)

    def test_secret_inside_a_failure_name_is_hidden_in_output_and_records_including_keys(self):
        code, _ = self.regress('secretword', fail=True)
        self.assertEqual(code, 1)
        self.assertNotIn('secretword', self.last_output)
        for label in ('base', 'branch'):
            path = self.root / '.frontlights' / 'issues' / '12' / 'checks' / f'regression-{label}.json'
            self.assertNotIn('secretword', path.read_text(encoding='utf-8'))

    def test_short_password_is_not_refused_by_the_regression(self):
        code, _ = self.regress('abc')
        self.assertEqual(code, 0)


class UserSecretMaskTest(IntegrationTestCase):
    def test_short_or_mask_colliding_secrets_are_refused_by_integration_and_smoke_before_anything_runs(self):
        cases = ([{'login': LOGIN, 'password': 'abc'}], [{'login': 'ab', 'password': PASSWORD}],
                 [{'login': LOGIN, 'password': '[oculto]x'}], [{'login': 'x[oculto]', 'password': PASSWORD}])
        for users in cases:
            self.write_config({'integration': {'argv': self.recorder_argv()}, 'smoke': {'paths': ['/']}},
                              users=users)
            self.write_registry('http://127.0.0.1:1/health')
            for command in ('integration', 'smoke'):
                code, result = self.run_check(command)
                self.assertEqual(code, 2, (users, command))
                self.assertIn('browserTest.users', result['error'])
                for secret in (users[0]['login'], users[0]['password']):
                    if len(secret) > 4:
                        self.assertNotIn(secret, self.last_output)
                self.assertFalse((self.tmp / 'seen.json').exists())
                self.assertFalse((self.root / '.frontlights' / 'issues').exists())

    def test_secrets_that_collide_with_keys_and_words_leave_the_json_structure_intact(self):
        blocks = {'integration': {'argv': self.recorder_argv()}, 'smoke': {'paths': ['/', '/health']}}
        self.write_config(blocks)
        self.start_serve()
        shapes = {}
        for command in ('integration', 'smoke'):
            self.run_check(command)
            shapes[command] = (key_names(json.loads(self.last_output)), key_names(self.record(command)))
        for password in ('head', 'path', 'name', 'port', 'ocul'):
            self.write_config(blocks, users=[{'login': LOGIN, 'password': password}])
            for command in ('integration', 'smoke'):
                code, result = self.run_check(command)
                self.assertEqual(code, 0, (password, command, result))
                record = self.record(command)
                self.assertEqual((key_names(result), key_names(record)), shapes[command], (password, command))
                for text in (self.last_output, json.dumps(record)):
                    self.assertNotIn('[[oculto', text)
                    self.assertNotIn('to]to]', text)
                if password != 'ocul':  # 'ocul' faz parte do próprio marcador
                    for value in list(strings(result)) + list(strings(record)):
                        self.assertNotIn(password, value.replace('[oculto]', ''), (password, command))

    def test_redaction_is_idempotent_and_never_feeds_on_its_own_mask(self):
        samples = ['head path name port', 'x ocul y', '[oculto]', 'ocul[oculto]ocul', 'abcd', 'a"b\\c']
        with mock.patch.object(checks, 'USER_SECRETS', ['ocul', 'head', 'abcd', 'a"b\\c', 'path']):
            for sample in samples:
                once = checks.redact(sample)
                self.assertEqual(checks.redact(once), once, sample)
            self.assertEqual(checks.redact('[oculto]'), '[oculto]')
            self.assertEqual(checks.redact('xheadx'), 'x[oculto]x')
            value = {'head': ['path', {'path': 'head'}], 'n': 3}
            self.assertEqual(checks.scrub(value), {'head': ['[oculto]', {'path': '[oculto]'}], 'n': 3})
            self.assertEqual(checks.scrub(checks.scrub(value)), checks.scrub(value))

    def test_longest_secret_wins_in_a_single_pass(self):
        with mock.patch.object(checks, 'USER_SECRETS', ['senha', 'senha-longa']):
            self.assertEqual(checks.redact('senha-longa e senha'), '[oculto] e [oculto]')


def port_is_open(port):
    with socket.socket() as sock:
        sock.settimeout(1)
        return sock.connect_ex(('127.0.0.1', port)) == 0


class SmokeAutostartTest(IntegrationTestCase):
    """O smoke sobe o serve quando não há processos vivos e derruba só o que ele mesmo iniciou."""

    def smoke_config(self, **smoke):
        self.write_config({'smoke': {'paths': ['/', '/health'], 'target': 'web', **smoke}})

    def assert_all_down(self):
        for port in self.ports.values():
            for _ in range(50):
                if not port_is_open(port):
                    break
                time.sleep(0.1)
            self.assertFalse(port_is_open(port), port)

    def test_without_registry_it_starts_passes_and_tears_down_what_it_started(self):
        self.smoke_config()
        code, result = self.run_check('smoke')
        self.assertEqual(code, 0, result)
        self.assertEqual(result['classification'], 'healthy')
        self.assertIs(result['serve_iniciado_pelo_smoke'], True)
        self.assertIs(result['serve_encerrado'], True)
        record = self.record('smoke')
        self.assertIs(record['serve_iniciado_pelo_smoke'], True)
        self.assertIs(record['serve_encerrado'], True)
        self.assert_all_down()
        self.assertFalse((self.root / '.frontlights' / 'serve' / '12.json').exists())

    def test_with_the_serve_already_running_it_uses_it_and_leaves_it_up(self):
        self.smoke_config()
        self.start_serve()
        code, result = self.run_check('smoke')
        self.assertEqual(code, 0, result)
        self.assertIs(result['serve_iniciado_pelo_smoke'], False)
        self.assertIs(result['serve_encerrado'], False)
        self.assertIs(self.record('smoke')['serve_iniciado_pelo_smoke'], False)
        self.assertTrue(port_is_open(self.ports['web']))
        self.assertTrue((self.root / '.frontlights' / 'serve' / '12.json').is_file())

    def test_registry_whose_processes_are_all_dead_is_replaced_by_a_fresh_start(self):
        done = subprocess.Popen([sys.executable, '-c', 'pass'])
        done.wait()
        self.write_registry('http://127.0.0.1:1/health', pid=done.pid)
        self.smoke_config(target='api')
        self.write_config({'smoke': {'paths': ['/'], 'target': 'api'}})
        code, result = self.run_check('smoke')
        self.assertEqual(code, 0, result)
        self.assertIs(result['serve_iniciado_pelo_smoke'], True)
        self.assertIs(result['serve_encerrado'], True)
        self.assert_all_down()

    def test_failing_path_still_tears_down_what_it_started(self):
        self.smoke_config(paths=['/', '/broken'])
        code, result = self.run_check('smoke')
        self.assertEqual(code, 1)
        self.assertEqual(result['failures'], ['/broken'])
        self.assertIs(result['serve_iniciado_pelo_smoke'], True)
        self.assertIs(result['serve_encerrado'], True)
        self.assert_all_down()

    def test_interruption_still_tears_down_what_it_started(self):
        self.smoke_config()
        with mock.patch.object(checks, 'fetch_status', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_check('smoke')
        self.assert_all_down()
        self.assertFalse((self.root / '.frontlights' / 'serve' / '12.json').exists())

    def test_unknown_target_after_the_start_still_tears_down(self):
        self.smoke_config(target='inexistente')
        code, _ = self.run_check('smoke')
        self.assertEqual(code, 2)
        self.assert_all_down()

    def test_start_failure_is_infrastructure_with_the_masked_error_and_nothing_left_behind(self):
        sleeper = self.tmp / 'sleeper.py'
        sleeper.write_text('import time\ntime.sleep(60)\n', encoding='utf-8')
        port = self.ports['web']
        unhealthy = {'name': 'web', 'argv': [sys.executable, str(sleeper)], 'port': port,
                     'health': f'http://127.0.0.1:{port}/health?k={PASSWORD}', 'timeoutSeconds': 2}
        self.write_config({'smoke': {'paths': ['/'], 'target': 'web'}}, processes=[self.process('api'), unhealthy])
        code, result = self.run_check('smoke')
        self.assertEqual(code, 3, result)
        self.assertEqual(result['classification'], 'infrastructure')
        self.assertEqual(result['infrastructure']['kind'], 'serve_start_failed')
        self.assertTrue(result['infrastructure']['message'])
        self.assertIs(result['serve_iniciado_pelo_smoke'], False)
        self.assertIs(result['serve_encerrado'], False)
        record_text = (self.root / '.frontlights' / 'issues' / '12' / 'checks' / 'smoke.json'
                       ).read_text(encoding='utf-8')
        for text in (self.last_output, record_text):
            self.assertNotIn(PASSWORD, text)
        self.assert_all_down()
        self.assertFalse((self.root / '.frontlights' / 'serve' / '12.json').exists())

    def test_invalid_config_block_is_infrastructure_not_a_traceback(self):
        self.config.write_text(json.dumps({'checks': {'smoke': {'paths': ['/']}}}), encoding='utf-8')
        code, result = self.run_check('smoke')
        self.assertEqual(code, 3, result)
        self.assertEqual(result['infrastructure']['kind'], 'serve_start_failed')

    def test_secrets_are_absent_from_output_and_record_of_a_started_run(self):
        self.smoke_config()
        code, _ = self.run_check('smoke')
        self.assertEqual(code, 0)
        record = (self.root / '.frontlights' / 'issues' / '12' / 'checks' / 'smoke.json').read_text(encoding='utf-8')
        for text in (self.last_output, record):
            self.assertNotIn(PASSWORD, text)
            self.assertNotIn(LOGIN, text)

    def test_integration_still_requires_the_serve_to_be_up(self):
        self.write_config({'integration': {'argv': self.recorder_argv()}})
        code, result = self.run_check('integration')
        self.assertEqual(code, 3)
        self.assertEqual(result['infrastructure']['kind'], 'serve_registry_missing')
        self.assertFalse(port_is_open(self.ports['api']))
        self.assertNotIn('serve_iniciado_pelo_smoke', result)


class EnvironmentCheckRobustnessTest(IntegrationTestCase):
    def assert_infrastructure_json(self, command, kinds):
        code, result = self.run_check(command)
        self.assertEqual(code, 3, result)
        self.assertEqual(result['classification'], 'infrastructure')
        self.assertIn(result['infrastructure']['kind'], kinds)
        self.assertEqual(self.record(command)['classification'], 'infrastructure')
        return result

    def test_malformed_registry_entries_are_infrastructure_not_a_traceback(self):
        pid = self.live_pid()
        base = {'name': 'api', 'pid': pid, 'port': 1, 'startedAt': 'agora', 'identity': serve.UNKNOWN_IDENTITY}
        entries = [dict(base), dict(base, url=123), dict(base, url=None), dict(base, url=['http://127.0.0.1/']),
                   dict(base, url='http://[::1/'), dict(base, url='http://127.0.0.1:abc/'),
                   dict(base, url='http://127.0.0.1:99999/'), dict(base, url='http://\u00e9/'), 'texto']
        self.write_config({'integration': {'argv': self.recorder_argv()}, 'smoke': {'paths': ['/']}})
        for entry in entries:
            self.write_raw_registry([entry])
            for command in ('integration', 'smoke'):
                with self.subTest(entry=entry, command=command):
                    self.assert_infrastructure_json(command, ('serve_registry_invalid', 'invalid_url',
                                                              'non_local_url'))
        self.assertFalse((self.tmp / 'seen.json').exists())

    def test_registry_that_is_not_json_or_has_no_processes_is_infrastructure(self):
        self.write_config({'integration': {'argv': self.recorder_argv()}, 'smoke': {'paths': ['/']}})
        path = self.root / '.frontlights' / 'serve' / '12.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        for content in ('{', '[]', '{"processes": null}', '{"processes": []}', b'\xff\xfe'):
            path.write_bytes(content if isinstance(content, bytes) else content.encode())
            for command in ('integration', 'smoke'):
                with self.subTest(content=content, command=command):
                    self.assert_infrastructure_json(command, ('serve_registry_invalid',))

    def test_smoke_path_that_cannot_be_sent_is_refused_with_valid_json(self):
        for path in ('/\u00e9', '/a\u0001b', '/a\x7fb'):
            self.write_config({'smoke': {'paths': [path]}})
            code, result = self.run_check('smoke')
            self.assertEqual(code, 2, path)
            self.assertIn('checks.smoke.paths', result['error'])

    def test_url_without_http_scheme_is_refused_as_infrastructure(self):
        self.write_config({'integration': {'argv': self.recorder_argv()}, 'smoke': {'paths': ['/']}})
        for url in ('ftp://localhost/', 'file://localhost/', '//127.0.0.1:80/x', '127.0.0.1:80/x', 'ws://127.0.0.1/'):
            self.write_registry(url)
            for command in ('integration', 'smoke'):
                with self.subTest(url=url, command=command):
                    self.assert_infrastructure_json(command, ('non_local_url', 'invalid_url'))
        self.assertFalse((self.tmp / 'seen.json').exists())

    def test_https_is_accepted_and_localhost_is_delivered_as_the_literal_address(self):
        self.write_config({'integration': {'argv': self.recorder_argv()}})
        for url, expected in (('https://127.0.0.1:8443/h', 'https://127.0.0.1:8443'),
                              ('http://localhost:8080/h', 'http://127.0.0.1:8080'),
                              ('http://LOCALHOST/h', 'http://127.0.0.1'),
                              ('http://[::1]:8080/h', 'http://[::1]:8080')):
            self.write_registry(url)
            code, result = self.run_check('integration')
            self.assertEqual(code, 0, url)
            self.assertEqual(self.seen(), {'env': expected, 'arg': expected})
            self.assertEqual(result['backend_url'], expected)


class EnvironmentCheckSecurityTest(IntegrationTestCase):
    NON_LOCAL = ('http://localhost.evil.example/h', 'http://127.0.0.1.evil/h', 'http://127.0.0.1@evil.example/h',
                 'http://[::ffff:127.0.0.1]/h', 'http://127.1/h', 'http://2130706433/h',
                 'http://evil.example/127.0.0.1', 'http://evil.example/?h=localhost')

    def test_hosts_that_only_look_local_are_refused_without_any_request_or_run(self):
        self.write_config({'integration': {'argv': self.recorder_argv()}, 'smoke': {'paths': ['/']}})
        for url in self.NON_LOCAL:
            self.write_registry(url)
            with mock.patch.object(urllib.request.OpenerDirector, 'open') as opened, \
                    mock.patch.object(checks, 'run_process', return_value=(0, '')) as ran:
                for command in ('integration', 'smoke'):
                    code, result = self.run_check(command)
                    self.assertEqual(code, 3, (url, command))
                    self.assertEqual(result['infrastructure']['kind'], 'non_local_url', (url, command))
                opened.assert_not_called()
                ran.assert_not_called()

    def test_environment_proxy_is_ignored(self):
        hits = []

        class Counting(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                self.send_response(200)
                self.send_header('Content-Length', '0')
                self.end_headers()

            def log_message(self, *args):
                pass

        proxy = http.server.HTTPServer(('127.0.0.1', 0), Counting)
        thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(proxy.server_close)
        self.addCleanup(proxy.shutdown)
        address = f'http://127.0.0.1:{proxy.server_address[1]}'
        self.write_config({'smoke': {'paths': ['/', '/health'], 'target': 'web'}})
        self.start_serve()
        env = {name: address for name in ('HTTP_PROXY', 'http_proxy', 'ALL_PROXY', 'all_proxy')}
        with mock.patch.dict(os.environ, env):
            code, result = self.run_check('smoke')
        self.assertEqual(code, 0, result)
        self.assertEqual(hits, [])

    def test_integration_cwd_is_respected_and_outside_the_tree_is_refused(self):
        (self.root / 'sub').mkdir()
        (self.tmp / 'cwdrec.py').write_text(CWD_RECORDER, encoding='utf-8')
        argv = [sys.executable, str(self.tmp / 'cwdrec.py'), str(self.tmp / 'cwd.txt')]
        self.write_config({'integration': {'argv': argv, 'cwd': 'sub'}})
        self.write_registry('http://127.0.0.1:1/h')
        code, _ = self.run_check('integration')
        self.assertEqual(code, 0)
        self.assertEqual(Path((self.tmp / 'cwd.txt').read_text(encoding='utf-8')).resolve(),
                         (self.root / 'sub').resolve())
        (self.tmp / 'cwd.txt').unlink()
        for cwd in ('..', str(self.tmp), 'sub/../../', 'nao-existe'):
            self.write_config({'integration': {'argv': argv, 'cwd': cwd}})
            code, result = self.run_check('integration')
            self.assertEqual(code, 2, cwd)
            self.assertIn('cwd', result['error'])
            self.assertFalse((self.tmp / 'cwd.txt').exists(), cwd)


if __name__ == '__main__':
    unittest.main()
