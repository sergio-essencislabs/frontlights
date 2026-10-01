import json
import os
import re
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

# Fake server: the port comes from argv[1] when given, else from PORT. It answers with what it received.
FAKE_SERVER = r"""import http.server
import os
import sys

port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ['PORT'])
body = '|'.join([str(port), os.environ.get('PORT', ''), os.environ.get('FRONTLIGHTS_PORT_WEB_API', '')]).encode()


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


http.server.HTTPServer(('127.0.0.1', port), Handler).serve_forever()
"""

# Fake server that IGNORES the reserved port: it listens on a fixed port given in argv[1].
IGNORING_SERVER = FAKE_SERVER.replace("port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ['PORT'])",
                                      'port = int(sys.argv[1])')


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def fetch(url):
    with urllib.request.urlopen(url, timeout=5) as answer:
        return answer.read().decode()


def reachable(url):
    try:
        fetch(url)
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


class PortsTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / 'worktree'
        self.root.mkdir()
        self.script = self.base / 'fake_server.py'
        self.script.write_text(FAKE_SERVER, encoding='utf-8')
        self.config = self.base / 'config.json'
        self.addCleanup(self.stop_everything)
        self.touched = [(self.root, 11)]

    def stop_everything(self):
        for root, issue in self.touched:
            subprocess.run([sys.executable, str(SCRIPT), 'stop', '--config', str(self.config), '--root', str(root),
                            '--issue', str(issue)], capture_output=True, timeout=120)

    def write_config(self, processes, path=None):
        block = {'processes': processes, 'baseUrl': 'http://127.0.0.1:1',
                 'users': [{'login': LOGIN, 'password': PASSWORD}]}
        (path or self.config).write_text(json.dumps({'repository': 'OWNER/REPOSITORY', 'roads': None,
                                                     'browserTest': block}), encoding='utf-8')

    def auto(self, name, argv_port=True, **extra):
        argv = [sys.executable, str(self.script)] + (['{port}'] if argv_port else [])
        entry = {'name': name, 'argv': argv, 'port': 'auto', 'health': 'http://127.0.0.1:{port}/',
                 'timeoutSeconds': 20}
        entry.update(extra)
        return entry

    def command(self, operation, issue=11, root=None, config=None):
        return [sys.executable, str(SCRIPT), operation, '--config', str(config or self.config),
                '--root', str(root or self.root), '--issue', str(issue)]

    def serve(self, operation, issue=11, root=None, config=None):
        done = subprocess.run(self.command(operation, issue, root, config), capture_output=True, text=True,
                              encoding='utf-8', timeout=120)
        self.last = done
        return done

    def serve_in_background(self, issue, root=None, config=None):
        self.touched.append((root or self.root, issue))
        return subprocess.Popen(self.command('start', issue, root, config), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding='utf-8')

    def payload(self, done):
        return json.loads(done.stdout)


class AutoPortTest(PortsTestCase):
    def test_an_auto_port_is_reserved_and_injected_by_argv_environment_and_health_url(self):
        self.write_config([self.auto('web-api')])
        done = self.serve('start')
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        entry = self.payload(done)['processes'][0]
        port = entry['port']
        self.assertIsInstance(port, int)
        self.assertEqual(entry['url'], f'http://127.0.0.1:{port}/')
        self.assertEqual(fetch(entry['url']), f'{port}|{port}|{port}')

    def reservations(self):
        return self.root / '.frontlights' / 'serve' / 'ports'

    def test_the_reservation_is_recorded_with_issue_process_coordinator_and_time(self):
        self.write_config([self.auto('web-api')])
        port = self.payload(self.serve('start'))['processes'][0]['port']
        record = json.loads((self.reservations() / f'{port}.json').read_text(encoding='utf-8'))
        self.assertEqual((record['issue'], record['process']), (11, 'web-api'))
        self.assertIsInstance(record['pid'], int)
        self.assertTrue(record['reservedAt'])


