import contextlib
import errno
import io
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
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


class _LimitedClock:
    """Stand-in for `serve.time`: the real clock, but any wait loop that outlives `seconds` fails the test."""

    def __init__(self, seconds):
        self.seconds = seconds
        self.start = time.monotonic()

    def check(self):
        if time.monotonic() - self.start > self.seconds:
            raise AssertionError(f'o laço de espera passou de {self.seconds} s: ignorou o prazo')

    def monotonic(self):
        self.check()
        return time.monotonic()

    def sleep(self, seconds):
        self.check()
        time.sleep(seconds)

    def __getattr__(self, name):
        return getattr(time, name)


@contextlib.contextmanager
def watchdog(seconds=10):
    """Run a deadline test with a watchdog: a loop that ignores its deadline fails in `seconds` instead of hanging."""
    with mock.patch.object(serve, 'time', _LimitedClock(seconds)):
        yield


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
                              encoding='utf-8', errors='replace', timeout=120)
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

    def test_simultaneous_starts_with_two_auto_processes_each_all_succeed_with_distinct_ports(self):
        self.make_repo()
        self.write_config([self.auto('web-api'), self.auto('worker', argv_port=False)])
        trees = [self.make_worktree(f'wt{number}') for number in range(self.STARTS)]
        for round_number in range(self.ROUNDS):
            runs = []
            for number, tree in enumerate(trees):
                issue = 100 + number
                self.touched.append((tree, issue))
                runs.append(self.serve_in_background(issue, root=tree))
            outputs = [run.communicate(timeout=240) for run in runs]
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
            with serve.reservation_lock(self.shared):  # a leaked lock would refuse here after the deadline
                pass
            self.assertEqual([path.name for path in self.shared.iterdir()], ['.reserve.lock'], 'sobrou lixo')


class RealConcurrency12Test(RealConcurrencyTest):
    STARTS = 12
    ROUNDS = 1


class RealConcurrency16Test(RealConcurrencyTest):
    STARTS = 16
    ROUNDS = 1


HOLDER = r"""import sys
import time
from pathlib import Path

sys.path.insert(0, sys.argv[1])
import serve

with serve.reservation_lock(Path(sys.argv[2])):
    print('held', flush=True)
    time.sleep(float(sys.argv[3]))
"""

# Takes the lock `loops` times, each time claiming a marker file with O_EXCL: a failed claim is a violation.
WORKER = r"""import contextlib
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, sys.argv[1])
import serve

directory, marker, log = Path(sys.argv[2]), sys.argv[3], sys.argv[4]
start_at, loops = float(sys.argv[5]), int(sys.argv[6])
while time.time() < start_at:
    time.sleep(0.0005)
for _ in range(loops):
    with serve.reservation_lock(directory):
        try:
            descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            os.write(os.open(log, os.O_CREAT | os.O_APPEND | os.O_WRONLY), b'V')
        else:
            os.close(descriptor)
            time.sleep(0.002)
            with contextlib.suppress(OSError):
                os.unlink(marker)
            os.write(os.open(log, os.O_CREAT | os.O_APPEND | os.O_WRONLY), b'K')
"""

# Windows only: opens the file with sharing mode 0 (no read, write or delete sharing) and holds it.
EXCLUSIVE_OPEN = r"""import ctypes
import sys
import time

kernel = ctypes.windll.kernel32
kernel.CreateFileW.restype = ctypes.c_void_p
handle = kernel.CreateFileW(sys.argv[1], 0x80000000 | 0x40000000, 0, None, 3, 0x80, None)
if handle in (None, ctypes.c_void_p(-1).value):
    sys.exit(3)
print('open', flush=True)
time.sleep(float(sys.argv[2]))
"""


class ReserveLockTest(PortsTestCase):
    """The reservation lock is an operating-system lock on a file that is never moved or deleted."""

    def setUp(self):
        super().setUp()
        self.ports = self.base / 'ports'

    def hold(self, seconds):
        holder = subprocess.Popen([sys.executable, '-c', HOLDER, str(SCRIPT.parent), str(self.ports), str(seconds)],
                                  stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.communicate)
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), 'held')
        return holder

    def test_the_lock_is_reentrant_for_the_same_start(self):
        with mock.patch.object(serve, 'RESERVE_LOCK_SECONDS', 1):
            with serve.reservation_lock(self.ports):
                with serve.reservation_lock(self.ports):
                    pass
            with serve.reservation_lock(self.ports):  # released for real: it can be taken again
                pass

    def test_the_lock_is_released_and_the_descriptor_closed_when_the_body_fails(self):
        seen = []
        real = serve.take_reserve_file

        def spying(directory):
            descriptor = real(directory)
            seen.append(descriptor)
            return descriptor

        with mock.patch.object(serve, 'take_reserve_file', spying), \
                mock.patch.object(serve, 'RESERVE_LOCK_SECONDS', 1):
            with self.assertRaises(RuntimeError):
                with serve.reservation_lock(self.ports):
                    raise RuntimeError('falha no corpo')
            with serve.reservation_lock(self.ports):
                pass
        self.assertEqual(len(seen), 2)
        for descriptor in seen:
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_the_lock_file_is_never_deleted_and_nothing_else_is_left_in_the_folder(self):
        with serve.reservation_lock(self.ports):
            pass
        self.assertEqual([path.name for path in self.ports.iterdir()], ['.reserve.lock'])

    def test_whatever_a_legacy_lock_file_holds_it_is_just_taken(self):
        self.ports.mkdir(parents=True)
        lock = self.ports / '.reserve.lock'
        for content, age in ((str(dead_pid()), 0), (str(os.getpid()), 3600), ('lixo', 0), ('', 3600)):
            lock.write_text(content, encoding='utf-8')
            if age:
                old(lock, age)
            started = time.monotonic()
            with mock.patch.object(serve, 'RESERVE_LOCK_SECONDS', 5):
                with serve.reservation_lock(self.ports):
                    pass
            self.assertLess(time.monotonic() - started, 2, content)

    def test_the_owner_killed_in_the_middle_of_the_critical_section_frees_the_lock_at_once(self):
        holder = self.hold(120)
        holder.kill()  # TerminateProcess / SIGKILL: no cleanup code runs in the owner
        holder.wait()
        started = time.monotonic()
        with mock.patch.object(serve, 'RESERVE_LOCK_SECONDS', 20):
            with serve.reservation_lock(self.ports):
                pass
        self.assertLess(time.monotonic() - started, 5, 'esperou o prazo em vez de obter a trava ao morrer o dono')

    def test_a_lock_held_by_another_process_past_the_deadline_is_refused_in_about_that_time(self):
        self.hold(120)
        started = time.monotonic()
        with mock.patch.object(serve, 'RESERVE_LOCK_SECONDS', 1.5), watchdog():
            with self.assertRaises(serve.Refusal) as caught:
                with serve.reservation_lock(self.ports):
                    self.fail('obteve a trava de outro processo')
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 1.4)
        self.assertLess(elapsed, 6)
        self.assertIn('trava de reserva de portas', str(caught.exception))
        self.assertIn('1.5', str(caught.exception))

    @unittest.skipUnless(os.name == 'nt', 'abertura sem compartilhamento só existe no Windows')
    def test_a_lock_file_open_without_sharing_by_another_process_is_refused_within_the_deadline(self):
        self.ports.mkdir(parents=True)
        lock = self.ports / '.reserve.lock'
        lock.write_text('', encoding='utf-8')
        old(lock, 3600)
        holder = subprocess.Popen([sys.executable, '-c', EXCLUSIVE_OPEN, str(lock), '60'], stdout=subprocess.PIPE,
                                  text=True)
        self.addCleanup(holder.communicate)
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), 'open')
        started = time.monotonic()
        with mock.patch.object(serve, 'RESERVE_LOCK_SECONDS', 1.5), watchdog():
            with self.assertRaises(serve.Refusal) as caught:
                with serve.reservation_lock(self.ports):
                    self.fail('obteve a trava de um arquivo aberto sem compartilhamento')
        self.assertLess(time.monotonic() - started, 6, 'o laço ignorou o prazo')
        self.assertIn('trava de reserva', str(caught.exception))

    def test_many_processes_never_share_the_critical_section_with_fresh_stale_or_missing_lock_files(self):
        rounds, workers, loops = 25, 6, 6
        self.ports.mkdir(parents=True)
        lock = self.ports / '.reserve.lock'
        marker = self.base / 'marker'
        log = self.base / 'log'
        for number in range(rounds):
            lock.unlink(missing_ok=True)
            marker.unlink(missing_ok=True)
            log.unlink(missing_ok=True)
            if number % 3 == 0:  # a leftover from the old mechanism, dead owner, long ago
                lock.write_text(str(dead_pid()), encoding='utf-8')
                old(lock, 3600)
            elif number % 3 == 1:  # a lock file with a fresh live pid in it
                lock.write_text(str(os.getpid()), encoding='utf-8')
            start_at = time.time() + 2.5
            runs = [subprocess.Popen([sys.executable, '-c', WORKER, str(SCRIPT.parent), str(self.ports), str(marker),
                                      str(log), str(start_at), str(loops)], stderr=subprocess.PIPE, text=True)
                    for _ in range(workers)]
            errors = [run.communicate(timeout=120)[1] for run in runs]
            self.assertEqual([run.returncode for run in runs], [0] * workers, errors)
            outcome = log.read_bytes()
            self.assertEqual(outcome.count(b'V'), 0, f'rodada {number}: seção crítica violada {outcome!r}')
            self.assertEqual(outcome.count(b'K'), workers * loops, f'rodada {number}')


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


