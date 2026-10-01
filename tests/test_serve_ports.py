import json
import os
import re
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


def old(path, seconds=120):
    """Make the file look untouched for `seconds` (a crashed writer's leftover)."""
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp))


def dead_pid():
    gone = subprocess.Popen([sys.executable, '-c', 'pass'])
    gone.wait()
    return gone.pid


class GitPortsCase(PortsTestCase):
    """A temporary Git repository whose worktrees share one reservation folder."""

    def git(self, *args, cwd=None):
        done = subprocess.run(['git', '-c', 'user.name=Teste', '-c', 'user.email=teste@exemplo.test', *args],
                              cwd=cwd or self.repo, capture_output=True, text=True, encoding='utf-8', timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)

    def make_repo(self):
        self.repo = self.base / 'repo'
        self.repo.mkdir()
        self.git('init', '-q')
        self.git('commit', '-q', '--allow-empty', '-m', 'inicio')
        self.shared = self.repo / '.git' / 'frontlights-serve' / 'ports'

    def make_worktree(self, name):
        path = self.base / name
        self.git('worktree', 'add', '-q', '-b', name, str(path))
        return path


class RealConcurrencyTest(GitPortsCase):
    STARTS = 8
    ROUNDS = 3

    def test_eight_simultaneous_starts_with_two_auto_processes_each_all_succeed_with_distinct_ports(self):
        self.make_repo()
        self.write_config([self.auto('web-api'), self.auto('worker', argv_port=False)])
        trees = [self.make_worktree(f'wt{number}') for number in range(self.STARTS)]
        for round_number in range(self.ROUNDS):
            runs = []
            for number, tree in enumerate(trees):
                issue = 100 + number
                self.touched.append((tree, issue))
                runs.append(self.serve_in_background(issue, root=tree))
            outputs = [run.communicate(timeout=120) for run in runs]
            codes = [run.returncode for run in runs]
            self.assertEqual(codes, [0] * self.STARTS, f'rodada {round_number}: {outputs}')
            entries = [entry for out, _ in outputs for entry in json.loads(out)['processes']]
            ports = [entry['port'] for entry in entries]
            self.assertEqual(len(ports), self.STARTS * 2)
            self.assertEqual(len(set(ports)), len(ports), ports)
            for entry in entries:
                self.assertEqual(fetch(entry['url']).split('|')[0], str(entry['port']))
            for number, tree in enumerate(trees):
                self.assertEqual(self.serve('stop', 100 + number, root=tree).returncode, 0, self.last.stdout)
            self.assertEqual(list(self.shared.glob('*.json')), [])
            self.assertFalse((self.shared / '.reserve.lock').exists(), 'a trava vazou')

    def test_the_lock_is_reentrant_for_the_same_start(self):
        directory = self.root / 'ports'
        with mock.patch.object(serve, 'RESERVE_LOCK_SECONDS', 1):
            with serve.reservation_lock(directory):
                with serve.reservation_lock(directory):
                    pass
        self.assertFalse((directory / '.reserve.lock').exists())

    def test_the_lock_is_not_released_silently_when_the_file_cannot_be_removed(self):
        directory = self.root / 'ports'
        real_unlink = os.unlink

        def refuse(path, *args, **kwargs):
            if os.path.basename(str(path)) == '.reserve.lock':
                raise PermissionError('em uso')
            return real_unlink(path, *args, **kwargs)

        with mock.patch.object(os, 'unlink', refuse), mock.patch.object(os, 'remove', refuse), \
                mock.patch.object(serve.time, 'sleep', lambda seconds: None):
            with self.assertRaises(serve.Refusal) as caught:
                with serve.reservation_lock(directory):
                    pass
        self.assertIn('trava', str(caught.exception))
        (directory / '.reserve.lock').unlink()


class LockRecoveryTest(PortsTestCase):
    def setUp(self):
        super().setUp()
        self.ports = self.root / '.frontlights' / 'serve' / 'ports'
        self.ports.mkdir(parents=True)
        self.lock = self.ports / '.reserve.lock'

    def test_a_stale_lock_of_a_dead_pid_is_cleaned(self):
        self.lock.write_text(str(dead_pid()), encoding='utf-8')
        with mock.patch.object(serve, 'RESERVE_LOCK_SECONDS', 3):
            self.assertIsInstance(serve.reserve_port(self.root, 11, 'web'), int)
        self.assertFalse(self.lock.exists())

    def test_an_empty_or_garbage_old_lock_is_an_orphan_and_is_cleaned(self):
        for content in ('', 'lixo-sem-numero'):
            self.lock.write_text(content, encoding='utf-8')
            old(self.lock)
            with mock.patch.object(serve, 'RESERVE_LOCK_SECONDS', 3):
                self.assertIsInstance(serve.reserve_port(self.root, 11, 'web'), int, content)
            self.assertFalse(self.lock.exists())

    def test_an_empty_recent_lock_is_still_being_written_so_it_is_waited_for(self):
        self.lock.write_text('', encoding='utf-8')
        with mock.patch.object(serve, 'RESERVE_LOCK_SECONDS', 1):
            with self.assertRaises(serve.Refusal) as caught:
                serve.reserve_port(self.root, 11, 'web')
        self.assertIn('trava de reserva', str(caught.exception))
        self.assertTrue(self.lock.exists())


