import contextlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import serve  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'serve.py'
PASSWORD = 'senha-fake'
LOGIN = 'usuario1@exemplo.test'
NEWLINE = chr(10)


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


PARENT_SCRIPT = r"""import http.server
import subprocess
import sys

GRANDCHILD = (
    'import sys, time\n'
    'while True:\n'
    '    open(sys.argv[1], "w").write(str(time.time()))\n'
    '    time.sleep(0.1)\n'
)
subprocess.Popen([sys.executable, '-c', GRANDCHILD, sys.argv[1]])
http.server.test(HandlerClass=http.server.SimpleHTTPRequestHandler, port=int(sys.argv[2]), bind='127.0.0.1')
"""


ANY_ARGS_SERVER = r"""import http.server
import sys

http.server.test(HandlerClass=http.server.SimpleHTTPRequestHandler, port=int(sys.argv[1]), bind='127.0.0.1')
"""

STATUS_SERVER = r"""import http.server
import sys

CODE = int(sys.argv[2])


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(CODE)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def log_message(self, *args):
        pass


http.server.HTTPServer(('127.0.0.1', int(sys.argv[1])), Handler).serve_forever()
"""


def reachable(url):
    try:
        urllib.request.urlopen(url, timeout=2).close()
        return True
    except OSError:
        return False


def wait_until(condition, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.1)
    return condition()


def http_server(port):
    return [sys.executable, '-m', 'http.server', str(port), '--bind', '127.0.0.1']


class ServeTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'worktree'
        self.root.mkdir()
        self.config = Path(self.tmp.name) / 'config.json'
        self.addCleanup(self.serve, 'stop')

    def write_config(self, processes, login=LOGIN, password=PASSWORD, path=None):
        block = {'processes': processes, 'baseUrl': 'http://127.0.0.1:1',
                 'users': [{'login': login, 'password': password}]}
        (path or self.config).write_text(json.dumps({'repository': 'OWNER/REPOSITORY', 'roads': None,
                                           'browserTest': block}), encoding='utf-8')

    def web(self, name, port=None, **extra):
        port = port or free_port()
        entry = {'name': name, 'argv': http_server(port), 'port': port,
                 'health': f'http://127.0.0.1:{port}/', 'timeoutSeconds': 20}
        entry.update(extra)
        return entry

    def serve(self, command, issue=9):
        done = subprocess.run([sys.executable, str(SCRIPT), command, '--config', str(self.config),
                               '--root', str(self.root), '--issue', str(issue)],
                              capture_output=True, text=True, encoding='utf-8', timeout=120)
        self.last = done
        return done

    def serve_in_background(self, config, issue=9):
        return subprocess.Popen([sys.executable, str(SCRIPT), 'start', '--config', str(config),
                                 '--root', str(self.root), '--issue', str(issue)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8')

    def payload(self, done):
        return json.loads(done.stdout)

    def registry(self, issue=9):
        return self.root / '.frontlights' / 'serve' / f'{issue}.json'


class StartTest(ServeTestCase):
    def test_start_brings_up_every_process_and_prints_url_port_and_pid(self):
        first, second = self.web('web'), self.web('api')
        self.write_config([first, second])
        done = self.serve('start')
        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
        result = self.payload(done)
        self.assertTrue(result['ok'])
        by_name = {item['name']: item for item in result['processes']}
        self.assertEqual(set(by_name), {'web', 'api'})
        self.assertEqual(by_name['web']['port'], first['port'])
        self.assertEqual(by_name['web']['url'], first['health'])
        self.assertIsInstance(by_name['api']['pid'], int)
        self.assertTrue(self.registry().is_file())


class StopTest(ServeTestCase):
    def test_stop_kills_the_process_and_removes_the_registry(self):
        entry = self.web('web')
        self.write_config([entry])
        self.assertEqual(self.serve('start').returncode, 0)
        self.assertTrue(reachable(entry['health']))
        done = self.serve('stop')
        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
        self.assertTrue(self.payload(done)['ok'])
        self.assertFalse(self.registry().exists())
        self.assertTrue(wait_until(lambda: not reachable(entry['health'])))

    def test_stop_kills_a_grandchild_spawned_by_the_process(self):
        port = free_port()
        beat = Path(self.tmp.name) / 'beat.txt'
        script = Path(self.tmp.name) / 'parent.py'
        script.write_text(PARENT_SCRIPT, encoding='utf-8')
        entry = {'name': 'web', 'argv': [sys.executable, str(script), str(beat), str(port)], 'port': port,
                 'health': f'http://127.0.0.1:{port}/', 'timeoutSeconds': 20}
        self.write_config([entry])
        self.assertEqual(self.serve('start').returncode, 0, self.last.stdout)
        self.assertTrue(wait_until(beat.exists))
        before = beat.read_text()
        self.assertTrue(wait_until(lambda: beat.read_text() != before))
        self.assertEqual(self.serve('stop').returncode, 0, self.last.stdout)
        time.sleep(0.5)
        frozen = beat.read_text()
        time.sleep(1)
        self.assertEqual(beat.read_text(), frozen, 'o neto continuou vivo depois do stop')


class DuplicateStartTest(ServeTestCase):
    def test_a_second_start_is_refused_and_the_first_stays_manageable(self):
        entry = self.web('web')
        self.write_config([entry])
        first = self.payload(self.serve('start'))
        before = self.registry().read_text(encoding='utf-8')
        done = self.serve('start')
        self.assertEqual(done.returncode, 1, done.stdout)
        result = self.payload(done)
        self.assertFalse(result['ok'])
        self.assertIn('stop', result['error'])
        self.assertEqual(self.registry().read_text(encoding='utf-8'), before)
        self.assertTrue(reachable(entry['health']))
        self.assertEqual(self.payload(self.serve('status'))['processes'][0]['pid'], first['processes'][0]['pid'])
        self.assertEqual(self.serve('stop').returncode, 0)
        self.assertTrue(wait_until(lambda: not reachable(entry['health'])))

    def test_a_record_with_only_dead_pids_is_replaced(self):
        entry = self.web('web')
        self.write_config([entry])
        self.assertEqual(self.serve('start').returncode, 0)
        old = self.payload(self.serve('status'))['processes'][0]['pid']
        serve.kill_tree(old)
        self.assertTrue(wait_until(lambda: not serve.pid_alive(old)))
        done = self.serve('start')
        self.assertEqual(done.returncode, 0, done.stdout)
        self.assertNotEqual(self.payload(done)['processes'][0]['pid'], old)


class IdentityTest(ServeTestCase):
    def foreign(self):
        other = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
        self.addCleanup(other.wait)
        self.addCleanup(other.kill)
        self.assertTrue(wait_until(lambda: serve.pid_alive(other.pid)))
        return other

    def record(self, **extra):
        entry = {'name': 'web', 'pid': self.other.pid, 'port': 1, 'url': 'http://127.0.0.1:1/'}
        entry.update(extra)
        self.registry().parent.mkdir(parents=True, exist_ok=True)
        self.registry().write_text(json.dumps({'issue': 9, 'root': str(self.root), 'processes': [entry]}),
                                   encoding='utf-8')

    def test_stop_does_not_kill_a_live_pid_that_belongs_to_another_process(self):
        self.write_config([self.web('web')])
        self.other = self.foreign()
        self.record(identity='identidade-de-outro-processo')
        done = self.serve('stop')
        self.assertTrue(serve.pid_alive(self.other.pid), 'stop matou um processo alheio: ' + done.stdout)
        self.assertEqual(done.returncode, 0, done.stdout)
        self.assertFalse(self.registry().exists())

    def test_stop_refuses_to_kill_when_the_identity_is_unknown(self):
        self.write_config([self.web('web')])
        self.other = self.foreign()
        self.record()
        done = self.serve('stop')
        self.assertTrue(serve.pid_alive(self.other.pid), 'stop matou sem conferir a identidade')
        self.assertEqual(done.returncode, 1)
        self.assertIn('dentidade desconhecida', self.payload(done)['error'])
        self.assertTrue(self.registry().exists())

    def test_start_records_the_identity_of_each_process(self):
        self.write_config([self.web('web')])
        self.assertEqual(self.serve('start').returncode, 0)
        record = json.loads(self.registry().read_text(encoding='utf-8'))
        self.assertTrue(record['processes'][0]['identity'])


class StatusTest(ServeTestCase):
    def test_status_reports_alive_then_dead_from_the_registry_on_disk(self):
        self.write_config([self.web('web')])
        pid = self.payload(self.serve('start'))['processes'][0]['pid']
        done = self.serve('status')
        self.assertEqual(done.returncode, 0, done.stdout)
        result = self.payload(done)
        self.assertTrue(result['running'])
        self.assertEqual(result['processes'][0]['pid'], pid)
        self.assertTrue(result['processes'][0]['alive'])
        subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'] if os.name == 'nt' else ['kill', '-9', str(pid)],
                       capture_output=True)
        self.assertTrue(wait_until(lambda: not self.payload(self.serve('status'))['running']))

    def test_status_without_a_registry_fails_with_a_clear_message(self):
        self.write_config([self.web('web')])
        done = self.serve('status')
        self.assertEqual(done.returncode, 1)
        self.assertFalse(self.payload(done)['ok'])
        self.assertIn('registro', self.payload(done)['error'])


class FailureTest(ServeTestCase):
    def test_a_process_that_never_gets_healthy_tears_down_the_ones_already_up(self):
        good = self.web('web')
        broken = self.web('api', health='http://127.0.0.1:%d/' % free_port(), timeoutSeconds=2)
        self.write_config([good, broken])
        done = self.serve('start')
        self.assertEqual(done.returncode, 1)
        result = self.payload(done)
        self.assertFalse(result['ok'])
        self.assertEqual(result['failedProcess'], 'api')
        self.assertEqual(result['category'], 'infraestrutura')
        self.assertTrue(wait_until(lambda: not reachable(good['health'])))
        self.assertFalse(self.registry().exists())

    def test_a_process_that_exits_early_is_reported_as_the_failing_one(self):
        crashing = {'name': 'web', 'argv': [sys.executable, '-c', 'raise SystemExit(3)'],
                    'health': f'http://127.0.0.1:{free_port()}/', 'timeoutSeconds': 20}
        self.write_config([crashing])
        done = self.serve('start')
        self.assertEqual(done.returncode, 1)
        self.assertEqual(self.payload(done)['failedProcess'], 'web')
        self.assertFalse(self.registry().exists())

    def test_a_missing_executable_is_reported_as_an_infrastructure_failure(self):
        self.write_config([{'name': 'web', 'argv': ['executavel-que-nao-existe-xyz'],
                            'health': f'http://127.0.0.1:{free_port()}/'}])
        done = self.serve('start')
        self.assertEqual(done.returncode, 1)
        result = self.payload(done)
        self.assertEqual((result['failedProcess'], result['category']), ('web', 'infraestrutura'))

    def test_a_health_endpoint_answering_500_or_404_is_not_healthy(self):
        script = Path(self.tmp.name) / 'status_server.py'
        script.write_text(STATUS_SERVER, encoding='utf-8')
        for code in (500, 404):
            with self.subTest(code=code):
                port = free_port()
                self.write_config([{'name': 'web', 'argv': [sys.executable, str(script), str(port), str(code)],
                                    'health': f'http://127.0.0.1:{port}/', 'timeoutSeconds': 2}])
                done = self.serve('start')
                self.assertEqual(done.returncode, 1, done.stdout)
                result = self.payload(done)
                self.assertEqual(result['failedProcess'], 'web')
                self.assertIn('saudável', result['error'])
                self.assertFalse(self.registry().exists())
                self.assertTrue(wait_until(lambda: not reachable(f'http://127.0.0.1:{port}/')))

    def test_a_failing_kill_during_rollback_does_not_leave_the_others_running(self):
        good, other = self.web('web'), self.web('api')
        broken = self.web('worker', health='http://127.0.0.1:%d/' % free_port(), timeoutSeconds=1)
        self.write_config([good, other, broken])
        real, calls = serve.kill_tree, []

        def flaky(pid):
            calls.append(pid)
            if len(calls) == 1:
                raise OSError('falha simulada')
            real(pid)

        with mock.patch.object(serve, 'kill_tree', flaky):
            with self.assertRaises(serve.Refusal) as caught:
                serve.start(str(self.config), str(self.root), 9)
        for pid in calls:
            real(pid)
        self.assertEqual(len(calls), 3)
        self.assertIn('worker', str(caught.exception))
        self.assertIn(str(calls[0]), str(caught.exception))
        self.assertTrue(wait_until(lambda: not reachable(good['health']) and not reachable(other['health'])))

    @unittest.skipUnless(os.name == 'nt', 'cmd.exe só existe no Windows')
    def test_batch_files_refuse_shell_metacharacters_in_argv(self):
        marker = Path(self.tmp.name) / 'marker.txt'
        script = Path(self.tmp.name) / 'fake.cmd'
        script.write_text('@echo off' + NEWLINE + f'echo ran > "{marker}"' + NEWLINE, encoding='utf-8')
        for bad in ('a&b', 'a|b', 'a^b', '%PATH%', 'a<b', 'a>b', 'a!b', 'a"b'):
            with self.subTest(argument=bad):
                self.write_config([{'name': 'web', 'argv': [str(script), bad],
                                    'health': f'http://127.0.0.1:{free_port()}/', 'timeoutSeconds': 1}])
                done = self.serve('start')
                self.assertEqual(done.returncode, 1, done.stdout)
                result = self.payload(done)
                self.assertEqual(result['failedProcess'], 'web')
                self.assertIn('metacaracteres', result['error'])
        time.sleep(0.5)
        self.assertFalse(marker.exists(), 'o .cmd foi executado com argumento perigoso')

    def test_argv_must_be_a_list_never_a_shell_string(self):
        self.write_config([{'name': 'web', 'argv': 'npm run watch',
                            'health': f'http://127.0.0.1:{free_port()}/'}])
        done = self.serve('start')
        self.assertEqual(done.returncode, 1)
        self.assertIn('argv', self.payload(done)['error'])


class SecretTest(ServeTestCase):
    def test_login_and_password_inside_the_health_url_and_argv_stay_out_of_registry_and_output(self):
        port = free_port()
        script = Path(self.tmp.name) / 'any_args_server.py'
        script.write_text(ANY_ARGS_SERVER, encoding='utf-8')
        health = f'http://127.0.0.1:{port}/?u={LOGIN}&p={PASSWORD}'
        self.write_config([{'name': 'web', 'port': port, 'timeoutSeconds': 20, 'health': health,
                            'argv': [sys.executable, str(script), str(port), '--user', LOGIN, '--pass', PASSWORD]}])
        outputs = []
        done = self.serve('start')
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        outputs += [done.stdout, done.stderr, self.registry().read_text(encoding='utf-8')]
        done = self.serve('status')
        outputs += [done.stdout, done.stderr]
        everything = NEWLINE.join(outputs)
        self.assertNotIn(PASSWORD, everything)
        self.assertNotIn(LOGIN, everything)
        self.assertIn('redacted', everything)

    def tree_text(self):
        found = []
        for base in (self.root, Path(self.tmp.name)):
            for path in base.rglob('*'):
                if path.is_file() and path != self.config:
                    found.append(path.read_text(encoding='utf-8', errors='replace'))
        return NEWLINE.join(found)

    def test_login_and_password_never_reach_the_output_or_the_files(self):
        broken = self.web('api', health='http://127.0.0.1:%d/' % free_port(), timeoutSeconds=1)
        self.write_config([self.web('web')])
        outputs = []
        for command in ('start', 'status', 'status'):
            done = self.serve(command)
            outputs += [done.stdout, done.stderr]
        outputs += [self.tree_text()]
        self.serve('stop')
        self.write_config([broken])
        done = self.serve('start')
        outputs += [done.stdout, done.stderr]
        # a refusal that would quote the secret (here through the process name) must be redacted
        self.write_config([{'name': 'web-' + PASSWORD, 'argv': ['executavel-que-nao-existe-xyz'],
                            'health': f'http://127.0.0.1:{free_port()}/'}])
        done = self.serve('start')
        outputs += [done.stdout, done.stderr, self.tree_text()]
        everything = NEWLINE.join(outputs)
        self.assertIn('"ok"', everything)
        self.assertNotIn(PASSWORD, everything)
        self.assertNotIn(LOGIN, everything)


class SecretMaskingTest(ServeTestCase):
    def test_a_login_or_password_shorter_than_four_characters_is_refused_before_starting_anything(self):
        entry = self.web('web')
        for login, password in (('abc', PASSWORD), (LOGIN, 'xy'), ('a', 'e')):
            with self.subTest(login=login, password=password):
                self.write_config([entry], login=login, password=password)
                done = self.serve('start')
                self.assertEqual(done.returncode, 1, done.stdout)
                result = self.payload(done)
                self.assertEqual(result['category'], 'uso')
                self.assertIn('mascar', result['error'])
                self.assertFalse(self.registry().exists())
                self.assertFalse(reachable(entry['health']), 'um processo subiu apesar da recusa')
                self.serve('stop')

    def test_a_secret_that_is_a_prefix_of_the_mask_keeps_masking_idempotent_and_a_second_pass_does_not_grow_the_text(self):
        for secret in ('[redact', '[redacted', '[', '[r'):
            with self.subTest(secret=secret):
                serve._SECRETS[:] = [secret]
                self.addCleanup(serve._SECRETS.clear)
                text = 'a ' + secret + ' b [redacted] c'
                once = serve.protect(text)
                self.assertEqual(serve.protect(once), once)
                self.assertEqual(len(serve.protect(once)), len(once))

    def test_a_login_or_password_containing_the_mask_text_is_refused_before_starting_anything(self):
        entry = self.web('web')
        for login, password in ((LOGIN, '[redacted]abc'), (LOGIN, 'abc[redacted]'),
                                ('[redacted]abc', PASSWORD), ('abc[redacted]', PASSWORD)):
            with self.subTest(login=login, password=password):
                try:
                    self.write_config([entry], login=login, password=password)
                    done = self.serve('start')
                    self.assertEqual(done.returncode, 1, done.stdout)
                    result = self.payload(done)
                    self.assertEqual(result['category'], 'uso')
                    self.assertIn('marcador de máscara', result['error'])
                    self.assertFalse(self.registry().exists())
                    self.assertFalse(reachable(entry['health']), 'um processo subiu apesar da recusa')
                finally:
                    self.serve('stop')

    def test_a_secret_that_is_a_substring_of_a_key_keeps_the_structure_and_the_registry_readable(self):
        for secret in ('name', 'port', 'dact', 'redacted'):
            with self.subTest(secret=secret):
                entry = self.web('web')
                self.write_config([entry], login='usuario1@exemplo.test', password=secret)
                done = self.serve('start')
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                started = self.payload(done)
                self.assertEqual(set(started['processes'][0]),
                                 {'name', 'pid', 'port', 'url', 'startedAt', 'identity'})
                registry = json.loads(self.registry().read_text(encoding='utf-8'))
                self.assertIn('name', registry['processes'][0])
                status = self.serve('status')
                self.assertEqual(status.returncode, 0, status.stdout)
                self.assertTrue(self.payload(status)['running'])
                self.assertLess(len(status.stdout), 1500, 'a saída cresceu além do esperado')
                done = self.serve('stop')
                self.assertEqual(done.returncode, 0, done.stdout)
                self.assertTrue(wait_until(lambda: not reachable(entry['health'])))

    def test_masking_is_one_pass_and_never_grows_the_text(self):
        serve._SECRETS[:] = ['dact', 'e', 'name']
        self.addCleanup(serve._SECRETS.clear)
        once = serve.protect('name=dact e')
        self.assertEqual(serve.protect(once), once)
        self.assertEqual(serve.scrub({'name': 'dact'}), {'name': '[redacted]'})

    def test_percent_encoded_forms_of_login_and_password_stay_out_of_registry_and_output(self):
        login, password = 'ana@exemplo.test', 'p@ss w+rd'
        port = free_port()
        script = Path(self.tmp.name) / 'any_args_server.py'
        script.write_text(ANY_ARGS_SERVER, encoding='utf-8')
        health = f'http://127.0.0.1:{port}/?u=ana%40exemplo.test&p=p%40ss+w%2Brd&q=p%40ss%20w%2Brd'
        self.write_config([{'name': 'web', 'port': port, 'timeoutSeconds': 20, 'health': health,
                            'argv': [sys.executable, str(script), str(port)]}], login=login, password=password)
        done = self.serve('start')
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        outputs = [done.stdout, done.stderr, self.registry().read_text(encoding='utf-8')]
        done = self.serve('status')
        outputs += [done.stdout, done.stderr]
        everything = NEWLINE.join(outputs)
        for form in ('ana%40exemplo.test', 'p%40ss+w%2Brd', 'p%40ss%20w%2Brd'):
            self.assertNotIn(form, everything)
        self.assertIn('redacted', everything)


class LockTest(ServeTestCase):
    def lock(self):
        return self.root / '.frontlights' / 'serve' / '9.lock'

    def test_two_concurrent_starts_for_the_same_issue_leave_exactly_one_winner_and_no_orphan(self):
        first, second = self.web('web'), self.web('api')
        other_config = Path(self.tmp.name) / 'other.json'
        self.write_config([first])
        self.write_config([second], path=other_config)
        runs = [self.serve_in_background(self.config), self.serve_in_background(other_config)]
        outputs = [run.communicate(timeout=120) for run in runs]
        self.assertEqual(sorted(run.returncode for run in runs), [0, 1], outputs)
        winner = first if runs[0].returncode == 0 else second
        loser = second if winner is first else first
        record = json.loads(self.registry().read_text(encoding='utf-8'))
        self.assertEqual([entry['port'] for entry in record['processes']], [winner['port']])
        self.assertTrue(reachable(winner['health']))
        self.assertFalse(reachable(loser['health']), 'o start recusado deixou um processo órfão')
        self.assertFalse(self.lock().exists())

    def test_a_stale_lock_of_a_dead_owner_is_replaced_and_removed_at_the_end(self):
        gone = subprocess.Popen([sys.executable, '-c', 'pass'])
        gone.wait()
        self.lock().parent.mkdir(parents=True)
        self.lock().write_text(str(gone.pid), encoding='utf-8')
        self.write_config([self.web('web')])
        done = self.serve('start')
        self.assertEqual(done.returncode, 0, done.stdout)
        self.assertFalse(self.lock().exists())

    def test_an_unreadable_lock_refuses_and_names_the_file_to_delete(self):
        self.lock().parent.mkdir(parents=True)
        self.lock().write_text('lixo', encoding='utf-8')
        entry = self.web('web')
        self.write_config([entry])
        done = self.serve('start')
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn('9.lock', self.payload(done)['error'])
        self.assertIn('apague', self.payload(done)['error'])
        self.assertFalse(reachable(entry['health']))
        self.assertTrue(self.lock().exists())

    def test_the_registry_is_written_as_each_process_starts(self):
        slow = self.web('slow', health='http://127.0.0.1:%d/' % free_port(), timeoutSeconds=30)
        self.write_config([self.web('web'), slow])
        run = self.serve_in_background(self.config)
        self.addCleanup(run.communicate)
        self.addCleanup(run.kill)
        self.assertTrue(wait_until(lambda: self.registry().is_file()), 'o registro não apareceu durante o start')
        self.assertEqual(self.serve('status').returncode, 0)
        run.kill()
        run.communicate()
        self.assertEqual(self.serve('stop').returncode, 0, self.last.stdout)

    def test_a_corrupt_registry_message_names_the_file_to_delete(self):
        self.write_config([self.web('web')])
        self.registry().parent.mkdir(parents=True, exist_ok=True)
        self.registry().write_text('{quebrado', encoding='utf-8')
        for command in ('status', 'start'):
            done = self.serve(command)
            self.assertEqual(done.returncode, 1)
            self.assertIn('9.json', self.payload(done)['error'])
            self.assertIn('apague', self.payload(done)['error'])


class KillVerificationTest(ServeTestCase):
    def test_stop_reports_what_did_not_die_when_the_kill_returns_without_killing(self):
        survivor = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
        self.addCleanup(survivor.wait)
        self.addCleanup(survivor.kill)
        self.assertTrue(wait_until(lambda: serve.pid_alive(survivor.pid)))
        self.registry().parent.mkdir(parents=True)
        self.registry().write_text(json.dumps({'issue': 9, 'root': str(self.root), 'processes': [
            {'name': 'web', 'pid': survivor.pid, 'port': 1, 'url': 'http://127.0.0.1:1/',
             'identity': serve.process_identity(survivor.pid)}]}), encoding='utf-8')
        out = io.StringIO()
        with mock.patch.object(serve, '_kill_signal', lambda pid: None), \
                mock.patch.object(serve, 'KILL_WAIT_SECONDS', 0.3), contextlib.redirect_stdout(out):
            code = serve.main(['stop', '--config', str(self.config), '--root', str(self.root), '--issue', '9'])
        result = json.loads(out.getvalue())
        self.assertEqual(code, 1)
        self.assertFalse(result['ok'])
        self.assertEqual([item['pid'] for item in result['left']], [survivor.pid])
        self.assertFalse(result['processes'][0]['stopped'])
        self.assertTrue(self.registry().exists())

    def test_a_failed_rollback_kill_is_reported_in_left_and_keeps_the_record_for_stop(self):
        good = self.web('web')
        broken = self.web('worker', health='http://127.0.0.1:%d/' % free_port(), timeoutSeconds=1)
        self.write_config([good, broken])
        with mock.patch.object(serve, '_kill_signal', lambda pid: None), \
                mock.patch.object(serve, 'KILL_WAIT_SECONDS', 0.3):
            with self.assertRaises(serve.Refusal) as caught:
                serve.start(str(self.config), str(self.root), 9)
        self.assertEqual(len(caught.exception.extra['left']), 2)
        self.assertTrue(self.registry().exists())
        self.assertEqual(self.serve('stop').returncode, 0, self.last.stdout)
        self.assertTrue(wait_until(lambda: not reachable(good['health'])))


if __name__ == '__main__':
    unittest.main()