class AutoHealthNeedsPortTest(PortsTestCase):
    def test_an_auto_process_whose_health_url_lacks_the_port_placeholder_is_refused_before_anything_starts(self):
        other = free_port()
        other_server = subprocess.Popen([sys.executable, str(self.script), str(other)])
        self.addCleanup(other_server.kill)
        self.assertTrue(wait_until(lambda: reachable(f'http://127.0.0.1:{other}/')))
        sleeper = self.auto('api', argv_port=False, health=f'http://127.0.0.1:{other}/')
        sleeper['argv'] = [sys.executable, '-c', 'import time; time.sleep(60)']
        self.write_config([sleeper])
        done = self.serve('start')
        self.assertEqual(done.returncode, 1, done.stdout)
        result = self.payload(done)
        self.assertEqual(result['category'], 'uso')
        self.assertIn('{port}', result['error'])
        self.assertIn('api', result['error'])
        self.assertFalse((self.root / '.frontlights' / 'serve' / '11.json').exists())
        self.assertEqual(list((self.root / '.frontlights' / 'serve').glob('ports/*.json')), [])


class NameCollisionScopeTest(PortsTestCase):
    def fixed(self, name):
        port = free_port()
        return {'name': name, 'argv': [sys.executable, str(self.script), str(port)], 'port': port,
                'health': f'http://127.0.0.1:{port}/', 'timeoutSeconds': 20}

    def test_two_fixed_port_processes_with_colliding_names_start_normally(self):
        self.write_config([self.fixed('web-api'), self.fixed('web_api')])
        done = self.serve('start')
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def test_one_auto_and_one_fixed_with_colliding_names_start_and_only_the_auto_gets_the_variable(self):
        self.write_config([self.auto('web-api'), self.fixed('web_api')])
        done = self.serve('start')
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        by_name = {item['name']: item for item in self.payload(done)['processes']}
        auto_port = by_name['web-api']['port']
        self.assertEqual(fetch(by_name['web-api']['url']).split('|')[2], str(auto_port))
        self.assertEqual(fetch(by_name['web_api']['url']).split('|')[2], '')

    def test_two_auto_processes_with_colliding_names_are_still_refused(self):
        self.write_config([self.auto('web-api'), self.auto('web_api')])
        done = self.serve('start')
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertEqual(self.payload(done)['category'], 'uso')


class SpawnWindowTest(PortsTestCase):
    def test_a_start_that_dies_between_spawn_and_recording_the_child_keeps_the_port_for_a_while(self):
        self.write_config([self.auto('web-api')])
        driver = (
            'import json, os, sys\n'
            f'sys.path.insert(0, {str(SCRIPT.parent)!r})\n'
            'import serve\n'
            'def die(root, port, entry):\n'
            "    print(json.dumps({'port': port, 'pid': entry['pid']}), flush=True)\n"
            '    os._exit(0)\n'
            'serve.attach_child = die\n'
            f'serve.start({str(self.config)!r}, {str(self.root)!r}, 11)\n')
        done = subprocess.run([sys.executable, '-c', driver], capture_output=True, text=True, encoding='utf-8',
                              timeout=120)
        info = json.loads(done.stdout.strip().splitlines()[-1])
        self.addCleanup(serve.kill_tree, info['pid'])
        self.assertTrue(wait_until(lambda: reachable(f'http://127.0.0.1:{info["port"]}/')))
        reservation = self.root / '.frontlights' / 'serve' / 'ports' / f'{info["port"]}.json'
        self.assertFalse(serve.pid_alive(json.loads(reservation.read_text(encoding='utf-8'))['pid']))
        free = free_port()
        with mock.patch.object(serve, 'bindable', lambda candidate: True), \
                mock.patch.object(serve, 'candidate_ports', lambda: iter([info['port'], free])):
            self.assertEqual(serve.reserve_port(self.root, 12, 'web'), free)

    def test_the_grace_for_a_spawning_reservation_ends_after_the_orphan_period(self):
        ports = self.root / '.frontlights' / 'serve' / 'ports'
        ports.mkdir(parents=True)
        path = ports / '1.json'
        path.write_text(json.dumps({'issue': 5, 'process': 'p', 'root': 'x', 'pid': dead_pid(),
                                    'spawning': 'agora'}), encoding='utf-8')
        self.assertTrue(serve.reservation_live(path))
        old(path)
        self.assertFalse(serve.reservation_live(path))

    def test_a_reservation_of_a_dead_coordinator_that_never_reached_spawn_is_an_orphan_at_once(self):
        ports = self.root / '.frontlights' / 'serve' / 'ports'
        ports.mkdir(parents=True)
        path = ports / '1.json'
        path.write_text(json.dumps({'issue': 5, 'process': 'p', 'root': 'x', 'pid': dead_pid()}), encoding='utf-8')
        self.assertFalse(serve.reservation_live(path))


def fake_sockets(refuse_bind=(), connect_ok=(), seen=None):
    """Replacement for the socket module seen by serve: scripted bind refusals and connect answers."""
    seen = [] if seen is None else seen

    class Fake:
        def __init__(self, family=socket.AF_INET, *args):
            self.family = family

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def bind(self, address):
            seen.append(('bind', address[0]))
            if address[0] in refuse_bind:
                raise OSError('em uso')

        def settimeout(self, seconds):
            pass

        def connect_ex(self, address):
            seen.append(('connect', address[0]))
            return 0 if address[0] in connect_ok else 10061

    return types.SimpleNamespace(socket=Fake, AF_INET=socket.AF_INET, AF_INET6=socket.AF_INET6)


class BindableDetailTest(PortsTestCase):
    def check(self, module, ipv6=False, extra=True):
        with mock.patch.object(serve, 'socket', module), mock.patch.object(serve, 'ipv6_usable', lambda: ipv6), \
                mock.patch.object(serve, 'loopback_alias_usable', lambda: extra, create=True):
            return serve.bindable(40000)

    def test_a_bind_refusal_on_the_wildcard_address_alone_makes_the_port_busy(self):
        self.assertFalse(self.check(fake_sockets(refuse_bind=('0.0.0.0',))))

    def test_a_bind_refusal_on_the_loopback_alias_alone_makes_the_port_busy(self):
        self.assertFalse(self.check(fake_sockets(refuse_bind=('127.0.0.2',))))

    def test_a_connection_that_is_accepted_makes_the_port_busy_even_when_every_bind_succeeds(self):
        for address in ('127.0.0.1', '127.0.0.2'):
            self.assertFalse(self.check(fake_sockets(connect_ok=(address,))), address)

    def test_the_loopback_alias_is_skipped_when_the_platform_has_none(self):
        seen = []
        self.assertTrue(self.check(fake_sockets(refuse_bind=('127.0.0.2',), seen=seen), extra=False))
        self.assertNotIn(('bind', '127.0.0.2'), seen)

    def test_a_free_port_is_bound_and_probed_on_every_address(self):
        seen = []
        self.assertTrue(self.check(fake_sockets(seen=seen)))
        for address in ('127.0.0.1', '0.0.0.0', '127.0.0.2'):
            self.assertIn(('bind', address), seen)
        self.assertIn(('connect', '127.0.0.1'), seen)
        self.assertIn(('connect', '127.0.0.2'), seen)

    def test_a_real_listener_that_only_accepts_connections_is_detected_by_the_connect_probe(self):
        real = socket

        class NoBind:
            def __init__(self, family=real.AF_INET, *args):
                self.inner = real.socket(family)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.inner.close()
                return False

            def bind(self, address):
                pass

            def settimeout(self, seconds):
                self.inner.settimeout(seconds)

            def connect_ex(self, address):
                return self.inner.connect_ex(address)

        module = types.SimpleNamespace(socket=NoBind, AF_INET=real.AF_INET, AF_INET6=real.AF_INET6)
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            busy = listener.getsockname()[1]
            with mock.patch.object(serve, 'socket', module), mock.patch.object(serve, 'ipv6_usable', lambda: False), \
                    mock.patch.object(serve, 'loopback_alias_usable', lambda: False, create=True):
                self.assertFalse(serve.bindable(busy))
        with mock.patch.object(serve, 'socket', module), mock.patch.object(serve, 'ipv6_usable', lambda: False), \
                mock.patch.object(serve, 'loopback_alias_usable', lambda: False, create=True):
            self.assertTrue(serve.bindable(busy))