class ReservationRulesTest(GitPortsCase):
    def setUp(self):
        super().setUp()
        self.ports = self.root / '.frontlights' / 'serve' / 'ports'

    def test_the_reservation_is_created_exclusively_even_when_a_racer_writes_between_check_and_create(self):
        self.ports.mkdir(parents=True)
        real = serve.bindable
        hijacked = []

        def racer(port):
            if not hijacked:
                hijacked.append(port)
                (self.ports / f'{port}.json').write_text(json.dumps({'issue': 99, 'pid': os.getpid(), 'process': 'x',
                                                                     'root': 'y'}), encoding='utf-8')
            return real(port)

        with mock.patch.object(serve, 'bindable', racer):
            got = serve.reserve_port(self.root, 11, 'web')
        self.assertNotEqual(got, hijacked[0])
        self.assertEqual(json.loads((self.ports / f'{hijacked[0]}.json').read_text(encoding='utf-8'))['issue'], 99)

    def test_the_lock_serialises_two_reservers_that_both_judge_the_same_reservation_an_orphan(self):
        port = free_port()
        self.ports.mkdir(parents=True)
        (self.ports / f'{port}.json').write_text(json.dumps({'issue': 5, 'process': 'old', 'root': 'x',
                                                             'pid': dead_pid()}), encoding='utf-8')
        barrier = threading.Barrier(2, timeout=1.5)
        real = serve.reservation_live

        def meet(path):
            answer = real(path)
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
            return answer

        real_remove = serve.remove_file

        def late_remove(path):
            # the second reserver removes the orphan only after the first one has recreated it as its own
            if threading.current_thread().name == 'second':
                wait_until(lambda: (serve.read_reservation(path) or {}).get('issue') == 11, 3)
            return real_remove(path)

        got, errors = [], []

        def reserve(issue):
            try:
                got.append(serve.reserve_port(self.root, issue, 'web'))
            except Exception as error:  # noqa: BLE001
                errors.append(error)

        spare = [free_port(), free_port()]
        offered = lambda: iter([port] * 5 + spare)  # noqa: E731
        with mock.patch.object(serve, 'reservation_live', meet), \
                mock.patch.object(serve, 'candidate_ports', offered), \
                mock.patch.object(serve, 'remove_file', late_remove):
            threads = [threading.Thread(target=reserve, args=(11,), name='first'),
                       threading.Thread(target=reserve, args=(12,), name='second')]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(len(got), 2)
        self.assertEqual(len(set(got)), 2, got)

    def test_a_process_that_outlives_its_start_keeps_the_port_for_other_issues(self):
        self.write_config([self.auto('web-api')])
        port = self.payload(self.serve('start'))['processes'][0]['port']
        record = json.loads((self.ports / f'{port}.json').read_text(encoding='utf-8'))
        self.assertIsInstance(record.get('child', {}).get('pid'), int, record)
        self.assertFalse(serve.pid_alive(record['pid']), 'o coordenador já terminou')
        free = free_port()
        with mock.patch.object(serve, 'bindable', lambda candidate: True), \
                mock.patch.object(serve, 'candidate_ports', lambda: iter([port, free])):
            self.assertEqual(serve.reserve_port(self.root, 12, 'web'), free)
        self.assertTrue((self.ports / f'{port}.json').exists())

    def test_reservation_live_follows_the_child_when_the_coordinator_is_dead(self):
        self.ports.mkdir(parents=True)
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        self.addCleanup(child.kill)
        path = self.ports / '1.json'
        record = {'issue': 5, 'process': 'p', 'root': 'x', 'pid': dead_pid(),
                  'child': {'pid': child.pid, 'identity': serve.process_identity(child.pid) or 'desconhecida'}}
        path.write_text(json.dumps(record), encoding='utf-8')
        self.assertTrue(serve.reservation_live(path))
        child.kill()
        child.wait()
        self.assertFalse(serve.reservation_live(path))

    def test_release_ports_only_touches_reservations_made_from_the_same_worktree(self):
        self.ports.mkdir(parents=True)
        mine, theirs = free_port(), free_port()
        for port, root in ((mine, self.root), (theirs, self.base / 'outra')):
            (self.ports / f'{port}.json').write_text(json.dumps({'issue': 11, 'root': str(Path(root).resolve()),
                                                                 'pid': os.getpid()}), encoding='utf-8')
        serve.release_ports(self.root, 11)
        self.assertFalse((self.ports / f'{mine}.json').exists())
        self.assertTrue((self.ports / f'{theirs}.json').exists())

    def test_release_ports_keeps_the_ports_listed_in_keep(self):
        self.ports.mkdir(parents=True)
        first, second = free_port(), free_port()
        for port in (first, second):
            (self.ports / f'{port}.json').write_text(json.dumps({'issue': 11, 'root': str(self.root.resolve()),
                                                                 'pid': os.getpid()}), encoding='utf-8')
        serve.release_ports(self.root, 11, keep=[first])
        self.assertTrue((self.ports / f'{first}.json').exists())
        self.assertFalse((self.ports / f'{second}.json').exists())

    def test_an_unreadable_recent_reservation_counts_as_live_and_an_old_one_is_an_orphan(self):
        self.ports.mkdir(parents=True)
        taken, free = free_port(), free_port()
        path = self.ports / f'{taken}.json'
        for content in ('', '{lixo'):
            path.write_text(content, encoding='utf-8')
            with mock.patch.object(serve, 'candidate_ports', lambda: iter([taken, free])):
                self.assertEqual(serve.reserve_port(self.root, 11, 'web'), free, content)
            (self.ports / f'{free}.json').unlink()
            old(path)
            with mock.patch.object(serve, 'candidate_ports', lambda: iter([taken, free])):
                self.assertEqual(serve.reserve_port(self.root, 11, 'web'), taken, content)
            path.unlink()

    def test_reservations_in_the_shared_git_folder_hold_no_login_or_password(self):
        self.make_repo()
        tree = self.make_worktree('wt-secret')
        self.write_config([self.auto('web-api', health=f'http://127.0.0.1:{{port}}/?u={LOGIN}&p={PASSWORD}')])
        self.touched.append((tree, 11))
        done = self.serve('start', 11, root=tree)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        files = [path for path in (self.repo / '.git' / 'frontlights-serve').rglob('*') if path.is_file()]
        self.assertTrue(files)
        text = ''.join(path.read_text(encoding='utf-8') for path in files)
        self.assertNotIn(PASSWORD, text)
        self.assertNotIn(LOGIN, text)

    def test_start_reports_whether_the_reservations_are_shared(self):
        self.write_config([self.auto('web-api')])
        self.assertIs(self.payload(self.serve('start'))['reservas_compartilhadas'], False)
        self.make_repo()
        tree = self.make_worktree('wt-flag')
        self.touched.append((tree, 12))
        self.assertIs(self.payload(self.serve('start', 12, root=tree))['reservas_compartilhadas'], True)

    def test_the_common_dir_is_resolved_when_path_format_absolute_is_not_supported(self):
        self.make_repo()
        tree = self.make_worktree('wt-old-git')
        real = subprocess.run

        def old_git(command, *args, **kwargs):
            if '--path-format=absolute' in command:
                return subprocess.CompletedProcess(command, 129, '', 'unknown option')
            return real(command, *args, **kwargs)

        with mock.patch.object(subprocess, 'run', old_git):
            folder, shared = serve.ports_location(tree)
        self.assertTrue(shared)
        self.assertEqual(folder.resolve(), self.shared.resolve())


