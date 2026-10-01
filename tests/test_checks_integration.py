import contextlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
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

    def write_config(self, checks_block, processes=None):
        browser = {'processes': processes or [self.process('api'), self.process('web')],
                   'baseUrl': 'http://127.0.0.1:1', 'users': [{'login': LOGIN, 'password': PASSWORD}]}
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
            self.assertEqual(self.seen()['env'], f'http://{host}:8080')


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

    def test_without_serve_registry_it_is_infrastructure_and_says_to_run_serve_start(self):
        self.smoke_config()
        code, result = self.run_check('smoke')
        self.assertEqual(code, 3)
        self.assertEqual(result['infrastructure']['kind'], 'serve_registry_missing')
        self.assertIn('serve start', result['infrastructure']['message'])

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


if __name__ == '__main__':
    unittest.main()