class SpecificAddressListenerTest(PortsTestCase):
    def alias_or_skip(self):
        try:
            with socket.socket() as probe:
                probe.bind(('127.0.0.2', 0))
        except OSError:
            self.skipTest('127.0.0.2 indisponível')

    def test_a_listener_on_the_loopback_alias_is_detected_and_skipped(self):
        self.alias_or_skip()
        with socket.socket() as busy:
            busy.bind(('127.0.0.2', 0))
            busy.listen()
            taken, free = busy.getsockname()[1], free_port()
            self.assertFalse(serve.bindable(taken))
            with mock.patch.object(serve, 'candidate_ports', lambda: iter([taken, free])):
                self.assertEqual(serve.reserve_port(self.root, 11, 'web'), free)

    def test_a_bound_socket_that_does_not_listen_on_all_interfaces_makes_the_port_busy(self):
        with socket.socket() as busy:
            busy.bind(('0.0.0.0', 0))
            self.assertFalse(serve.bindable(busy.getsockname()[1]))


class StopWithoutRegistryTest(PortsTestCase):
    def reservation(self, port, issue, root):
        ports = self.root / '.frontlights' / 'serve' / 'ports'
        ports.mkdir(parents=True, exist_ok=True)
        (ports / f'{port}.json').write_text(json.dumps({'issue': issue, 'process': 'web', 'pid': os.getpid(),
                                                        'root': str(Path(root).resolve())}), encoding='utf-8')
        return ports / f'{port}.json'

    def test_stop_without_a_registry_still_releases_the_reservations_of_the_issue_from_this_worktree(self):
        mine, other_issue, other_tree = free_port(), free_port(), free_port()
        mine_file = self.reservation(mine, 11, self.root)
        other_file = self.reservation(other_issue, 12, self.root)
        foreign_file = self.reservation(other_tree, 11, self.base / 'outra')
        done = self.serve('stop')
        self.assertEqual(done.returncode, 1, done.stdout)
        result = self.payload(done)
        self.assertEqual(result['reservas_liberadas'], [mine])
        self.assertFalse(mine_file.exists())
        self.assertTrue(other_file.exists())
        self.assertTrue(foreign_file.exists())

    def test_stop_reports_the_released_ports_when_it_succeeds(self):
        self.write_config([self.auto('web-api')])
        port = self.payload(self.serve('start'))['processes'][0]['port']
        self.assertEqual(self.payload(self.serve('stop'))['reservas_liberadas'], [port])


class SingleAcquisitionTest(PortsTestCase):
    def test_all_the_ports_of_one_start_are_reserved_under_one_acquisition_of_the_lock(self):
        real = serve.take_reserve_file
        taken = []

        def counting(directory):
            taken.append(directory)
            return real(directory)

        with mock.patch.object(serve, 'take_reserve_file', counting):
            ports = serve.reserve_ports(self.root, 11, ['web', 'worker'])
        self.assertEqual(len(set(ports.values())), 2)
        self.assertEqual(len(taken), 1)


BAD_PIDS = (2 ** 40, 10 ** 30, -1, -2 ** 40, 0, True, False, '123', 1.5, None, [1])


class InvalidPidTest(PortsTestCase):
    """A pid outside 1..2**31-1 (or not an integer) is unreadable data, never a crash."""

    def setUp(self):
        super().setUp()
        self.ports = self.root / '.frontlights' / 'serve' / 'ports'
        self.ports.mkdir(parents=True)

    def reservation(self, **fields):
        port = free_port()
        path = self.ports / f'{port}.json'
        path.write_text(json.dumps(dict({'issue': 5, 'process': 'p', 'root': 'x'}, **fields)), encoding='utf-8')
        return port, path

    def test_a_reservation_with_an_invalid_pid_is_unreadable_so_recent_is_live_and_old_is_an_orphan(self):
        for value in BAD_PIDS:
            port, path = self.reservation(pid=value)
            self.assertIsNone(serve.read_reservation(path), repr(value))
            self.assertTrue(serve.reservation_live(path), repr(value))
            free = free_port()
            with mock.patch.object(serve, 'candidate_ports', lambda: iter([port, free])):
                self.assertEqual(serve.reserve_port(self.root, 11, 'web'), free, repr(value))
            (self.ports / f'{free}.json').unlink()
            old(path)
            self.assertFalse(serve.reservation_live(path), repr(value))
            with mock.patch.object(serve, 'candidate_ports', lambda: iter([port])):
                self.assertEqual(serve.reserve_port(self.root, 11, 'web'), port, repr(value))
            path.unlink()

    def test_a_reservation_whose_child_has_an_invalid_pid_ignores_the_child(self):
        for value in BAD_PIDS:
            _, path = self.reservation(pid=dead_pid(), child={'pid': value, 'identity': 'x'})
            self.assertFalse(serve.reservation_live(path), repr(value))

    def test_the_valid_range_accepts_the_edges(self):
        self.assertTrue(serve.valid_pid(1))
        self.assertTrue(serve.valid_pid(2 ** 31 - 1))
        for value in BAD_PIDS + (2 ** 31,):
            self.assertFalse(serve.valid_pid(value), repr(value))

    def test_a_registry_with_an_invalid_pid_is_refused_as_unreadable(self):
        path = self.root / '.frontlights' / 'serve' / '11.json'
        for value in BAD_PIDS:
            path.write_text(json.dumps({'issue': 11, 'processes': [{'name': 'p', 'pid': value}]}), encoding='utf-8')
            with self.assertRaises(serve.Refusal) as caught:
                serve.read_registry(self.root, 11)
            self.assertIn('ilegível', str(caught.exception), repr(value))

    def test_an_issue_lock_with_an_invalid_pid_is_refused_as_unreadable_without_a_traceback(self):
        self.write_config([self.auto('web-api')])
        lock = self.root / '.frontlights' / 'serve' / '11.lock'
        for text in ('1099511627776', '1' + '0' * 30, '-5', '0', '2147483648'):
            lock.write_text(text, encoding='utf-8')
            done = self.serve('start')
            self.assertEqual(done.returncode, 1, (text, done.stdout, done.stderr))
            self.assertNotIn('Traceback', done.stderr, text)
            result = self.payload(done)
            self.assertIn('ilegível', result['error'], text)
            self.assertIn('11.lock', result['error'], text)


SLOW_START = r"""import sys
import time
from pathlib import Path

sys.path.insert(0, sys.argv[1])
import serve

config, root, flags = sys.argv[2], sys.argv[3], Path(sys.argv[4])
real = serve.spawn


def slow(*args, **kwargs):
    (flags / 'before-spawn').write_text('x')
    while not (flags / 'go').exists():
        time.sleep(0.05)
    return real(*args, **kwargs)


serve.spawn = slow
sys.exit(serve.main(['start', '--config', config, '--root', root, '--issue', '11']))
"""