class ReleaseTest(PortsTestCase):
    def reservations(self):
        return self.root / '.frontlights' / 'serve' / 'ports'

    def test_stop_releases_the_reservations_of_the_issue_and_keeps_those_of_other_issues(self):
        self.write_config([self.auto('web-api')])
        mine = self.payload(self.serve('start'))['processes'][0]['port']
        other = self.payload(self.serve('start', issue=12))['processes'][0]['port']
        self.touched.append((self.root, 12))
        self.assertEqual(self.serve('stop').returncode, 0, self.last.stdout)
        self.assertFalse((self.reservations() / f'{mine}.json').exists())
        self.assertTrue((self.reservations() / f'{other}.json').exists())
        self.assertTrue(wait_until(lambda: not reachable(f'http://127.0.0.1:{mine}/')))


class IgnoredPortTest(PortsTestCase):
    def test_a_server_that_ignores_the_reserved_port_fails_as_infrastructure_and_releases_everything(self):
        ignoring = self.base / 'ignoring_server.py'
        ignoring.write_text(IGNORING_SERVER, encoding='utf-8')
        fixed = free_port()
        bad = self.auto('api', argv_port=False, timeoutSeconds=2)
        bad['argv'] = [sys.executable, str(ignoring), str(fixed)]
        self.write_config([self.auto('web'), bad])
        done = self.serve('start')
        self.assertEqual(done.returncode, 1, done.stdout)
        result = self.payload(done)
        self.assertEqual((result['category'], result['failedProcess']), ('infraestrutura', 'api'))
        reserved = re.search(r'porta reservada (\d+)', result['error'])
        self.assertTrue(reserved, result['error'])
        self.assertNotEqual(int(reserved.group(1)), fixed)
        self.assertTrue(wait_until(lambda: not reachable(f'http://127.0.0.1:{fixed}/')), 'o servidor ficou vivo')
        ports = self.root / '.frontlights' / 'serve' / 'ports'
        self.assertEqual(list(ports.glob('*.json')), [])
        self.assertFalse((self.root / '.frontlights' / 'serve' / '11.json').exists())


class ConcurrencyTest(PortsTestCase):
    def test_two_concurrent_starts_of_different_issues_get_different_ports_and_both_urls_answer(self):
        self.write_config([self.auto('web-api'), self.auto('worker', argv_port=False)])
        runs = [self.serve_in_background(11), self.serve_in_background(12)]
        outputs = [run.communicate(timeout=120) for run in runs]
        self.assertEqual([run.returncode for run in runs], [0, 0], outputs)
        entries = [entry for out, _ in outputs for entry in json.loads(out)['processes']]
        ports = [entry['port'] for entry in entries]
        self.assertEqual(len(ports), 4)
        self.assertEqual(len(set(ports)), 4, ports)
        for entry in entries:
            self.assertEqual(fetch(entry['url']).split('|')[0], str(entry['port']))

    def test_two_reservers_offered_the_same_candidate_never_get_the_same_port(self):
        first, second = free_port(), free_port()
        offered = lambda: iter([first, first, second])  # noqa: E731
        with mock.patch.object(serve, 'candidate_ports', offered):
            got = [serve.reserve_port(self.root, 11, 'web'), serve.reserve_port(self.root, 12, 'web')]
        self.assertEqual(got, [first, second])

    def test_a_port_held_by_another_program_is_skipped(self):
        with socket.socket() as busy:
            busy.bind(('127.0.0.1', 0))
            busy.listen()
            taken, free = busy.getsockname()[1], free_port()
            with mock.patch.object(serve, 'candidate_ports', lambda: iter([taken, free])):
                self.assertEqual(serve.reserve_port(self.root, 11, 'web'), free)
            ports = self.root / '.frontlights' / 'serve' / 'ports'
            self.assertFalse((ports / f'{taken}.json').exists())

    def test_an_orphan_reservation_of_a_dead_pid_is_reused(self):
        gone = subprocess.Popen([sys.executable, '-c', 'pass'])
        gone.wait()
        port = free_port()
        ports = self.root / '.frontlights' / 'serve' / 'ports'
        ports.mkdir(parents=True)
        (ports / f'{port}.json').write_text(json.dumps({'issue': 5, 'process': 'old', 'root': 'x', 'pid': gone.pid,
                                                        'reservedAt': 'ontem'}), encoding='utf-8')
        with mock.patch.object(serve, 'candidate_ports', lambda: iter([port])):
            self.assertEqual(serve.reserve_port(self.root, 11, 'web'), port)
        self.assertEqual(json.loads((ports / f'{port}.json').read_text(encoding='utf-8'))['issue'], 11)


