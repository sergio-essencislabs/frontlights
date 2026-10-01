import contextlib
import ctypes
import http.client
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
import urllib.error
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

    def write_config(self, checks_block, processes=None, users=None, base_url='http://127.0.0.1:1'):
        browser = {'processes': processes or [self.process('api'), self.process('web')],
                   'baseUrl': base_url,
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
            value = {'name': ['path', {'path': 'head'}], 'n': 3}
            self.assertEqual(checks.scrub(value), {'name': ['[oculto]', {'path': '[oculto]'}], 'n': 3})
            self.assertEqual(checks.scrub(checks.scrub(value)), checks.scrub(value))

    def test_each_evidence_key_keeps_its_value(self):
        keys = ('head', 'diff_sha256', 'diff_sha256_vs_base', 'files_sha256', 'timestamp', 'pid', 'port',
                'classification', 'kind', 'backend_url', 'base_url')
        with mock.patch.object(checks, 'USER_SECRETS', ['2026']):
            for key in keys:
                with self.subTest(key=key):
                    self.assertEqual(checks.scrub({key: 'a2026b'}), {key: 'a2026b'})
            self.assertEqual(checks.scrub({'outra': 'a2026b'}), {'outra': 'a[oculto]b'})

    def test_evidence_keys_keep_their_value_and_free_text_is_still_masked(self):
        with mock.patch.object(checks, 'USER_SECRETS', ['2026', 'abcdef']):
            value = {'timestamp': '2026-10-01T00:00:00', 'head': 'abcdef0123', 'nested': {'port': '12026'},
                     'texto': 'ano 2026 e abcdef', 'lista': ['2026']}
            self.assertEqual(checks.scrub(value), {'timestamp': '2026-10-01T00:00:00', 'head': 'abcdef0123',
                                                   'nested': {'port': '12026'},
                                                   'texto': 'ano [oculto] e [oculto]', 'lista': ['[oculto]']})
            self.assertEqual(checks.scrub({'head': 'abcdef'}, skip=()), {'head': '[oculto]'})

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


class SmokeHelpers:
    """Auxiliares dos testes que sobem o serve pelo smoke."""

    def smoke_config(self, **smoke):
        self.write_config({'smoke': {'paths': ['/', '/health'], 'target': 'web', **smoke}})

    def assert_all_down(self):
        for port in self.ports.values():
            for _ in range(50):
                if not port_is_open(port):
                    break
                time.sleep(0.1)
            self.assertFalse(port_is_open(port), port)

    def registry_file(self):
        return self.root / '.frontlights' / 'serve' / '12.json'

    def counter(self, host='127.0.0.1'):
        """Servidor que só conta as requisições recebidas; devolve (porta, lista de caminhos)."""
        hits = []

        class Counting(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                self.send_response(200)
                self.send_header('Content-Length', '0')
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer((host, 0), Counting)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1], hits


class RegistryPidTest(IntegrationTestCase):
    """Pid fora da faixa no registro é infraestrutura, nunca traceback (ctypes.ArgumentError, OverflowError)."""

    def test_invalid_pids_are_infrastructure_for_integration_and_smoke(self):
        self.write_config({'integration': {'argv': self.recorder_argv()}, 'smoke': {'paths': ['/']}})
        for pid in (99999999999, 2 ** 70, 2 ** 31, -1, 0, True, False, 1.5, '12', None):
            self.write_raw_registry([{'name': 'api', 'pid': pid, 'port': 1, 'url': 'http://127.0.0.1:1/health',
                                      'startedAt': 'agora', 'identity': serve.UNKNOWN_IDENTITY}])
            for command in ('integration', 'smoke'):
                with self.subTest(pid=pid, command=command):
                    code, result = self.run_check(command)
                    self.assertEqual(code, 3, result)
                    self.assertEqual(result['infrastructure']['kind'], 'serve_registry_invalid')
                    self.assertEqual(self.record(command)['infrastructure']['kind'], 'serve_registry_invalid')
                    self.assertNotIn('Traceback', self.last_output)
        self.assertFalse((self.tmp / 'seen.json').exists())

    def test_the_largest_valid_pid_is_not_rejected_as_invalid(self):
        self.write_config({'integration': {'argv': self.recorder_argv()}})
        for pid in (1, checks.MAX_PID):
            self.write_raw_registry([{'name': 'api', 'pid': pid, 'port': 1, 'url': 'http://127.0.0.1:1/health',
                                      'startedAt': 'agora', 'identity': serve.UNKNOWN_IDENTITY}])
            with self.subTest(pid=pid):
                code, result = self.run_check('integration')
                self.assertNotEqual(result.get('infrastructure', {}).get('kind'), 'serve_registry_invalid', result)

    def test_a_valid_pid_is_still_accepted(self):
        self.write_config({'integration': {'argv': self.recorder_argv()}})
        self.write_registry('http://127.0.0.1:8080/health')
        self.assertEqual(self.run_check('integration')[0], 0)


class RegistryReadingCaptureTest(IntegrationTestCase):
    """Cada exceção do serve ou da leitura do registro vira infraestrutura classificada."""

    EXCEPTIONS = (KeyError('k'), AttributeError('a'), TypeError('t'), ValueError('v'), UnicodeError('u'),
                  IndexError('i'), RuntimeError('r'), OverflowError('o'), ctypes.ArgumentError('c'))
    SHAPES = ({}, {'processes': 5}, {'processes': None}, {'processes': [None]}, {'processes': ['texto']},
              {'processes': [{'name': 'api', 'url': 'http://127.0.0.1:1/h', 'pid': 1}]})

    def setUp(self):
        super().setUp()
        self.write_config({'integration': {'argv': self.recorder_argv()}, 'smoke': {'paths': ['/']}})
        self.write_registry('http://127.0.0.1:8080/health')

    def test_exceptions_raised_by_serve_status_are_infrastructure(self):
        for error in self.EXCEPTIONS:
            with mock.patch.object(serve, 'status', side_effect=error):
                for command in ('integration', 'smoke'):
                    with self.subTest(error=type(error).__name__, command=command):
                        code, result = self.run_check(command)
                        self.assertEqual(code, 3, result)
                        self.assertEqual(result['infrastructure']['kind'], 'serve_registry_invalid')
                        self.assertEqual(self.record(command)['classification'], 'infrastructure')

    def test_unexpected_status_shapes_are_infrastructure(self):
        for shape in self.SHAPES:
            with mock.patch.object(serve, 'status', return_value=shape):
                for command in ('integration', 'smoke'):
                    with self.subTest(shape=shape, command=command):
                        code, result = self.run_check(command)
                        self.assertEqual(code, 3, result)
                        self.assertEqual(result['infrastructure']['kind'], 'serve_registry_invalid')


class FetchStatusTest(IntegrationTestCase):
    """Cada captura de fetch_status tem um teste que a prova."""

    def fetch_with(self, error):
        with mock.patch.object(urllib.request.OpenerDirector, 'open', side_effect=error):
            return checks.fetch_status('http://127.0.0.1:1/', 1)

    def kind_of(self, error):
        with self.assertRaises(checks.Infra) as caught:
            self.fetch_with(error)
        return caught.exception.kind

    def test_value_and_unicode_errors_are_invalid_url(self):
        for error in (ValueError('v'), UnicodeError('u'), UnicodeEncodeError('idna', 'x', 0, 1, 'r')):
            with self.subTest(error=type(error).__name__):
                self.assertEqual(self.kind_of(error), 'invalid_url')

    def test_timeouts_are_timeout(self):
        for error in (TimeoutError(), urllib.error.URLError(TimeoutError())):
            with self.subTest(error=repr(error)):
                self.assertEqual(self.kind_of(error), 'timeout')

    def test_connection_problems_are_connection_failed(self):
        for error in (urllib.error.URLError(ConnectionRefusedError()), ConnectionResetError(), OSError('x'),
                      http.client.RemoteDisconnected('x'), http.client.BadStatusLine('x')):
            with self.subTest(error=repr(error)):
                self.assertEqual(self.kind_of(error), 'connection_failed')

    def test_http_error_returns_its_status_code(self):
        error = urllib.error.HTTPError('http://127.0.0.1:1/', 404, 'x', {}, io.BytesIO())
        self.assertEqual(self.fetch_with(error), 404)

    def test_smoke_with_an_unbuildable_request_is_infrastructure(self):
        self.write_config({'smoke': {'paths': ['/']}})
        self.write_registry('http://127.0.0.1:8080/health')
        with mock.patch.object(urllib.request.OpenerDirector, 'open', side_effect=ValueError('v')):
            code, result = self.run_check('smoke')
        self.assertEqual((code, result['infrastructure']['kind']), (3, 'invalid_url'))


class DirectHealthyTest(IntegrationTestCase):
    """`direct_healthy` repete a regra do `serve.healthy` (2xx/3xx saudável) sem proxy do ambiente."""

    def healthy_with(self, effect=None, answer=None):
        with mock.patch.object(urllib.request.OpenerDirector, 'open', side_effect=effect, return_value=answer):
            return checks.direct_healthy('http://127.0.0.1:1/health')

    def test_answers_and_errors_are_classified_like_the_serve_does(self):
        def http_error(code):
            return urllib.error.HTTPError('http://127.0.0.1:1/health', code, 'x', {}, io.BytesIO())

        answer = mock.MagicMock()
        answer.__enter__.return_value.status = 200
        self.assertIs(self.healthy_with(answer=answer), True)
        answer.__enter__.return_value.status = 500
        self.assertIs(self.healthy_with(answer=answer), False)
        self.assertIs(self.healthy_with(http_error(302)), True)
        self.assertIs(self.healthy_with(http_error(500)), False)
        for error in (urllib.error.URLError(ConnectionRefusedError()), OSError('x'), ValueError('v'),
                      http.client.RemoteDisconnected('x'), http.client.BadStatusLine('x'),
                      http.client.IncompleteRead(b'')):
            with self.subTest(error=repr(error)):
                self.assertIs(self.healthy_with(error), False)


class AutostartHostTest(SmokeHelpers, IntegrationTestCase):
    """O host de cada health declarado é conferido ANTES de o smoke chamar serve.start."""

    def refused(self, config_processes=None, base_url='http://127.0.0.1:1'):
        self.write_config({'smoke': {'paths': ['/'], 'target': 'api'}}, processes=config_processes,
                          base_url=base_url)
        with mock.patch.object(serve, 'start') as started, \
                mock.patch.object(urllib.request.OpenerDirector, 'open') as opened:
            code, result = self.run_check('smoke')
        started.assert_not_called()
        opened.assert_not_called()
        self.assertEqual((code, result['infrastructure']['kind']), (3, 'non_local_url'), result)
        self.assertIs(result['serve_iniciado_pelo_smoke'], False)
        self.assertFalse(self.registry_file().exists())
        self.assertEqual(self.record('smoke')['infrastructure']['kind'], 'non_local_url')

    def test_non_local_health_hosts_are_refused_without_starting_or_requesting_anything(self):
        for host in ('127.0.0.2', 'evil.example', '127.0.0.1.evil', '0.0.0.0', 'localhost.evil.example'):
            with self.subTest(host=host):
                port = self.ports['api']
                self.refused([dict(self.process('api'), health=f'http://{host}:{port}/health')])

    def test_a_non_local_health_in_any_process_is_refused(self):
        web = dict(self.process('web'), health='http://evil.example/health')
        self.refused([self.process('api'), web])

    def test_a_non_local_base_url_is_refused(self):
        self.refused(base_url='http://evil.example/')

    def test_a_server_on_127_0_0_2_receives_zero_requests_and_nothing_comes_up(self):
        try:
            port, hits = self.counter('127.0.0.2')
        except OSError:
            self.skipTest('127.0.0.2 não está disponível neste sistema')
        self.write_config({'smoke': {'paths': ['/'], 'target': 'api'}},
                          processes=[dict(self.process('api'), health=f'http://127.0.0.2:{port}/health')])
        code, result = self.run_check('smoke')
        self.assertEqual((code, result['infrastructure']['kind']), (3, 'non_local_url'), result)
        self.assertEqual(hits, [])
        self.assertFalse(self.registry_file().exists())
        self.assert_all_down()

    def test_local_hosts_and_a_config_without_browser_test_do_not_trigger_the_check(self):
        self.smoke_config()
        self.assertEqual(self.run_check('smoke')[0], 0)
        self.config.write_text(json.dumps({'checks': {'smoke': {'paths': ['/']}}}), encoding='utf-8')
        code, result = self.run_check('smoke')
        self.assertEqual((code, result['infrastructure']['kind']), (3, 'serve_start_failed'))


class SmokeStartFailureTest(SmokeHelpers, IntegrationTestCase):
    def smoke_with_start(self, **patch):
        self.smoke_config()
        with mock.patch.object(serve, 'start', **patch) as started, \
                mock.patch.object(serve, 'stop', return_value={'ok': True, 'processes': []}) as stopped:
            code, result = self.run_check('smoke')
        return code, result, started, stopped

    def test_failed_start_with_left_processes_tries_one_more_stop(self):
        left = [{'name': 'api', 'pid': 1}]
        code, result, _, stopped = self.smoke_with_start(side_effect=serve.Refusal('falhou', extra={'left': left}))
        self.assertEqual((code, result['infrastructure']['kind']), (3, 'serve_start_failed'))
        stopped.assert_called_once()
        self.assertEqual(stopped.call_args.args[1], 12)
        self.assertNotIn('warnings', result)

    def test_failed_start_without_left_does_not_call_stop(self):
        for error in (serve.Refusal('falhou'), OSError('sem executável'), RuntimeError('inesperado'),
                      KeyError('k'), ValueError('v')):
            with self.subTest(error=type(error).__name__):
                code, result, _, stopped = self.smoke_with_start(side_effect=error)
                self.assertEqual((code, result['infrastructure']['kind']), (3, 'serve_start_failed'), result)
                stopped.assert_not_called()
                self.assertIs(result['serve_iniciado_pelo_smoke'], False)

    def test_start_that_returns_an_unexpected_value_without_a_registry_leaves_no_warning(self):
        for value in ({}, None, {'processes': 5}, {'processes': [None]}, {'processes': [{'pid': 1}]}):
            with self.subTest(value=value):
                code, result, _, stopped = self.smoke_with_start(return_value=value)
                self.assertEqual((code, result['infrastructure']['kind']), (3, 'serve_registry_missing'), result)
                stopped.assert_not_called()
                self.assertIs(result['serve_iniciado_pelo_smoke'], False)
                self.assertNotIn('warnings', result)

    def test_cleanup_stop_that_fails_leaves_a_warning_to_run_serve_stop(self):
        left = [{'name': 'api', 'pid': 1}]
        self.smoke_config()
        with mock.patch.object(serve, 'start', side_effect=serve.Refusal('falhou', extra={'left': left})), \
                mock.patch.object(serve, 'stop', side_effect=serve.Refusal('nem todos', extra={'left': left})):
            code, result = self.run_check('smoke')
        self.assertEqual((code, result['infrastructure']['kind']), (3, 'serve_start_failed'))
        self.assertTrue(any('Rode `serve stop`' in w for w in result['warnings']), result)
        self.assertIs(result['serve_encerrado'], False)


class SmokeStopFailureTest(SmokeHelpers, IntegrationTestCase):
    """Processos que o próprio smoke subiu e não conseguiu derrubar são órfãos e bloqueiam."""

    LEFT = [{'name': 'web', 'pid': 1}]

    def stop_failures(self):
        refusal = serve.Refusal('Nem todos os processos foram encerrados.', extra={'left': self.LEFT})
        return {'refusal com left': dict(side_effect=refusal), 'erro inesperado': dict(side_effect=RuntimeError('x')),
                'oserror': dict(side_effect=OSError('x')),
                'retorno ok falso': dict(return_value={'ok': False, 'left': self.LEFT}),
                'retorno com left': dict(return_value={'ok': True, 'left': self.LEFT, 'processes': []})}

    def run_failing_stop(self, patch, **smoke):
        self.smoke_config(**smoke)
        with mock.patch.object(serve, 'stop', **patch):
            outcome = self.run_check('smoke')
        self.assertTrue(self.registry_file().is_file())  # o registro foi mantido para nova tentativa
        self.serve('stop')  # limpeza real entre uma combinação e outra
        self.assert_all_down()
        return outcome

    def assert_orphan_warning(self, result):
        self.assertIs(result['serve_iniciado_pelo_smoke'], True)
        self.assertIs(result['serve_encerrado'], False)
        self.assertTrue(any('Rode `serve stop`' in w for w in result['warnings']), result)
        record = self.record('smoke')
        self.assertIs(record['serve_encerrado'], False)
        self.assertEqual(record['warnings'], result['warnings'])

    def test_all_paths_passing_but_stop_failing_blocks_as_infrastructure(self):
        for name, patch in self.stop_failures().items():
            with self.subTest(name):
                code, result = self.run_failing_stop(patch)
                self.assertEqual(code, 3, result)
                self.assertIs(result['ok'], False)
                self.assertIs(result['blocking'], True)
                self.assertEqual(result['classification'], 'infrastructure')
                self.assertEqual(result['infrastructure']['kind'], 'serve_stop_failed')
                self.assertEqual([p['status'] for p in result['paths']], [200, 200])
                self.assert_orphan_warning(result)
                self.assertEqual(self.record('smoke')['classification'], 'infrastructure')

    def test_failing_path_and_failing_stop_keep_exit_1_with_the_orphan_warning(self):
        for name, patch in self.stop_failures().items():
            with self.subTest(name):
                code, result = self.run_failing_stop(patch, paths=['/', '/broken'])
                self.assertEqual(code, 1, result)
                self.assertEqual(result['classification'], 'product_failure')
                self.assertEqual(result['failures'], ['/broken'])
                self.assert_orphan_warning(result)

    def test_infrastructure_failure_and_failing_stop_keep_the_original_kind(self):
        problem = checks.Infra('connection_failed', 'sem conexão')
        with mock.patch.object(checks, 'fetch_status', side_effect=problem):
            code, result = self.run_failing_stop(self.stop_failures()['refusal com left'])
        self.assertEqual((code, result['infrastructure']['kind']), (3, 'connection_failed'))
        self.assert_orphan_warning(result)

    def test_unknown_target_after_start_keeps_serve_keys_record_and_orphan_warning(self):
        code, result = self.run_failing_stop(self.stop_failures()['refusal com left'], target='inexistente')
        self.assertEqual(code, 2, result)
        self.assertIn('inexistente', result['error'])
        self.assertIs(result['ok'], False)
        self.assert_orphan_warning(result)

    def test_unknown_target_after_start_with_a_good_stop_reports_the_serve_keys(self):
        self.smoke_config(target='inexistente')
        code, result = self.run_check('smoke')
        self.assertEqual(code, 2)
        self.assertIs(result['serve_iniciado_pelo_smoke'], True)
        self.assertIs(result['serve_encerrado'], True)
        self.assertIs(self.record('smoke')['serve_encerrado'], True)
        self.assertNotIn('warnings', result)
        self.assert_all_down()

    def test_successful_stop_has_no_warning_and_reports_ended(self):
        self.smoke_config()
        code, result = self.run_check('smoke')
        self.assertEqual(code, 0)
        self.assertIs(result['serve_encerrado'], True)
        self.assertNotIn('warnings', result)

    def test_stop_is_not_called_when_the_serve_was_already_running(self):
        self.smoke_config()
        self.start_serve()
        with mock.patch.object(serve, 'stop') as stopped:
            code, result = self.run_check('smoke')
        self.assertEqual(code, 0)
        stopped.assert_not_called()
        self.assertIs(result['serve_encerrado'], False)


class SmokeInterruptionTest(SmokeHelpers, IntegrationTestCase):
    def test_interruption_right_after_serve_start_returns_still_tears_down(self):
        self.smoke_config()
        real_start = serve.start

        def start_then_interrupt(*args, **kwargs):
            real_start(*args, **kwargs)
            raise KeyboardInterrupt

        with mock.patch.object(serve, 'start', side_effect=start_then_interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_check('smoke')
        self.assert_all_down()
        self.assertFalse(self.registry_file().exists())

    def test_interruption_inside_serve_start_leaves_nothing_and_no_stop_warning(self):
        self.smoke_config()
        with mock.patch.object(serve, 'wait_healthy', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_check('smoke')
        self.assert_all_down()
        self.assertFalse(self.registry_file().exists())


class SmokeSharedServeTest(SmokeHelpers, IntegrationTestCase):
    """O serve.stop é por issue: outra sessão pode ter feito stop+start entre a subida e o encerramento."""

    def test_registry_replaced_by_another_session_is_not_torn_down(self):
        self.smoke_config()

        done = []

        def other_session(url, timeout):
            if not done:  # só na primeira requisição
                done.append(True)
                self.assertEqual(self.serve('stop').returncode, 0)
                self.start_serve()
            return 200

        with mock.patch.object(checks, 'fetch_status', side_effect=other_session):
            code, result = self.run_check('smoke')
        self.assertEqual(code, 0, result)
        self.assertIs(result['serve_iniciado_pelo_smoke'], True)
        self.assertIs(result['serve_encerrado'], False)
        self.assertTrue(any('outra sessão' in w for w in result['warnings']), result)
        self.assertFalse(any('Rode `serve stop`' in w for w in result['warnings']), result)
        self.assertTrue(port_is_open(self.ports['web']))
        self.assertTrue(self.registry_file().is_file())

    def test_registry_removed_by_another_session_is_a_warning_not_a_failure(self):
        self.smoke_config()

        done = []

        def other_session(url, timeout):
            if not done:
                done.append(True)
                self.assertEqual(self.serve('stop').returncode, 0)
            return 200

        with mock.patch.object(checks, 'fetch_status', side_effect=other_session):
            code, result = self.run_check('smoke')
        self.assertEqual(code, 0, result)
        self.assertIs(result['serve_encerrado'], False)
        self.assertTrue(any('outra sessão' in w for w in result['warnings']), result)

    def test_partial_registry_of_a_running_start_is_infrastructure_not_a_config_error(self):
        pid = self.live_pid()
        self.write_raw_registry([{'name': 'api', 'pid': pid, 'port': 1, 'url': 'http://127.0.0.1:1/health',
                                  'startedAt': 'agora', 'identity': serve.process_identity(pid)
                                  or serve.UNKNOWN_IDENTITY}])
        self.write_config({'integration': {'argv': self.recorder_argv()}, 'backend': 'web',
                           'smoke': {'paths': ['/'], 'target': 'web'}})
        for command in ('smoke', 'integration'):
            with self.subTest(command=command):
                code, result = self.run_check(command)
                self.assertEqual(code, 3, result)
                self.assertEqual(result['infrastructure']['kind'], 'serve_registry_partial')
                self.assertEqual(self.record(command)['infrastructure']['kind'], 'serve_registry_partial')
        self.assertTrue(serve.pid_alive(pid))
        self.assertTrue(self.registry_file().is_file())
        self.assertFalse((self.tmp / 'seen.json').exists())

    def test_target_that_the_config_does_not_declare_is_still_a_config_error(self):
        self.smoke_config(target='inexistente')
        self.start_serve()
        code, result = self.run_check('smoke')
        self.assertEqual(code, 2)
        self.assertIn('inexistente', result['error'])

    def test_using_a_running_serve_warns_about_the_hard_kill_limit(self):
        self.smoke_config()
        self.start_serve()
        code, result = self.run_check('smoke')
        self.assertEqual(code, 0)
        self.assertIs(result['serve_iniciado_pelo_smoke'], False)
        self.assertTrue(any('à força' in w and 'serve stop' in w for w in result['warnings']), result)
        self.assertEqual(self.record('smoke')['warnings'], result['warnings'])

    def test_the_hard_kill_limit_is_documented(self):
        self.assertIn('à força', checks.__doc__)
        self.assertIn('à força', checks.run_smoke.__doc__)


class SmokeProxyTest(SmokeHelpers, IntegrationTestCase):
    """O health do autostart (serve.healthy) não pode passar por proxy do ambiente."""

    def test_autostart_in_a_process_started_with_proxy_variables_never_touches_the_proxy(self):
        port, hits = self.counter()
        address = f'http://127.0.0.1:{port}'
        self.smoke_config()
        env = {k: v for k, v in os.environ.items() if k.upper() != 'NO_PROXY'}
        env.update({name: address for name in ('HTTP_PROXY', 'http_proxy', 'ALL_PROXY', 'all_proxy')})
        done = subprocess.run([sys.executable, str(SCRIPTS / 'checks.py'), 'smoke', '--config', str(self.config),
                               '--root', str(self.root), '--issue', '12'], capture_output=True, text=True,
                              encoding='utf-8', env=env, timeout=120)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(hits, [])
        self.assert_all_down()

    def test_serve_health_check_is_restored_after_the_start(self):
        original = serve.healthy
        self.smoke_config()
        self.assertEqual(self.run_check('smoke')[0], 0)
        self.assertIs(serve.healthy, original)
        with mock.patch.object(serve, 'start', side_effect=RuntimeError('x')):
            self.run_check('smoke')
        self.assertIs(serve.healthy, original)

    def test_the_environment_of_the_started_processes_is_not_changed(self):
        before = dict(os.environ)
        self.smoke_config()
        self.run_check('smoke')
        self.assertEqual(dict(os.environ), before)


class EvidenceFieldsTest(SmokeHelpers, IntegrationTestCase):
    """A máscara só vale para texto livre: head, hashes, horário, porta e classificação ficam exatos."""

    def setUp(self):
        super().setUp()
        self.base = self.tmp / 'base'
        subprocess.run(['git', 'clone', '-q', str(self.root), str(self.base)], check=True, env=GIT_ENV)
        self.head = subprocess.run(['git', '-C', str(self.root), 'rev-parse', 'HEAD'], capture_output=True,
                                   text=True, check=True).stdout.strip()
        self.blocks = {'integration': {'argv': self.recorder_argv()},
                       'smoke': {'paths': ['/', '/health'], 'target': 'web'}}
        self.write_config(self.blocks)
        self.start_serve()  # com o serve no ar, a senha especial não passa pelo registro do serve

    def passwords(self):
        return {'ano': '2026', 'head': self.head[:6], 'porta api': str(self.ports['api']),
                'porta web': str(self.ports['web'])}

    def test_evidence_fields_stay_exact_whatever_the_password(self):
        for label, password in self.passwords().items():
            self.write_config(self.blocks, users=[{'login': LOGIN, 'password': password}])
            for command, port_key in (('integration', 'api'), ('smoke', 'web')):
                with self.subTest(password=label, command=command):
                    code, result = self.run_check(command, extra=['--base', str(self.base)])
                    self.assertEqual(code, 0, result)
                    for shown in (result, self.record(command)):
                        self.assertEqual(shown['head'], self.head)
                        self.assertRegex(shown['diff_sha256'], '^[0-9a-f]{64}$')
                        self.assertRegex(shown['diff_sha256_vs_base'], '^[0-9a-f]{64}$')
                        self.assertRegex(shown['files_sha256'], '^[0-9a-f]{64}$')
                        self.assertRegex(shown['timestamp'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d')
                        self.assertIn(shown['classification'], ('passed', 'healthy'))
                        url = shown['backend_url'] if command == 'integration' else shown['base_url']
                        self.assertEqual(url, f'http://127.0.0.1:{self.ports[port_key]}')

    def test_the_secret_is_still_hidden_in_free_text_argv_and_errors(self):
        for label, password in self.passwords().items():
            self.write_config({**self.blocks, 'integration': {'argv': self.recorder_argv() + [password]}},
                              users=[{'login': LOGIN, 'password': password}])
            with self.subTest(password=label):
                _, result = self.run_check('integration')
                self.assertEqual(result['argv'][-1], '[oculto]')
                self.assertEqual(self.record('integration')['argv'][-1], '[oculto]')
                self.write_config({'smoke': {'paths': ['/'], 'target': password}},
                                  users=[{'login': LOGIN, 'password': password}])
                code, result = self.run_check('smoke')
                self.assertEqual(code, 2)
                self.assertNotIn(password, result['error'])
                self.assertIn('[oculto]', result['error'])

    def test_regression_records_keep_their_evidence_exact_too(self):
        (self.tmp / 'suite.py').write_text(FAKE_SUITE, encoding='utf-8')
        for label, password in self.passwords().items():
            self.write_config({'regression': {'argv': [sys.executable, str(self.tmp / 'suite.py')]}},
                              users=[{'login': LOGIN, 'password': password}])
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = checks.main(['regression', '--config', str(self.config), '--root', str(self.root),
                                    '--base', str(self.base), '--issue', '12'])
            with self.subTest(password=label):
                self.assertEqual(code, 0, out.getvalue())
                shown = json.loads(out.getvalue())
                for side in ('base', 'branch'):
                    self.assertEqual(shown[side]['head'], self.head)
                    self.assertRegex(shown[side]['diff_sha256'], '^[0-9a-f]{64}$')


if __name__ == '__main__':
    unittest.main()