class StopDuringStartTest(PortsTestCase):
    def test_stop_during_a_slow_start_of_the_same_issue_is_refused_and_keeps_the_live_reservation(self):
        self.write_config([self.auto('web-api')])
        flags = self.base / 'flags'
        flags.mkdir()
        ports = self.root / '.frontlights' / 'serve' / 'ports'
        start = subprocess.Popen([sys.executable, '-c', SLOW_START, str(SCRIPT.parent), str(self.config),
                                  str(self.root), str(flags)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, encoding='utf-8')
        self.addCleanup(start.communicate)
        self.addCleanup(start.kill)
        self.assertTrue(wait_until(lambda: (flags / 'before-spawn').exists(), 30), 'o start não chegou ao spawn')
        reserved = [path.name for path in ports.glob('*.json')]
        self.assertEqual(len(reserved), 1, reserved)
        done = self.serve('stop')
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn('em andamento', self.payload(done)['error'])
        self.assertEqual([path.name for path in ports.glob('*.json')], reserved, 'o stop apagou a reserva viva')
        (flags / 'go').write_text('x')
        out, err = start.communicate(timeout=120)
        self.assertEqual(start.returncode, 0, out + err)
        entry = json.loads(out)['processes'][0]
        self.assertEqual([path.name for path in ports.glob('*.json')], [f'{entry["port"]}.json'])
        self.assertEqual(fetch(entry['url']).split('|')[0], str(entry['port']))
        done = self.serve('stop')
        self.assertEqual(done.returncode, 0, done.stdout)
        self.assertEqual(self.payload(done)['reservas_liberadas'], [entry['port']])
        self.assertFalse((self.root / '.frontlights' / 'serve' / '11.lock').exists(), 'a trava da issue vazou')

    def test_stop_of_another_issue_is_not_blocked_by_a_start_in_progress(self):
        self.write_config([self.auto('web-api')])
        flags = self.base / 'flags'
        flags.mkdir()
        start = subprocess.Popen([sys.executable, '-c', SLOW_START, str(SCRIPT.parent), str(self.config),
                                  str(self.root), str(flags)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, encoding='utf-8')
        self.addCleanup(start.communicate)
        self.addCleanup(start.kill)
        self.assertTrue(wait_until(lambda: (flags / 'before-spawn').exists(), 30))
        done = self.serve('stop', issue=12)
        self.assertNotIn('em andamento', done.stdout)
        (flags / 'go').write_text('x')
        out, err = start.communicate(timeout=120)
        self.assertEqual(start.returncode, 0, out + err)


class FakeHttpServer:
    """Accepts connections and answers each with fixed raw bytes, then closes."""

    def __init__(self, payload):
        self.payload = payload
        self.listener = socket.socket()
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen()
        self.port = self.listener.getsockname()[1]
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        while True:
            try:
                connection, _ = self.listener.accept()
            except OSError:
                return
            with connection:
                try:
                    connection.settimeout(1)
                    connection.recv(4096)
                    connection.sendall(self.payload)
                except OSError:
                    pass

    def close(self):
        self.listener.close()


class MalformedAnswerTest(PortsTestCase):
    PAYLOADS = (b'LIXO SEM STATUS\r\n\r\n', b'HTTP/1.1 200 OK\r\n' + b'X' * 70000, b'\x00\x01\x02\xff',
                b'HTTP/1.1 999999 X\r\n\r\n')

    def test_a_server_that_answers_garbage_is_not_healthy_and_never_raises(self):
        for payload in self.PAYLOADS:
            server = FakeHttpServer(payload)
            self.addCleanup(server.close)
            self.assertFalse(serve.healthy(f'http://127.0.0.1:{server.port}/'), payload[:20])

    def test_wait_healthy_turns_a_garbage_answer_into_a_refusal_not_a_traceback(self):
        server = FakeHttpServer(self.PAYLOADS[0])
        self.addCleanup(server.close)
        child = types.SimpleNamespace(poll=lambda: None, returncode=None)
        item = {'name': 'web', 'health': f'http://127.0.0.1:{server.port}/', 'timeoutSeconds': 1}
        with self.assertRaises(serve.Refusal) as caught:
            serve.wait_healthy(child, item)
        self.assertIn('não ficou saudável', str(caught.exception))
        self.assertEqual(caught.exception.process, 'web')


# Tries to take the issue lock of issue 11 and, when it wins, holds it for argv[3] seconds.
COMPETITOR = r"""import sys
import time

sys.path.insert(0, sys.argv[1])
import serve

sys.stdout.reconfigure(encoding='utf-8')
try:
    lock = serve.acquire_lock(sys.argv[2], 11)
except serve.Refusal as refusal:
    print('refused ' + str(refusal), flush=True)
    sys.exit(0)
print('won', flush=True)
time.sleep(float(sys.argv[3]))
serve.release_lock(lock)
"""


class IssueLockCase(PortsTestCase):
    """Helpers only (no tests), so the lock test classes below do not rerun each other's tests."""

    def setUp(self):
        super().setUp()
        self.lock = self.root / '.frontlights' / 'serve' / '11.lock'
        self.lock.parent.mkdir(parents=True)


class IssueLockTest(IssueLockCase):
    """The per-issue lock taken by start and stop: one owner even when a stale lock is replaced concurrently."""

    def test_a_stale_lock_replaced_while_another_caller_competes_ends_with_a_single_owner(self):
        self.lock.write_text(str(dead_pid()), encoding='utf-8')
        real_unlink = os.unlink
        lines = []
        competitors = []

        def hooked(path, *args, **kwargs):
            if os.path.basename(str(path)) == '11.lock' and not competitors:
                # right before the stale lock is removed, another start/stop of the issue competes for it
                competitor = subprocess.Popen([sys.executable, '-c', COMPETITOR, str(SCRIPT.parent), str(self.root),
                                               '4'], stdout=subprocess.PIPE, text=True, encoding='utf-8')
                competitors.append(competitor)
                reader = threading.Thread(target=lambda: lines.append(competitor.stdout.readline().strip()),
                                          daemon=True)
                reader.start()
                reader.join(3)  # time enough for it to win, if nothing stops it
            return real_unlink(path, *args, **kwargs)

        with mock.patch.object(os, 'unlink', hooked):
            mine = serve.acquire_lock(self.root, 11)
        self.addCleanup(competitors[0].kill)
        try:
            self.assertEqual(lines, [], 'o concorrente também virou dono da trava da issue')
            self.assertEqual(self.lock.read_text(encoding='utf-8'), str(os.getpid()))
            self.assertTrue(wait_until(lambda: lines, 30), 'o concorrente não respondeu')
            self.assertTrue(lines[0].startswith('refused'), lines)
            self.assertIn('em andamento', lines[0])
        finally:
            serve.release_lock(mine)
        competitors[0].communicate(timeout=30)
        self.assertFalse(self.lock.exists())

    def test_a_lock_being_deleted_by_its_owner_is_retried_not_a_traceback(self):
        real_open = os.open
        refusals = []

        def deleting(path, flags, *args, **kwargs):
            if os.path.basename(str(path)) == '11.lock' and flags & os.O_EXCL and len(refusals) < 3:
                refusals.append(path)
                raise PermissionError('arquivo sendo apagado')
            return real_open(path, flags, *args, **kwargs)

        with mock.patch.object(os, 'open', deleting):
            mine = serve.acquire_lock(self.root, 11)
        self.assertEqual(len(refusals), 3)
        self.assertEqual(self.lock.read_text(encoding='utf-8'), str(os.getpid()))
        serve.release_lock(mine)
        self.assertFalse(self.lock.exists())

    def test_a_lock_that_stays_undeletable_is_refused_within_the_deadline(self):
        self.lock.write_text(str(os.getpid()), encoding='utf-8')
        real_read = Path.read_text

        def being_deleted(path, *args, **kwargs):
            if path.name == '11.lock':
                raise PermissionError('arquivo sendo apagado')
            return real_read(path, *args, **kwargs)

        started = time.monotonic()
        with mock.patch.object(Path, 'read_text', being_deleted), \
                mock.patch.object(serve, 'RESERVE_LOCK_SECONDS', 1), watchdog():
            with self.assertRaises(serve.Refusal) as caught:
                serve.acquire_lock(self.root, 11)
        self.assertLess(time.monotonic() - started, 6)
        self.assertIn('11.lock', str(caught.exception))

    def test_a_lock_read_while_its_owner_deletes_it_is_retried_not_called_unreadable(self):
        self.lock.write_text(str(os.getpid()), encoding='utf-8')
        real_read = Path.read_text

        def deleted_meanwhile(path, *args, **kwargs):
            if path.name == '11.lock':
                os.unlink(path)  # the owner finished and removed it; Windows answers the read with access denied
                raise PermissionError('arquivo sendo apagado')
            return real_read(path, *args, **kwargs)

        with mock.patch.object(Path, 'read_text', deleted_meanwhile):
            mine = serve.acquire_lock(self.root, 11)
        self.assertEqual(real_read(self.lock, encoding='utf-8'), str(os.getpid()))
        serve.release_lock(mine)

    def test_a_lock_with_bytes_that_are_not_utf8_is_refused_as_unreadable_not_a_traceback(self):
        self.lock.write_bytes(b'\xff\xfe\x00\x80')
        with self.assertRaises(serve.Refusal) as caught:
            serve.acquire_lock(self.root, 11)
        self.assertIn('ilegível', str(caught.exception))
        self.assertEqual(caught.exception.category, 'uso')
        self.assertEqual(self.lock.read_bytes(), b'\xff\xfe\x00\x80')

    def test_the_guard_file_is_the_only_thing_left_besides_the_records(self):
        serve.release_lock(serve.acquire_lock(self.root, 11))
        self.assertEqual([path.name for path in self.lock.parent.iterdir()], ['.locks.guard'])


class LockLoopDeadlineTest(IssueLockCase):
    """Every pass of the issue-lock loop reaches the deadline check, whatever branch it took."""

    def test_a_replacement_that_does_not_really_remove_the_lock_still_ends_at_the_deadline(self):
        self.lock.write_text(str(dead_pid()), encoding='utf-8')
        calls = []

        def pretends(path):
            calls.append(path)
            if len(calls) > 2000:
                raise AssertionError('o laço girou sem checar o prazo nem dormir')
            return True  # says it removed the file; the file stays

        started = time.monotonic()
        with mock.patch.object(serve, 'remove_file', pretends), \
                mock.patch.object(serve, 'RESERVE_LOCK_SECONDS', 2), watchdog():
            with self.assertRaises(serve.Refusal) as caught:
                serve.acquire_lock(self.root, 11)
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 1.9)
        self.assertLess(elapsed, 6)
        self.assertIn('11.lock', str(caught.exception))
        self.assertLess(len(calls), 1000, 'sem uma espera curta o laço gira à toa')

    def test_the_whole_call_including_the_guard_wait_ends_within_one_deadline(self):
        self.lock.write_text('', encoding='utf-8')  # recent and empty: waited for until the deadline
        stamp = time.time() - 1
        os.utime(self.lock, (stamp, stamp))
        held = serve.take_os_lock(self.lock.parent / '.locks.guard', 'ocupada')
        releaser = threading.Timer(1.5, serve.release_os_lock, [held])
        releaser.start()
        started = time.monotonic()
        try:
            with mock.patch.object(serve, 'RESERVE_LOCK_SECONDS', 2), watchdog():
                with self.assertRaises(serve.Refusal):
                    serve.acquire_lock(self.root, 11)
        finally:
            releaser.join()
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 1.9)
        self.assertLess(elapsed, 2.8, 'a espera pela trava-guarda e o laço somaram dois prazos')

    def test_a_persistent_denial_to_create_the_lock_fails_fast_as_infrastructure_with_the_reason(self):
        serve.release_lock(serve.acquire_lock(self.root, 11))  # the guard file exists from now on
        real_open = os.open

        def denied(path, flags, *args, **kwargs):
            if os.path.basename(str(path)) == '11.lock' and flags & os.O_EXCL:
                raise PermissionError(errno.EACCES, os.strerror(errno.EACCES))
            return real_open(path, flags, *args, **kwargs)

        started = time.monotonic()
        with mock.patch.object(os, 'open', denied), mock.patch.object(serve, 'RESERVE_LOCK_SECONDS', 4), watchdog():
            with self.assertRaises(serve.Refusal) as caught:
                serve.acquire_lock(self.root, 11)
        self.assertLess(time.monotonic() - started, 3, 'não falhou rápido')
        self.assertEqual(caught.exception.category, 'infraestrutura')
        self.assertIn(os.strerror(errno.EACCES), str(caught.exception))
        self.assertNotIn('Não foi possível obter a trava', str(caught.exception))

    def test_a_denial_while_the_lock_file_exists_is_contention_and_ends_at_the_deadline_as_usage(self):
        self.lock.write_text(str(os.getpid()), encoding='utf-8')
        real_open, real_read = os.open, Path.read_text

        def denied(path, flags, *args, **kwargs):
            if os.path.basename(str(path)) == '11.lock' and flags & os.O_EXCL:
                raise PermissionError(errno.EACCES, os.strerror(errno.EACCES))
            return real_open(path, flags, *args, **kwargs)

        def being_deleted(path, *args, **kwargs):
            if path.name == '11.lock':
                raise PermissionError('arquivo sendo apagado')
            return real_read(path, *args, **kwargs)

        with mock.patch.object(os, 'open', denied), mock.patch.object(Path, 'read_text', being_deleted), \
                mock.patch.object(serve, 'RESERVE_LOCK_SECONDS', 1), watchdog():
            with self.assertRaises(serve.Refusal) as caught:
                serve.acquire_lock(self.root, 11)
        self.assertEqual(caught.exception.category, 'uso')
        self.assertIn('Não foi possível obter a trava', str(caught.exception))

    def test_a_guard_that_is_a_folder_fails_fast_with_the_reason_instead_of_waiting_for_the_deadline(self):
        (self.lock.parent / '.locks.guard').mkdir()
        started = time.monotonic()
        with mock.patch.object(serve, 'RESERVE_LOCK_SECONDS', 6), watchdog():
            with self.assertRaises(serve.Refusal) as caught:
                serve.acquire_lock(self.root, 11)
        self.assertLess(time.monotonic() - started, 3, 'esperou o prazo inteiro')
        self.assertIn('Falha de entrada e saída', str(caught.exception))
        self.assertEqual(caught.exception.category, 'infraestrutura')

    def test_the_watchdog_fails_a_loop_that_ignores_its_deadline_by_assertion_instead_of_hanging(self):
        started = time.monotonic()
        with self.assertRaises(AssertionError), watchdog(1):
            while True:
                serve.time.sleep(0.02)
        self.assertLess(time.monotonic() - started, 5)