class SharedReservationTest(PortsTestCase):
    def git(self, *args, cwd=None):
        done = subprocess.run(['git', '-c', 'user.name=Teste', '-c', 'user.email=teste@exemplo.test', *args],
                              cwd=cwd or self.repo, capture_output=True, text=True, encoding='utf-8', timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)

    def setUp(self):
        super().setUp()
        self.repo = self.base / 'repo'
        self.repo.mkdir()
        self.git('init', '-q')
        self.git('commit', '-q', '--allow-empty', '-m', 'inicio')
        self.first, self.second = self.base / 'wt-a', self.base / 'wt-b'
        self.git('worktree', 'add', '-q', '-b', 'ramo-a', str(self.first))
        self.git('worktree', 'add', '-q', '-b', 'ramo-b', str(self.second))
        self.shared = self.repo / '.git' / 'frontlights-serve' / 'ports'
        self.write_config([self.auto('web-api')])

    def test_worktrees_of_the_same_repository_share_the_reservation_folder_and_never_collide(self):
        a = self.payload(self.serve('start', 11, root=self.first))['processes'][0]['port']
        self.touched.append((self.first, 11))
        b = self.payload(self.serve('start', 12, root=self.second))['processes'][0]['port']
        self.touched.append((self.second, 12))
        self.assertNotEqual(a, b)
        self.assertEqual(sorted(path.name for path in self.shared.glob('*.json')), sorted([f'{a}.json', f'{b}.json']))
        self.assertFalse((self.first / '.frontlights' / 'serve' / 'ports').exists())
        # the second worktree is offered the first one's live port and must skip it
        free = free_port()
        with mock.patch.object(serve, 'candidate_ports', lambda: iter([a, free])):
            self.assertEqual(serve.reserve_port(self.second, 13, 'web'), free)
        self.assertEqual(self.serve('stop', 11, root=self.first).returncode, 0, self.last.stdout)
        self.assertFalse((self.shared / f'{a}.json').exists())
        self.assertTrue((self.shared / f'{b}.json').exists())


class FixedPortTest(PortsTestCase):
    def test_a_fixed_port_still_works_next_to_an_auto_one_and_is_not_reserved(self):
        fixed = free_port()
        entry = {'name': 'api', 'argv': [sys.executable, str(self.script), str(fixed)], 'port': fixed,
                 'health': f'http://127.0.0.1:{fixed}/', 'timeoutSeconds': 20}
        self.write_config([entry, self.auto('web-api')])
        done = self.serve('start')
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        by_name = {item['name']: item for item in self.payload(done)['processes']}
        self.assertEqual(by_name['api']['port'], fixed)
        self.assertNotEqual(by_name['web-api']['port'], fixed)
        ports = self.root / '.frontlights' / 'serve' / 'ports'
        self.assertEqual([path.stem for path in ports.glob('*.json')], [str(by_name['web-api']['port'])])

    def test_login_and_password_in_an_auto_process_stay_out_of_output_and_files(self):
        self.write_config([self.auto('web-api', health=f'http://127.0.0.1:{{port}}/?u={LOGIN}&p={PASSWORD}')])
        done = self.serve('start')
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        text = done.stdout + done.stderr
        for path in self.root.rglob('*'):
            if path.is_file():
                text += path.read_text(encoding='utf-8')
        self.assertNotIn(PASSWORD, text)
        self.assertNotIn(LOGIN, text)
        self.assertIn('redacted', text)


if __name__ == '__main__':
    unittest.main()