class NameCollisionTest(PortsTestCase):
    def test_process_names_that_collide_after_normalisation_are_refused_as_usage(self):
        self.write_config([self.auto('web-api'), self.auto('web_api')])
        done = self.serve('start')
        self.assertEqual(done.returncode, 1, done.stdout)
        result = self.payload(done)
        self.assertEqual(result['category'], 'uso')
        self.assertIn('FRONTLIGHTS_PORT_WEB_API', result['error'])


class WideListenerTest(PortsTestCase):
    def occupied_is_skipped(self, family, address):
        with socket.socket(family) as busy:
            busy.bind((address, 0))
            busy.listen()
            taken, free = busy.getsockname()[1], free_port()
            self.assertFalse(serve.bindable(taken), f'{address} não foi detectado')
            with mock.patch.object(serve, 'candidate_ports', lambda: iter([taken, free])):
                self.assertEqual(serve.reserve_port(self.root, 11, 'web'), free)

    def ipv6_or_skip(self, address):
        try:
            with socket.socket(socket.AF_INET6) as probe:
                probe.bind((address, 0))
        except OSError:
            self.skipTest('IPv6 indisponível')

    def test_a_listener_on_all_ipv4_interfaces_is_detected_and_skipped(self):
        self.occupied_is_skipped(socket.AF_INET, '0.0.0.0')

    def test_a_listener_on_ipv6_any_is_detected_and_skipped(self):
        self.ipv6_or_skip('::')
        self.occupied_is_skipped(socket.AF_INET6, '::')

    def test_a_listener_on_ipv6_loopback_is_detected_and_skipped(self):
        self.ipv6_or_skip('::1')
        self.occupied_is_skipped(socket.AF_INET6, '::1')


if __name__ == '__main__':
    unittest.main()