class EmptyLockTest(IssueLockCase):
    """A lock left empty by an owner killed between creating and writing it is not a permanent refusal."""

    def age(self, seconds):
        stamp = time.time() - seconds
        os.utime(self.lock, (stamp, stamp))

    def test_an_empty_lock_older_than_the_limit_is_replaced(self):
        for content in ('', '  \n'):
            self.lock.write_text(content, encoding='utf-8')
            self.age(35)
            with watchdog():
                mine = serve.acquire_lock(self.root, 11)
            self.assertEqual(self.lock.read_text(encoding='utf-8'), str(os.getpid()), repr(content))
            serve.release_lock(mine)
            self.assertFalse(self.lock.exists())

    def test_a_recent_empty_lock_is_waited_for_and_then_refused_without_touching_it(self):
        self.lock.write_text('', encoding='utf-8')
        self.age(1)
        started = time.monotonic()
        with mock.patch.object(serve, 'RESERVE_LOCK_SECONDS', 1), watchdog():
            with self.assertRaises(serve.Refusal) as caught:
                serve.acquire_lock(self.root, 11)
        self.assertLess(time.monotonic() - started, 6)
        self.assertIn('11.lock', str(caught.exception))
        self.assertTrue(self.lock.exists())
        self.assertEqual(self.lock.read_text(encoding='utf-8'), '')

    def test_an_old_empty_lock_that_replacement_reports_removed_but_stays_ends_at_the_deadline(self):
        self.lock.write_text('', encoding='utf-8')
        self.age(3600)
        calls = []

        def pretends(path):
            calls.append(path)
            if len(calls) > 2000:
                raise AssertionError('o laço girou sem checar o prazo nem dormir')
            return True  # says it removed the file; the file stays

        started = time.monotonic()
        with mock.patch.object(serve, 'remove_file', pretends), \
                mock.patch.object(serve, 'RESERVE_LOCK_SECONDS', 2), watchdog():
            with self.assertRaises(serve.Refusal) as caught:
                serve.acquire_lock(self.root, 11)
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 1.9)
        self.assertLess(elapsed, 6)
        self.assertIn('11.lock', str(caught.exception))
        self.assertLess(len(calls), 1000, 'sem uma espera curta o laço gira à toa')

    def test_an_empty_lock_dated_in_the_future_is_refused_with_the_probable_cause(self):
        self.lock.write_text('', encoding='utf-8')
        future = time.time() + 7200
        os.utime(self.lock, (future, future))
        self.addCleanup(self.lock.unlink)  # or the cleanup `stop` would wait for this lock too
        with mock.patch.object(serve, 'RESERVE_LOCK_SECONDS', 1), watchdog():
            with self.assertRaises(serve.Refusal) as caught:
                serve.acquire_lock(self.root, 11)
        message = str(caught.exception)
        self.assertTrue(message.startswith('Não foi possível obter a trava'), message)
        self.assertIn('futuro', message)
        self.assertEqual(caught.exception.category, 'uso')

    def test_a_lock_with_other_text_is_still_refused_as_unreadable(self):
        for content in ('lixo', '12 34', '0'):
            self.lock.write_text(content, encoding='utf-8')
            self.age(3600)
            with self.assertRaises(serve.Refusal) as caught:
                serve.acquire_lock(self.root, 11)
            self.assertIn('ilegível', str(caught.exception), content)
            self.assertEqual(self.lock.read_text(encoding='utf-8'), content)

    def test_two_callers_competing_for_an_old_empty_lock_end_with_a_single_owner(self):
        self.lock.write_text('', encoding='utf-8')
        self.age(3600)
        runs = [subprocess.Popen([sys.executable, '-c', COMPETITOR, str(SCRIPT.parent), str(self.root), '4'],
                                 stdout=subprocess.PIPE, text=True, encoding='utf-8') for _ in range(2)]
        for run in runs:
            self.addCleanup(run.kill)
        lines = [run.stdout.readline().strip() for run in runs]
        for run in runs:
            run.communicate(timeout=60)
        self.assertEqual(sorted(line.split()[0] for line in lines), ['refused', 'won'], lines)
        self.assertFalse(self.lock.exists())


def fail_with(code):
    return OSError(code, os.strerror(code))


class IoFailureTest(GitPortsCase):
    """An I/O error on the lock or reservation folders and files is a refusal with JSON, never a traceback."""

    def assert_infrastructure_refusal(self, done, where=''):
        self.assertEqual(done.returncode, 1, (done.stdout, done.stderr))
        self.assertNotIn('Traceback', done.stderr, where)
        result = self.payload(done)
        self.assertFalse(result['ok'])
        self.assertEqual(result['category'], 'infraestrutura', where)
        self.assertIn('Falha de entrada e saída', result['error'], where)
        self.assertNotIn(PASSWORD, done.stdout)
        return result

    def test_a_reservation_folder_that_is_a_file_refuses_the_start_and_releases_the_issue_lock(self):
        self.make_repo()
        self.write_config([self.auto('web-api')])
        (self.repo / '.git' / 'frontlights-serve').write_text('x', encoding='utf-8')
        done = self.serve('start', root=self.repo)
        self.assert_infrastructure_refusal(done)
        serve_dir = self.repo / '.frontlights' / 'serve'
        self.assertFalse((serve_dir / '11.lock').exists(), 'a trava da issue vazou')
        self.assertFalse((serve_dir / '11.json').exists())

    def test_a_ports_folder_that_is_a_file_outside_git_refuses_the_start_and_releases_the_issue_lock(self):
        self.write_config([self.auto('web-api')])
        serve_dir = self.root / '.frontlights' / 'serve'
        serve_dir.mkdir(parents=True)
        (serve_dir / 'ports').write_text('x', encoding='utf-8')
        done = self.serve('start')
        self.assert_infrastructure_refusal(done)
        self.assertFalse((serve_dir / '11.lock').exists(), 'a trava da issue vazou')

    def test_a_serve_folder_that_is_a_file_refuses_start_and_stop(self):
        self.write_config([self.auto('web-api')])
        (self.root / '.frontlights').mkdir()
        (self.root / '.frontlights' / 'serve').write_text('x', encoding='utf-8')
        for operation in ('start', 'stop'):
            self.assert_infrastructure_refusal(self.serve(operation), operation)

    def test_the_lock_and_reservation_folders_that_are_files_are_refusals_at_the_function_level_too(self):
        (self.root / '.frontlights').mkdir()
        (self.root / '.frontlights' / 'serve').write_text('x', encoding='utf-8')
        with self.assertRaises(serve.Refusal) as caught:
            serve.acquire_lock(self.root, 11)
        self.assertEqual(caught.exception.category, 'infraestrutura')
        blocker = self.base / 'blocker'
        blocker.write_text('x', encoding='utf-8')
        with self.assertRaises(serve.Refusal) as caught:
            with serve.reservation_lock(blocker / 'ports'):
                self.fail('obteve a trava de reserva numa pasta impossível')
        self.assertEqual(caught.exception.category, 'infraestrutura')

    def test_a_failing_write_of_the_issue_lock_refuses_and_leaves_no_empty_lock(self):
        lock = self.root / '.frontlights' / 'serve' / '11.lock'

        def full_disk(descriptor, *args, **kwargs):
            os.close(descriptor)
            raise fail_with(errno.ENOSPC)

        with mock.patch.object(os, 'fdopen', full_disk):
            with self.assertRaises(serve.Refusal) as caught:
                serve.acquire_lock(self.root, 11)
        self.assertEqual(caught.exception.category, 'infraestrutura')
        self.assertIn('gravar a trava', str(caught.exception))
        self.assertFalse(lock.exists(), 'ficou uma trava vazia')
        serve.release_lock(serve.acquire_lock(self.root, 11))  # and the next caller is not blocked

    def test_a_failing_creation_of_the_issue_lock_is_a_refusal(self):
        real_open = os.open

        def broken(path, flags, *args, **kwargs):
            if os.path.basename(str(path)) == '11.lock':
                raise fail_with(errno.EIO)
            return real_open(path, flags, *args, **kwargs)

        with mock.patch.object(os, 'open', broken):
            with self.assertRaises(serve.Refusal) as caught:
                serve.acquire_lock(self.root, 11)
        self.assertEqual(caught.exception.category, 'infraestrutura')
        self.assertIn('Falha de entrada e saída', str(caught.exception))

    def test_a_failing_write_of_a_reservation_refuses_and_leaves_no_empty_reservation(self):
        root = self.base / 'ports-root'
        ports = root / '.frontlights' / 'serve' / 'ports'

        def full_disk(descriptor, *args, **kwargs):
            os.close(descriptor)
            raise fail_with(errno.ENOSPC)

        with mock.patch.object(os, 'fdopen', full_disk):
            with self.assertRaises(serve.Refusal) as caught:
                serve.reserve_port(root, 11, 'web')
        self.assertEqual(caught.exception.category, 'infraestrutura')
        self.assertIn('gravar a reserva', str(caught.exception))
        self.assertEqual([path.name for path in ports.glob('*.json')], [], 'ficou uma reserva vazia')
        with watchdog():  # the reservation lock was released
            serve.release_os_lock(serve.take_reserve_file(ports))

    def test_a_failing_creation_of_a_reservation_is_a_refusal(self):
        root = self.base / 'ports-root'
        real_open = os.open

        def broken(path, flags, *args, **kwargs):
            if str(path).endswith('.json') and flags & os.O_EXCL:
                raise fail_with(errno.EIO)
            return real_open(path, flags, *args, **kwargs)

        with mock.patch.object(os, 'open', broken):
            with self.assertRaises(serve.Refusal) as caught:
                serve.reserve_port(root, 11, 'web')
        self.assertEqual(caught.exception.category, 'infraestrutura')

    def test_a_system_that_offers_no_candidate_port_is_a_refusal(self):
        def no_socket(*args, **kwargs):
            raise fail_with(errno.EMFILE)

        with mock.patch.object(serve.socket, 'socket', no_socket):
            with self.assertRaises(serve.Refusal) as caught:
                serve.reserve_port(self.base / 'ports-root', 11, 'web')
        self.assertEqual(caught.exception.category, 'infraestrutura')
        self.assertIn('Falha de entrada e saída', str(caught.exception))

    def start_in_process(self, processes):
        self.write_config(processes)
        spawned = []
        real_spawn = serve.spawn

        def recording(*args, **kwargs):
            child = real_spawn(*args, **kwargs)
            spawned.append(child)
            self.addCleanup(lambda: child.poll() is None and child.kill())
            return child

        return spawned, mock.patch.object(serve, 'spawn', recording)

    def test_a_failing_write_before_the_spawn_spawns_nothing_and_releases_everything(self):
        spawned, recording = self.start_in_process([self.auto('web-api')])

        def broken(source, target):
            raise fail_with(errno.EIO)

        with recording, mock.patch.object(os, 'replace', broken):
            with self.assertRaises(serve.Refusal) as caught:
                serve.start(self.config, self.root, 11)
        self.assertEqual(caught.exception.category, 'infraestrutura')
        self.assertEqual(spawned, [])
        serve_dir = self.root / '.frontlights' / 'serve'
        self.assertFalse((serve_dir / '11.lock').exists(), 'a trava da issue vazou')
        self.assertEqual(list((serve_dir / 'ports').glob('*.json')), [], 'a reserva vazou')

    def test_a_failing_registry_write_after_the_spawn_tears_the_process_down(self):
        spawned, recording = self.start_in_process([self.auto('web-api')])
        real_replace = os.replace

        def broken(source, target):
            if os.path.basename(str(target)) == '11.json':
                raise fail_with(errno.EIO)
            return real_replace(source, target)

        with recording, mock.patch.object(os, 'replace', broken):
            with self.assertRaises(serve.Refusal) as caught:
                serve.start(self.config, self.root, 11)
        self.assertEqual(caught.exception.category, 'infraestrutura')
        self.assertEqual(len(spawned), 1)
        self.assertTrue(wait_until(lambda: not serve.pid_alive(spawned[0].pid), 20), 'o filho vazou')
        serve_dir = self.root / '.frontlights' / 'serve'
        self.assertFalse((serve_dir / '11.lock').exists(), 'a trava da issue vazou')
        self.assertEqual(list((serve_dir / 'ports').glob('*.json')), [], 'a reserva vazou')

    def test_a_teardown_that_cannot_rewrite_the_registry_still_reports_the_original_failure(self):
        spawned, recording = self.start_in_process([self.auto('web-api')])
        real_replace = os.replace

        def broken(source, target):
            if os.path.basename(str(target)) == '11.json':
                raise fail_with(errno.EIO)
            return real_replace(source, target)

        with recording, mock.patch.object(os, 'replace', broken), \
                mock.patch.object(serve, 'kill_tree', lambda pid: False):
            with self.assertRaises(serve.Refusal) as caught:
                serve.start(self.config, self.root, 11)
        self.assertEqual(caught.exception.category, 'infraestrutura')
        self.assertIn('Falha de entrada e saída', str(caught.exception))
        self.assertIn('Não foi possível derrubar', str(caught.exception))
        self.assertFalse((self.root / '.frontlights' / 'serve' / '11.lock').exists(), 'a trava da issue vazou')

    def test_a_registry_that_cannot_be_deleted_by_stop_is_a_refusal_and_the_lock_is_released(self):
        self.write_config([self.auto('web-api')])
        self.assertEqual(self.serve('start').returncode, 0, self.last.stdout)
        real_unlink = os.unlink

        def broken(path, *args, **kwargs):
            if os.path.basename(str(path)) == '11.json':
                raise fail_with(errno.EIO)
            return real_unlink(path, *args, **kwargs)

        port = json.loads((self.root / '.frontlights' / 'serve' / '11.json').read_text(encoding='utf-8'))[
            'processes'][0]['port']
        with mock.patch.object(os, 'unlink', broken):
            with self.assertRaises(serve.Refusal) as caught:
                serve.stop(self.root, 11)
        self.assertEqual(caught.exception.category, 'infraestrutura')
        self.assertIn('11.json', str(caught.exception))
        self.assertIn(os.strerror(errno.EIO), str(caught.exception))
        self.assertEqual([entry['stopped'] for entry in caught.exception.extra['processes']], [True])
        self.assertEqual(caught.exception.extra['reservas_liberadas'], [port])
        self.assertEqual(list(self.root.glob('.frontlights/serve/ports/*.json')), [], 'a reserva vazou')
        self.assertFalse((self.root / '.frontlights' / 'serve' / '11.lock').exists(), 'a trava da issue vazou')
        self.assertEqual(self.serve('stop').returncode, 0, self.last.stdout)  # and stop can be retried

    def test_the_reason_of_an_io_error_is_part_of_the_message(self):
        def disk_full(self, *args, **kwargs):
            raise fail_with(errno.ENOSPC)

        with mock.patch.object(Path, 'write_text', disk_full):
            with self.assertRaises(serve.Refusal) as caught:
                serve.write_json_atomic({'a': 1}, self.root / 'x.json')
        self.assertIn(os.strerror(errno.ENOSPC), str(caught.exception))
        self.assertEqual(caught.exception.category, 'infraestrutura')

    def test_an_unreadable_reservation_folder_does_not_break_the_release(self):
        def broken(path, pattern):
            raise fail_with(errno.EIO)

        (self.root / '.frontlights' / 'serve' / 'ports').mkdir(parents=True)
        with mock.patch.object(Path, 'glob', broken):
            self.assertEqual(serve.release_ports(self.root, 11), [])

    def test_an_oserror_that_escapes_every_handler_still_ends_as_json(self):
        out = io.StringIO()

        def explodes(*args, **kwargs):
            raise fail_with(errno.EIO)

        with mock.patch.object(serve, 'run', explodes), contextlib.redirect_stdout(out):
            code = serve.main(['start', '--config', str(self.config), '--root', str(self.root), '--issue', '11'])
        self.assertEqual(code, 1)
        result = json.loads(out.getvalue())
        self.assertEqual((result['ok'], result['category']), (False, 'infraestrutura'))
        self.assertIn('Falha de entrada e saída', result['error'])


# Writes a marker file and stays alive: if it ever runs, the marker proves a spawn.
MARKER_SERVER = "import pathlib, sys, time; pathlib.Path(sys.argv[1]).write_text('x'); time.sleep(30)"


class ConfigPortRangeTest(PortsTestCase):
    """A port outside 1..65535 (or other unusable config) is refused while the config is read: nothing is spawned."""

    def refused(self, processes, text):
        self.write_config(processes)
        done = self.serve('start')
        self.assertEqual(done.returncode, 1, (text, done.stdout, done.stderr))
        self.assertNotIn('Traceback', done.stderr, text)
        result = self.payload(done)
        self.assertEqual(result['category'], 'uso', text)
        self.assertEqual(result['failedProcess'], 'web', text)
        time.sleep(0.5)
        self.assertFalse((self.base / 'marker').exists(), f'{text}: um processo foi criado')
        self.assertFalse((self.root / '.frontlights' / 'serve' / '11.json').exists(), text)
        self.assertEqual(list(self.root.glob('.frontlights/serve/ports/*.json')), [], f'{text}: porta reservada')
        self.assertFalse((self.root / '.frontlights' / 'serve' / '11.lock').exists(), f'{text}: trava vazou')
        return result

    def process(self, **fields):
        item = {'name': 'web', 'argv': [sys.executable, '-c', MARKER_SERVER, str(self.base / 'marker')],
                'health': 'http://127.0.0.1:8080/', 'timeoutSeconds': 5}
        item.update(fields)
        return item

    def test_a_health_url_with_a_port_outside_the_range_is_refused_before_any_spawn(self):
        for health in ('http://127.0.0.1:99999/', 'http://127.0.0.1:0/', 'http://127.0.0.1:65536/',
                       'http://127.0.0.1:abc/', 'http://[::1/'):
            result = self.refused([self.process(health=health)], health)
            self.assertIn('web', result['error'])

    def test_a_fixed_port_outside_the_range_is_refused_before_any_spawn(self):
        for port in (0, -1, 65536, 70000):
            self.refused([self.process(port=port)], str(port))

    def test_the_port_edges_are_accepted(self):
        for health, port in (('http://127.0.0.1:1/', None), ('http://127.0.0.1:65535/', None),
                             ('http://127.0.0.1/', None), ('https://127.0.0.1/', None), ('http://127.0.0.1:1/', 65535)):
            fields = {'health': health}
            if port:
                fields['port'] = port
            self.write_config([self.process(**fields)])
            self.assertEqual(len(serve.load_block(self.config)), 1, (health, port))

    def variant(self, auto, **fields):
        item = self.process(port='auto', health='http://127.0.0.1:{port}/') if auto else self.process()
        item.update(fields)
        return item

    def refused_after_a_good_one(self, bad, text):
        """The bad process comes second: refusing it must happen before the first one is spawned or reserved."""
        good = self.variant(True, name='first')
        return self.refused([good, bad], text)

    def test_a_nul_character_in_the_command_health_or_cwd_is_refused_before_any_spawn(self):
        marker = str(self.base / 'marker')
        for auto in (False, True):
            for argv in (['py\u0000thon', '-c', MARKER_SERVER, marker], [sys.executable, 'a\u0000b'],
                         [sys.executable, '-c', MARKER_SERVER, marker, 'a', 'b\u0000c', 'd']):
                self.refused_after_a_good_one(self.variant(auto, argv=argv), f'argv {argv!r} auto={auto}')
            self.refused_after_a_good_one(self.variant(auto, cwd='a\u0000b'), f'cwd auto={auto}')
            health = 'http://127.0.0.1:{port}/\u0000' if auto else 'http://127.0.0.1:8080/\u0000'
            self.refused_after_a_good_one(self.variant(auto, health=health), f'health auto={auto}')

    def test_a_timeout_that_is_not_a_positive_number_is_refused_before_any_spawn(self):
        for value in (-1, 0, 'x', True, float('nan')):
            self.refused_after_a_good_one(self.variant(False, timeoutSeconds=value), repr(value))

    def test_a_cwd_that_is_absolute_missing_or_outside_the_worktree_is_refused_before_any_spawn(self):
        for cwd in (str(self.base), 'nao-existe', '..'):
            self.refused_after_a_good_one(self.variant(False, cwd=cwd), cwd)

    def test_a_health_url_with_credentials_is_refused_before_any_spawn(self):
        for health in ('http://u:p@127.0.0.1:8080/', 'http://u@127.0.0.1:8080/', 'http://:p@127.0.0.1:8080/'):
            self.refused_after_a_good_one(self.variant(False, health=health), health)
            self.refused_after_a_good_one(self.variant(True, health=health.replace('8080', '{port}')), health)

    def test_a_port_that_is_not_an_integer_or_auto_is_refused_as_usage_before_any_spawn(self):
        for port in (True, 1.5, 'abc', '8080'):
            self.refused_after_a_good_one(self.variant(False, port=port), repr(port))

    def test_an_auto_health_url_must_carry_the_placeholder_in_its_port(self):
        for health in ('http://127.0.0.1:3000/{port}', 'http://127.0.0.1/{port}', 'http://127.0.0.1:3000/?p={port}',
                       'http://{port}.example.test:3000/'):
            result = self.refused_after_a_good_one(self.variant(True, health=health), health)
            self.assertIn('{port}', result['error'])
        for health in ('http://127.0.0.1:{port}/x', 'http://localhost:{port}', 'http://[::1]:{port}/',
                       'https://127.0.0.1:{port}/a?b=1'):
            self.write_config([self.variant(True, health=health)])
            self.assertEqual(len(serve.load_block(self.config)), 1, health)

    def test_a_cwd_that_is_not_text_is_refused_before_any_spawn(self):
        for cwd in (5, ['a'], {'a': 1}, True):
            self.refused([self.process(cwd=cwd)], repr(cwd))

    def test_the_process_port_follows_the_validated_rules(self):
        self.assertEqual(serve.process_port({'port': 8080, 'health': 'http://127.0.0.1:1/'}), 8080)
        self.assertEqual(serve.process_port({'health': 'http://127.0.0.1:4321/'}), 4321)
        self.assertEqual(serve.process_port({'health': 'http://h/'}), 80)
        self.assertEqual(serve.process_port({'health': 'https://h/'}), 443)


def deny(code=errno.EACCES):
    return PermissionError(code, os.strerror(code))


class PersistentDenialTest(PortsTestCase):
    """A denial that persists with no competing file is an infrastructure failure, fast and with the reason;
    contention and a file being deleted keep being retried."""

    def creating(self, outcome):
        """os.open that fails the exclusive creation of a reservation as `outcome` says (an exception or None)."""
        real_open, calls = os.open, []

        def hooked(path, flags, *args, **kwargs):
            if str(path).endswith('.json') and flags & os.O_EXCL:
                calls.append(path)
                if outcome(len(calls)) is not None:
                    raise outcome(len(calls))
            return real_open(path, flags, *args, **kwargs)

        return mock.patch.object(os, 'open', hooked), calls

    def test_a_reservation_denied_over_and_over_fails_fast_as_infrastructure_with_the_reason(self):
        patch, calls = self.creating(lambda number: deny())
        started = time.monotonic()
        with patch:
            with self.assertRaises(serve.Refusal) as caught:
                serve.reserve_port(self.root, 11, 'web')
        self.assertEqual(caught.exception.category, 'infraestrutura')
        self.assertIn('Falha de entrada e saída', str(caught.exception))
        self.assertIn(os.strerror(errno.EACCES), str(caught.exception))
        self.assertLess(len(calls), 60, 'tentou quase todas as portas candidatas')
        self.assertLess(time.monotonic() - started, 20)

    def test_a_reservation_denied_only_a_few_times_is_still_made(self):
        patch, calls = self.creating(lambda number: deny() if number <= 3 else None)
        with patch:
            port = serve.reserve_port(self.root, 11, 'web')
        self.assertTrue((serve.ports_dir(self.root) / f'{port}.json').is_file())
        self.assertEqual(len(calls), 4)

    def test_a_reservation_that_always_exists_is_contention_not_a_denial(self):
        patch, calls = self.creating(lambda number: FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST)))
        with patch, mock.patch.object(serve, 'PORT_ATTEMPTS', 8):
            with self.assertRaises(serve.Refusal) as caught:
                serve.reserve_port(self.root, 11, 'web')
        self.assertIn('Não foi possível reservar uma porta livre', str(caught.exception))
        self.assertEqual(len(calls), 8)

    def test_a_reservation_denied_while_its_file_exists_is_contention_not_a_denial(self):
        real_open, calls = os.open, []

        def hooked(path, flags, *args, **kwargs):
            if str(path).endswith('.json') and flags & os.O_EXCL:
                calls.append(path)
                Path(path).write_text('{}', encoding='utf-8')  # the competing file is there
                raise deny()
            return real_open(path, flags, *args, **kwargs)

        with mock.patch.object(os, 'open', hooked), mock.patch.object(serve, 'PORT_ATTEMPTS', 8), \
                mock.patch.object(serve, 'reservation_live', lambda path: False), \
                mock.patch.object(serve, 'remove_file', lambda path: True):
            with self.assertRaises(serve.Refusal) as caught:
                serve.reserve_port(self.root, 11, 'web')
        self.assertIn('Não foi possível reservar uma porta livre', str(caught.exception))
        self.assertEqual(len(calls), 8)

    def test_a_reserve_lock_that_is_a_folder_fails_fast_with_the_reason(self):
        directory = self.root / 'ports'
        (directory / '.reserve.lock').mkdir(parents=True)
        started = time.monotonic()
        with mock.patch.object(serve, 'RESERVE_LOCK_SECONDS', 8):
            with self.assertRaises(serve.Refusal) as caught:
                with serve.reservation_lock(directory):
                    pass
        self.assertLess(time.monotonic() - started, 4, 'esperou o prazo inteiro')
        self.assertIn('Falha de entrada e saída', str(caught.exception))


@unittest.skipUnless(os.name == 'nt', 'ACL real do Windows (icacls)')
class RealAclDenialTest(PortsTestCase):
    """The same rules with a real permission denial on a folder (icacls), restored at the end."""

    def deny_writes(self, folder):
        user = subprocess.run(['whoami'], capture_output=True, text=True, timeout=30).stdout.strip()
        done = subprocess.run(['icacls', str(folder), '/deny', f'{user}:(WD,AD)'], capture_output=True, text=True,
                              timeout=60)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.addCleanup(lambda: subprocess.run(['icacls', str(folder), '/remove:d', user], capture_output=True,
                                               timeout=60))

    def assert_fast_infrastructure(self, started):
        self.assertEqual(self.last.returncode, 1, (self.last.stdout, self.last.stderr))
        self.assertNotIn('Traceback', self.last.stderr)
        result = self.payload(self.last)
        self.assertEqual(result['category'], 'infraestrutura', result)
        self.assertIn('Falha de entrada e saída', result['error'])
        self.assertIn(os.strerror(errno.EACCES), result['error'])
        self.assertLess(time.monotonic() - started, 25, 'demorou como a espera do prazo inteiro')
        self.assertFalse((self.root / '.frontlights' / 'serve' / '11.lock').exists(), 'a trava da issue vazou')

    def test_a_reservation_folder_that_denies_writes_fails_the_start_fast_as_infrastructure(self):
        self.write_config([self.auto('web-api')])
        ports = self.root / '.frontlights' / 'serve' / 'ports'
        ports.mkdir(parents=True)
        (ports / '.reserve.lock').write_bytes(b'')  # the lock file exists: only the reservation itself is denied
        self.deny_writes(ports)
        started = time.monotonic()
        self.serve('start')
        self.assert_fast_infrastructure(started)

    def test_an_issue_folder_that_denies_writes_fails_the_start_fast_as_infrastructure(self):
        self.write_config([self.auto('web-api')])
        folder = self.root / '.frontlights' / 'serve'
        serve.release_lock(serve.acquire_lock(self.root, 11))  # creates the guard file before the denial
        self.deny_writes(folder)
        started = time.monotonic()
        self.serve('start')
        self.assert_fast_infrastructure(started)


class PidAliveTest(PortsTestCase):
    BAD = (0, -1, -2 ** 31, 2 ** 31, 2 ** 40, 10 ** 30, True, False, '1', '123', 1.0, 1.5, None, [1])

    def test_an_invalid_pid_is_dead_and_never_reaches_the_operating_system(self):
        import ctypes
        with mock.patch.object(os, 'kill', side_effect=AssertionError('os.kill chamado')) as kill, \
                mock.patch.object(ctypes, 'windll', create=True) as windll:
            for value in self.BAD:
                self.assertIs(serve.pid_alive(value), False, repr(value))
            kill.assert_not_called()
            windll.kernel32.OpenProcess.assert_not_called()

    def test_a_real_pid_is_alive_and_a_dead_one_is_not(self):
        self.assertTrue(serve.pid_alive(os.getpid()))
        self.assertFalse(serve.pid_alive(dead_pid()))

    def test_a_child_record_without_a_pid_is_ignored_not_a_crash(self):
        path = self.base / '1.json'
        for child in ({'identity': 'x'}, {}, {'pid': None}, {'pid': 0}):
            path.write_text(json.dumps({'issue': 5, 'process': 'p', 'root': 'x', 'pid': dead_pid(), 'child': child}),
                            encoding='utf-8')
            self.assertFalse(serve.reservation_live(path), repr(child))


if __name__ == '__main__':
    unittest.main()
