import contextlib
import io
import itertools
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import checks

SUITE_ARGV = [sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-v']
GIT_ENV = {**os.environ, 'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.invalid',
           'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@example.invalid'}


_REWRITES = itertools.count(1)


def git(path, *args):
    subprocess.run(['git', '-C', str(path), *args], check=True, capture_output=True, env=GIT_ENV)


def make_suite(path, tests, message='suite'):
    """Tiny fake suite: `tests` maps a test name to True (falha) ou a um texto (falha com essa mensagem)."""
    (path / 'tests').mkdir(parents=True, exist_ok=True)
    body = ['import unittest', 'class FakeCase(unittest.TestCase):']
    for name, fails in sorted(tests.items()):
        if isinstance(fails, str):
            body += [f'    def test_{name}(self):', f'        self.fail({fails!r})']
        else:
            body += [f'    def test_{name}(self):', f'        self.assertEqual({1 if fails else 0}, 0)']
    (path / 'tests' / 'test_fake.py').write_text('\n'.join(body) + '\n', encoding='utf-8')
    if not (path / '.git').exists():
        git(path, 'init', '-q')
    git(path, 'add', '-A')
    git(path, 'commit', '-q', '--allow-empty', '-m', message)


class ChecksTestCase(unittest.TestCase):
    def setUp(self):
        # A suíte falsa é reescrita várias vezes: nenhum .pyc pode sobreviver entre as execuções.
        patcher = mock.patch.dict(os.environ, {'PYTHONDONTWRITEBYTECODE': '1'})
        patcher.start()
        self.addCleanup(patcher.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name) / 'base'
        self.root = Path(tmp.name) / 'branch'
        self.base.mkdir()
        self.root.mkdir()
        self.tmp = Path(tmp.name)
        self.config = Path(tmp.name) / 'config.json'
        self.write_config({'regression': {'argv': SUITE_ARGV, 'timeoutSeconds': 60}})

    def write_config(self, checks_block):
        self.config.write_text(json.dumps({'checks': checks_block}), encoding='utf-8')

    def script(self, code, *extra):
        """Argv que roda `code` como arquivo de script (a recusa de `python -c` vale para o config)."""
        folder = self.tmp / 'scripts'
        folder.mkdir(exist_ok=True)
        path = folder / f'script{len(list(folder.iterdir()))}.py'
        path.write_text(code, encoding='utf-8')
        return [sys.executable, str(path), *extra]

    def regression(self, issue=10):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = checks.main(['regression', '--config', str(self.config), '--root', str(self.root),
                                '--base', str(self.base), '--issue', str(issue)])
        return code, json.loads(out.getvalue())


class RegressionComparisonTest(ChecksTestCase):
    def test_failure_only_in_branch_is_new_and_blocks(self):
        make_suite(self.base, {'alpha': False, 'beta': False})
        make_suite(self.root, {'alpha': False, 'beta': True})
        code, result = self.regression()
        self.assertEqual(code, 1)
        self.assertTrue(result['blocking'])
        self.assertEqual(len(result['new_failures']), 1)
        self.assertIn('test_beta', result['new_failures'][0])
        self.assertEqual(result['existing_failures'], [])
        self.assertEqual(result['fixed'], [])

    def test_failure_already_in_base_is_reported_apart_and_does_not_block(self):
        make_suite(self.base, {'alpha': False, 'old': True})
        make_suite(self.root, {'alpha': False, 'old': True})
        code, result = self.regression()
        self.assertEqual(code, 0)
        self.assertFalse(result['blocking'])
        self.assertEqual(result['new_failures'], [])
        self.assertEqual(len(result['existing_failures']), 1)
        self.assertIn('test_old', result['existing_failures'][0])

    def test_new_old_and_fixed_failures_are_separated_in_one_run(self):
        make_suite(self.base, {'alpha': False, 'old': True, 'healed': True})
        make_suite(self.root, {'alpha': False, 'old': True, 'healed': False, 'fresh': True})
        code, result = self.regression()
        self.assertEqual(code, 1)
        self.assertEqual([('test_fresh' in n) for n in result['new_failures']], [True])
        self.assertEqual([('test_old' in n) for n in result['existing_failures']], [True])
        self.assertEqual([('test_healed' in n) for n in result['fixed']], [True])


    def test_same_name_with_a_different_cause_is_a_new_changed_failure(self):
        make_suite(self.base, {'a': 'causa antiga na base'})
        make_suite(self.root, {'a': 'causa diferente na branch'})
        code, result = self.regression()
        self.assertEqual(code, 1)
        self.assertTrue(result['blocking'])
        self.assertEqual(len(result['new_failures']), 1)
        self.assertEqual(result['new_failures'], result['changed_failures'])
        self.assertEqual(result['existing_failures'], [])

    def test_same_name_with_the_same_cause_is_existing(self):
        make_suite(self.base, {'a': 'mesma causa'})
        make_suite(self.root, {'a': 'mesma causa'})
        code, result = self.regression()
        self.assertEqual(code, 0)
        self.assertEqual(len(result['existing_failures']), 1)
        self.assertEqual(result['changed_failures'], [])
        self.assertEqual(result['new_failures'], [])

    def test_fingerprint_ignores_paths_addresses_and_line_numbers(self):
        a = checks.normalize_message(r'Erro em C:\Users\x\app.py line 10 <Obj at 0x7f12ab34cd56> em /tmp/abc/def.py')
        b = checks.normalize_message(r'Erro em D:\outro\app.py line 99 <Obj at 0x55aa00ff1122> em /var/zzz/ghi.py')
        self.assertEqual(a, b)

    def test_fingerprint_keeps_distinct_hexadecimal_values(self):
        self.assertNotEqual(checks.normalize_message('0x10 != 0x20'), checks.normalize_message('0xdead != 0xbeef'))
        self.assertNotEqual(checks.fingerprint('esperado 0xdead'), checks.fingerprint('esperado 0xbeef'))
        self.assertEqual(checks.normalize_message('<Obj at 0x1a2b3c>'), checks.normalize_message('<Obj at 0x9f8e7d>'))
        self.assertEqual(checks.normalize_message('ptr 0x7f12ab34cd56ef'),
                         checks.normalize_message('ptr 0x55aa00ff112233'))

    def test_flaky_check_is_reported_as_not_done_only_with_new_failures(self):
        make_suite(self.base, {'alpha': False})
        make_suite(self.root, {'alpha': False, 'fresh': True})
        _, result = self.regression()
        self.assertEqual(result['flaky_check'], 'nao_realizado')
        make_suite(self.root, {'alpha': False, 'fresh': False})
        _, result = self.regression()
        self.assertNotIn('flaky_check', result)


class PytestOutputComparisonTest(ChecksTestCase):
    def two_outputs(self, base_lines, branch_lines):
        """Suíte que imprime, no estilo pytest, `base_lines` na base e `branch_lines` na branch."""
        make_suite(self.base, {'alpha': False})
        make_suite(self.root, {'alpha': False})
        script = ('import os, sys\n'
                  f'base = {base_lines!r}\n'
                  f'branch = {branch_lines!r}\n'
                  'lines = base if os.path.basename(os.getcwd()) == "base" else branch\n'
                  'print(chr(10).join(lines))\n'
                  'sys.exit(1 if any(l.startswith(("FAILED", "ERROR")) for l in lines) else 0)\n')
        self.write_config({'regression': {'argv': self.script(script), 'timeoutSeconds': 60}})
        return self.regression()

    def record(self, which):
        path = self.root / '.frontlights' / 'issues' / '10' / 'checks' / f'regression-{which}.json'
        return json.loads(path.read_text(encoding='utf-8'))

    def test_parametrized_ids_with_spaces_are_not_truncated(self):
        code, result = self.two_outputs(
            ['FAILED tests/t.py::test_x[a b] - assert 1 == 2', '1 failed, 3 passed in 0.1s'],
            ['FAILED tests/t.py::test_x[a c] - assert 1 == 2', '1 failed, 3 passed in 0.1s'])
        self.assertEqual(code, 1)
        self.assertEqual(result['new_failures'], ['tests/t.py::test_x[a c]'])
        self.assertEqual(result['fixed'], ['tests/t.py::test_x[a b]'])

    def test_parametrized_id_with_spaces_and_no_message_is_extracted_whole(self):
        code, result = self.two_outputs(
            ['ERROR tests/t.py::test_x[a b]', '1 error in 0.1s'],
            ['ERROR tests/t.py::test_x[a b]', '1 error in 0.1s'])
        self.assertEqual(code, 0)
        self.assertEqual(result['existing_failures'], ['tests/t.py::test_x[a b]'])

    def test_cause_only_in_the_branch_blocks_as_changed(self):
        code, result = self.two_outputs(
            ['FAILED tests/t.py::test_x', '1 failed in 0.1s'],
            ['FAILED tests/t.py::test_x - ZeroDivisionError: division by zero', '1 failed in 0.1s'])
        self.assertEqual(code, 1)
        self.assertEqual(result['changed_failures'], ['tests/t.py::test_x'])
        self.assertEqual(result['new_failures'], ['tests/t.py::test_x'])

    def test_cause_only_in_the_base_also_counts_as_changed(self):
        code, result = self.two_outputs(
            ['FAILED tests/t.py::test_x - ZeroDivisionError', '1 failed in 0.1s'],
            ['FAILED tests/t.py::test_x', '1 failed in 0.1s'])
        self.assertEqual(code, 1)
        self.assertEqual(result['changed_failures'], ['tests/t.py::test_x'])

    def test_both_without_cause_stay_existing(self):
        code, result = self.two_outputs(
            ['FAILED tests/t.py::test_x', '1 failed in 0.1s'],
            ['FAILED tests/t.py::test_x', '1 failed in 0.1s'])
        self.assertEqual(code, 0)
        self.assertEqual(result['existing_failures'], ['tests/t.py::test_x'])

    def test_bigger_failure_count_with_the_same_names_blocks(self):
        code, result = self.two_outputs(
            ['FAILED tests/t.py::test_x - boom', '1 failed, 3 passed in 0.1s'],
            ['FAILED tests/t.py::test_x - boom', '3 failed, 1 passed in 0.1s'])
        self.assertEqual(code, 1)
        self.assertTrue(result['blocking'])
        self.assertEqual(len(result['new_failures']), 1)
        self.assertEqual(result['changed_failures'], result['new_failures'])
        self.assertIn('3', result['new_failures'][0])
        self.assertEqual(result['existing_failures'], ['tests/t.py::test_x'])

    def test_smaller_failure_count_with_the_same_names_does_not_block(self):
        code, result = self.two_outputs(
            ['FAILED tests/t.py::test_x - boom', '3 failed, 1 passed in 0.1s'],
            ['FAILED tests/t.py::test_x - boom', '1 failed, 3 passed in 0.1s'])
        self.assertEqual(code, 0)

    def test_extra_error_with_another_cause_under_the_same_name_is_detected(self):
        block = lambda kind, cause: [f'{kind}: test_a (m.C.test_a)', '-' * 70, 'Traceback (most recent call last):',
                                     '  File "x.py", line 1, in f', cause, '']
        base = block('FAIL', 'AssertionError: x') + block('ERROR', 'AssertionError: x')
        branch = block('FAIL', 'AssertionError: x') + block('ERROR', 'RuntimeError: teardown')
        tail = ['=' * 70, 'Ran 2 tests in 0.1s', '', 'FAILED (failures=1, errors=1)']
        code, result = self.two_outputs(base + tail, branch + tail)
        self.assertEqual(code, 1)
        self.assertEqual(result['changed_failures'], ['test_a (m.C.test_a)'])
        self.assertEqual(len(self.record('branch')['failure_fingerprints']['test_a (m.C.test_a)']), 2)


class RecordsTest(ChecksTestCase):
    def record(self, which):
        path = self.root / '.frontlights' / 'issues' / '10' / 'checks' / f'regression-{which}.json'
        return json.loads(path.read_text(encoding='utf-8'))

    def head(self, path):
        done = subprocess.run(['git', '-C', str(path), 'rev-parse', 'HEAD'], capture_output=True, text=True)
        return done.stdout.strip()

    def test_each_run_is_recorded_under_the_issue_with_head_and_diff_hash(self):
        make_suite(self.base, {'alpha': False, 'old': True}, 'base commit')
        make_suite(self.root, {'alpha': False, 'old': True, 'fresh': True}, 'branch commit')
        (self.root / 'tests' / 'dirty.txt').write_text('uncommitted', encoding='utf-8')
        git(self.root, 'add', '-N', 'tests/dirty.txt')
        self.regression()
        base, branch = self.record('base'), self.record('branch')
        self.assertEqual(base['argv'], SUITE_ARGV)
        self.assertEqual(base['exit_code'], 1)
        self.assertEqual(len(base['failures']), 1)
        self.assertEqual(len(branch['failures']), 2)
        self.assertEqual(branch['tests_run'], 3)
        self.assertEqual(base['head'], self.head(self.base))
        self.assertEqual(branch['head'], self.head(self.root))
        self.assertNotEqual(base['head'], branch['head'])
        self.assertRegex(branch['timestamp'], r'^\d{4}-\d\d-\d\dT')
        self.assertRegex(branch['diff_sha256'], r'^[0-9a-f]{64}$')
        self.assertNotEqual(base['diff_sha256'], branch['diff_sha256'])

    def test_diff_hash_follows_uncommitted_changes_but_not_the_records(self):
        make_suite(self.base, {'alpha': False})
        make_suite(self.root, {'alpha': False})
        self.regression()
        clean = self.record('branch')['diff_sha256']
        self.regression()
        self.assertEqual(self.record('branch')['diff_sha256'], clean)
        (self.root / 'tests' / 'test_fake.py').write_text('import unittest' + chr(10), encoding='utf-8')
        self.regression()
        self.assertNotEqual(self.record('branch')['diff_sha256'], clean)

    def worktree_pair(self):
        """Base e branch como worktrees do mesmo repositório (há merge-base)."""
        make_suite(self.base, {'alpha': False}, 'base commit')
        git(self.base, 'worktree', 'add', '-q', '-b', 'feature', str(self.root))

    def test_diff_vs_base_follows_commits_and_dirty_tree_while_plain_diff_ignores_commits(self):
        self.worktree_pair()
        self.regression()
        clean_plain = self.record('branch')['diff_sha256']
        clean_vs = self.record('branch')['diff_sha256_vs_base']
        self.assertEqual(self.record('base')['diff_sha256_vs_base'], clean_vs)
        make_suite(self.root, {'alpha': False, 'extra': False}, 'branch commit')
        self.regression()
        committed = self.record('branch')
        self.assertEqual(committed['diff_sha256'], clean_plain, 'diff HEAD continua vazio em branch commitada')
        self.assertNotEqual(committed['diff_sha256_vs_base'], clean_vs)
        self.assertNotEqual(committed['diff_sha256_vs_base'], self.record('base')['diff_sha256_vs_base'])
        (self.root / 'tests' / 'test_fake.py').write_text('import unittest' + chr(10), encoding='utf-8')
        self.regression()
        dirty = self.record('branch')
        self.assertNotEqual(dirty['diff_sha256'], clean_plain)
        self.assertNotEqual(dirty['diff_sha256_vs_base'], committed['diff_sha256_vs_base'])

    def test_files_hash_follows_files_but_not_the_records(self):
        make_suite(self.base, {'alpha': False})
        make_suite(self.root, {'alpha': False})
        self.regression()  # a primeira execução deixa __pycache__ (não ignorado) na árvore
        self.regression()
        first = self.record('branch')['files_sha256']
        self.regression()
        self.assertEqual(self.record('branch')['files_sha256'], first)
        (self.root / 'notes.txt').write_text('novo', encoding='utf-8')
        self.regression()
        self.assertNotEqual(self.record('branch')['files_sha256'], first)

    def test_argv_secrets_never_reach_the_records_or_the_output(self):
        make_suite(self.base, {'alpha': False})
        make_suite(self.root, {'alpha': False})
        script = 'import sys; sys.stderr.write("Ran 1 test in 0s" + chr(10))'
        argv = self.script(script, '--password=hunter22xx', '--token', 'abcdef123456', 'admin:s3cretpw')
        self.write_config({'regression': {'argv': argv, 'timeoutSeconds': 60}})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = checks.main(['regression', '--config', str(self.config), '--root', str(self.root),
                                '--base', str(self.base), '--issue', '10'])
        self.assertEqual(code, 0)
        texts = [out.getvalue()] + [r.read_text(encoding='utf-8') for r in
                                    (self.root / '.frontlights' / 'issues' / '10' / 'checks').glob('*.json')]
        self.assertEqual(len(texts), 3)
        for text in texts:
            for secret in ('hunter22xx', 'abcdef123456', 's3cretpw'):
                self.assertNotIn(secret, text)
            self.assertIn('--password=', text)
        self.assertIn('[oculto]', self.record('branch')['argv'][4])

    def test_base_runs_before_the_branch(self):
        make_suite(self.base, {'alpha': False})
        make_suite(self.root, {'alpha': False})
        self.regression()
        self.assertLessEqual(self.record('base')['timestamp'], self.record('branch')['timestamp'])

    def test_result_cites_both_heads_and_the_record_paths(self):
        make_suite(self.base, {'alpha': False}, 'one')
        make_suite(self.root, {'alpha': False}, 'two')
        _, result = self.regression()
        self.assertEqual(result['base']['head'], self.head(self.base))
        self.assertEqual(result['branch']['head'], self.head(self.root))
        self.assertTrue(result['branch']['record'].endswith('regression-branch.json'))
        self.assertTrue(Path(result['base']['record']).is_file())


class ConfigValidationTest(ChecksTestCase):
    def assertRefused(self, regression_block, fragment):
        make_suite(self.base, {'alpha': False})
        make_suite(self.root, {'alpha': False})
        self.write_config({'regression': regression_block})
        code, result = self.regression()
        self.assertEqual(code, 2)
        self.assertFalse(result['ok'])
        self.assertIn(fragment, result['error'])
        self.assertFalse((self.root / '.frontlights').exists(), 'nothing may run or be recorded')

    def test_string_argv_is_refused(self):
        self.assertRefused({'argv': 'python -m unittest'}, 'lista')

    def test_embedded_shell_is_refused(self):
        for argv in (['cmd', '/c', 'echo'], ['sh', '-c', 'x'], ['bash', '-c', 'x'],
                     ['powershell', '-Command', 'x'], ['/usr/bin/pwsh', '-Command', 'x'], [r'C:\Windows\System32\cmd.exe', '/c', 'x'],
                     ['python', '-m', 'unittest', '&&', 'echo'], ['python', '|', 'tee'],
                     ['python', ';', 'ls'], ['python', '>', 'out.txt'], ['python', '2>&1']):
            with self.subTest(argv=argv):
                self.assertRefused({'argv': argv}, 'shell')

    def test_hardened_shell_variants_are_refused(self):
        for argv in (['env', 'sh', '-c', 'x'], ['xargs', 'sh'], ['nohup', 'bash'], ['busybox', 'sh'], ['wsl', 'x'],
                     ['sudo', 'sh'], ['sudo', 'bash', '-c', 'x'],
                     ['python', '-c', 'import os'], ['python3', '-c', 'x'], ['py', '-c', 'x'],
                     ['run.bat'], ['cmd.bat'], ['cmd.com'], ['tools/x.cmd', 'a'],
                     ['"sh"', 'x'], ["'bash'", 'x'], ['powershell.exe '], ['pwsh\t'], ['cmd '], [' sh', 'x']):
            with self.subTest(argv=argv):
                self.assertRefused({'argv': argv}, 'shell')

    def test_argv_with_a_nul_byte_is_refused_not_a_traceback(self):
        self.assertRefused({'argv': ['python', 'a\0b']}, 'NUL')

    def test_legitimate_argv_is_accepted(self):
        for argv in (['python', '-m', 'unittest'], ['python', '>=3'], ['python', '-m', 'pytest', '-q'],
                     ['npm', 'test']):
            with self.subTest(argv=argv):
                self.write_config({'regression': {'argv': argv}})
                self.assertEqual(checks.regression_settings(self.config)['argv'], argv)

    def test_non_positive_issue_is_refused(self):
        make_suite(self.base, {'alpha': False})
        make_suite(self.root, {'alpha': False})
        for issue in (0, -3):
            with self.subTest(issue=issue):
                code, result = self.regression(issue=issue)
                self.assertEqual(code, 2)
                self.assertIn('issue', result['error'])
        self.assertFalse((self.root / '.frontlights').exists())

    def test_nested_base_and_root_are_refused(self):
        make_suite(self.root, {'alpha': False})
        inner = self.root / 'inner'
        inner.mkdir()
        make_suite(inner, {'alpha': False})
        for base, root in ((inner, self.root), (self.root, inner)):
            with self.subTest(base=base.name):
                self.base, self.root = base, root
                code, result = self.regression()
                self.assertEqual(code, 2)
                self.assertIn('base', result['error'])

    def test_base_without_git_does_not_print_the_full_local_path(self):
        make_suite(self.root, {'alpha': False})
        code, result = self.regression()
        self.assertEqual(code, 2)
        self.assertNotIn(str(self.base), result['error'])
        self.assertNotIn(str(self.tmp), result['error'])

    def test_cwd_escaping_the_tree_is_refused(self):
        self.assertRefused({'argv': SUITE_ARGV, 'cwd': '..'}, 'cwd')

    def test_base_and_root_must_differ(self):
        make_suite(self.root, {'alpha': False})
        self.base = self.root
        code, result = self.regression()
        self.assertEqual(code, 2)
        self.assertIn('base', result['error'])


class InfrastructureTest(ChecksTestCase):
    def use_argv(self, argv, timeout=60):
        self.write_config({'regression': {'argv': argv, 'timeoutSeconds': timeout}})

    def two_trees(self):
        make_suite(self.base, {'alpha': False})
        make_suite(self.root, {'alpha': False})

    def record(self, which):
        path = self.root / '.frontlights' / 'issues' / '10' / 'checks' / f'regression-{which}.json'
        return json.loads(path.read_text(encoding='utf-8'))

    def assertInfrastructure(self, code, result, kind):
        self.assertEqual(code, 3)
        self.assertTrue(result['blocking'])
        self.assertFalse(result['ok'])
        self.assertEqual(result['new_failures'], [])
        self.assertTrue(any(kind in item['kind'] for item in result['infrastructure']), result)

    def test_missing_executable_is_infrastructure_not_a_pass(self):
        self.two_trees()
        self.use_argv(['frontlights-no-such-executable', '--run'])
        code, result = self.regression()
        self.assertInfrastructure(code, result, 'executable_missing')
        self.assertEqual(self.record('branch')['classification'], 'infrastructure')

    def test_timeout_is_infrastructure(self):
        self.two_trees()
        self.use_argv(self.script('import time; time.sleep(30)'), timeout=1)
        code, result = self.regression()
        self.assertInfrastructure(code, result, 'timeout')

    def pid_alive(self, pid):
        if os.name == 'nt':
            out = subprocess.run(['tasklist', '/FI', f'PID eq {pid}', '/NH'], capture_output=True, text=True).stdout
            return str(pid) in out.split()
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def kill_pid(self, pid):
        if os.name == 'nt':
            subprocess.run(['taskkill', '/PID', str(pid), '/F'], capture_output=True)
        else:
            with contextlib.suppress(OSError):
                os.kill(pid, 9)

    def test_timeout_is_enforced_even_with_a_live_grandchild_and_leaves_no_orphan(self):
        self.two_trees()
        pidfile = self.tmp / 'grandchild.pids'
        script = ('import subprocess, sys, time\n'
                  'p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])\n'
                  'open(sys.argv[1], "a").write(str(p.pid) + chr(10))\n'
                  'time.sleep(60)\n')
        self.use_argv(self.script(script, str(pidfile)), timeout=2)
        started = time.monotonic()
        code, result = self.regression()
        elapsed = time.monotonic() - started
        pids = [int(x) for x in pidfile.read_text(encoding='utf-8').split()]
        for pid in pids:
            self.addCleanup(self.kill_pid, pid)
        self.assertInfrastructure(code, result, 'timeout')
        self.assertLess(elapsed, 14, 'o prazo precisa valer mesmo com neto vivo (dois lados de 2 s)')
        time.sleep(0.5)
        self.assertEqual([p for p in pids if self.pid_alive(p)], [])

    def hostile_script(self):
        """A inicia B, B inicia C (dorme 40 s) e sai; A segue vivo. Grava o pid de C no arquivo recebido."""
        middle = ('import subprocess, sys\n'
                  'c = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(40)"])\n'
                  'open(sys.argv[1], "a").write(str(c.pid) + chr(10))\n')
        middle_path = self.tmp / 'middle.py'
        middle_path.write_text(middle, encoding='utf-8')
        top = ('import subprocess, sys, time\n'
               f'subprocess.Popen([sys.executable, {str(middle_path)!r}, sys.argv[1]]).wait()\n'
               'time.sleep(60)\n')
        return top

    @unittest.skipUnless(os.name == 'nt', 'Job Object é específico do Windows')
    def test_timeout_kills_a_grandchild_whose_parent_already_died(self):
        self.two_trees()
        pidfile = self.tmp / 'orphan.pids'
        self.use_argv(self.script(self.hostile_script(), str(pidfile)), timeout=3)
        started = time.monotonic()
        code, result = self.regression()
        elapsed = time.monotonic() - started
        pids = [int(x) for x in pidfile.read_text(encoding='utf-8').split()]
        for pid in pids:
            self.addCleanup(self.kill_pid, pid)
        self.assertEqual(len(pids), 2, 'um neto por lado')
        self.assertInfrastructure(code, result, 'timeout')
        self.assertLess(elapsed, 20)
        time.sleep(0.5)
        self.assertEqual([p for p in pids if self.pid_alive(p)], [])

    @unittest.skipUnless(os.name == 'nt', 'Job Object é específico do Windows')
    def test_job_object_failure_falls_back_with_a_warning_in_portuguese(self):
        self.two_trees()
        self.use_argv(self.script('import time; time.sleep(30)'), timeout=1)
        with mock.patch.object(checks, 'create_job', side_effect=OSError('sem job')):
            code, result = self.regression()
        self.assertInfrastructure(code, result, 'timeout')
        self.assertTrue(result['warnings'])
        self.assertIn('Job Object', result['warnings'][0])
        self.assertNotIn('sem job', json.dumps(result))

    def test_raw_output_file_is_closed_even_on_timeout(self):
        self.two_trees()
        self.use_argv(self.script('import time; time.sleep(30)'), timeout=1)
        opened = []
        real = tempfile.TemporaryFile

        def spy(*args, **kwargs):
            handle = real(*args, **kwargs)
            opened.append(handle)
            return handle

        with mock.patch.object(checks.tempfile, 'TemporaryFile', spy):
            self.regression()
        self.assertEqual(len(opened), 2)
        self.assertTrue(all(h.closed for h in opened))

    def test_suite_reporting_zero_tests_is_infrastructure(self):
        self.two_trees()
        self.use_argv(self.script('import sys; sys.stderr.write("Ran 0 tests in 0.000s" + chr(10))'))
        code, result = self.regression()
        self.assertInfrastructure(code, result, 'no_result')

    def test_different_test_counts_between_base_and_branch_are_recorded(self):
        make_suite(self.base, {'alpha': False})
        make_suite(self.root, {'alpha': False, 'beta': False, 'gamma': False})
        code, result = self.regression()
        self.assertEqual(code, 0)
        self.assertEqual(self.record('base')['tests_run'], 1)
        self.assertEqual(self.record('branch')['tests_run'], 3)

    def test_cwd_symlink_escaping_the_tree_is_refused(self):
        self.two_trees()
        outside = self.tmp / 'outside'
        outside.mkdir()
        try:
            os.symlink(outside, self.root / 'link', target_is_directory=True)
            os.symlink(outside, self.base / 'link', target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('a plataforma não permite criar link simbólico')
        self.write_config({'regression': {'argv': SUITE_ARGV, 'cwd': 'link'}})
        code, result = self.regression()
        self.assertEqual(code, 2)
        self.assertIn('cwd', result['error'])

    def test_non_secret_environment_names_do_not_deform_test_names(self):
        make_suite(self.base, {'alpha': False})
        make_suite(self.root, {'alpha': True})
        os.environ['FRONTLIGHTS_MODE'] = 'alpha'
        self.addCleanup(os.environ.pop, 'FRONTLIGHTS_MODE', None)
        code, result = self.regression()
        self.assertEqual(code, 1)
        self.assertIn('test_alpha', result['new_failures'][0])

    def test_nonzero_exit_without_any_result_is_infrastructure(self):
        self.two_trees()
        self.use_argv(self.script('import sys; sys.exit(3)'))
        code, result = self.regression()
        self.assertInfrastructure(code, result, 'no_result')

    def test_clean_exit_without_any_test_run_is_not_a_pass(self):
        self.two_trees()
        self.use_argv(self.script('pass'))
        code, result = self.regression()
        self.assertInfrastructure(code, result, 'no_result')

    def test_base_infrastructure_failure_blocks_even_if_branch_is_green(self):
        make_suite(self.root, {'alpha': False})
        make_suite(self.base, {'alpha': False})
        # the base run is made to die before producing any result
        script = ('import os, sys\n'
                  'if os.path.basename(os.getcwd()) == "base": sys.exit(5)\n'
                  'sys.stderr.write("Ran 1 test in 0s" + chr(10))\n')
        self.use_argv(self.script(script))
        code, result = self.regression()
        self.assertEqual(code, 3)
        self.assertTrue(result['blocking'])
        self.assertEqual([i['side'] for i in result['infrastructure']], ['base'])

    def test_unnamed_failure_in_the_branch_is_not_comparable_and_blocks(self):
        self.two_trees()
        script = ('import sys; sys.stderr.write("Ran 4 tests in 0.1s" + chr(10) + "FAILED (failures=1)" + chr(10)); '
                  'sys.exit(1)')
        self.use_argv(self.script(script))
        code, result = self.regression()
        self.assertEqual(code, 1)
        self.assertTrue(result['blocking'])
        self.assertEqual(len(result['new_failures']), 1)
        self.assertEqual(result['existing_failures'], [])
        self.assertEqual(self.record('branch')['classification'], 'product_failure')

    def test_unnamed_failures_with_a_bigger_count_in_the_branch_block(self):
        self.two_trees()
        script = ('import os, sys\n'
                  'n = 1 if os.path.basename(os.getcwd()) == "base" else 3\n'
                  'sys.stderr.write("Ran 4 tests in 0.1s" + chr(10) + f"FAILED (failures={n})" + chr(10))\n'
                  'sys.exit(1)\n')
        self.use_argv(self.script(script))
        code, result = self.regression()
        self.assertEqual(code, 1)
        self.assertTrue(result['blocking'])
        self.assertEqual(self.record('base')['failure_count'], 1)
        self.assertEqual(self.record('branch')['failure_count'], 3)

    def test_pytest_style_failures_are_extracted(self):
        self.two_trees()
        script = ('import sys, os\n'
                  'if os.path.basename(os.getcwd()) == "branch":\n'
                  '    print("FAILED tests/test_a.py::test_x - assert 1 == 2")\n'
                  '    print("ERROR tests/test_b.py::test_y")\n'
                  '    print("1 failed, 1 error, 3 passed in 0.12s")\n'
                  '    sys.exit(1)\n'
                  'print("5 passed in 0.1s")\n')
        self.use_argv(self.script(script))
        code, result = self.regression()
        self.assertEqual(code, 1)
        self.assertEqual(result['new_failures'], ['tests/test_a.py::test_x', 'tests/test_b.py::test_y'])
        self.assertEqual(self.record('branch')['tests_run'], 5)

    def test_relative_cwd_is_applied_inside_each_tree(self):
        self.two_trees()
        for tree in (self.base, self.root):
            (tree / 'sub').mkdir()
            (tree / 'sub' / 'marker.txt').write_text('x', encoding='utf-8')
        script = ('import os, sys\n'
                  'sys.stderr.write("Ran 1 test in 0s" + chr(10))\n'
                  'sys.exit(0 if os.path.exists("marker.txt") else 1)\n')
        self.write_config({'regression': {'argv': self.script(script), 'cwd': 'sub'}})
        code, result = self.regression()
        self.assertEqual(code, 0, result)

    def test_secrets_from_the_environment_never_reach_the_output_or_records(self):
        self.two_trees()
        secret = 'fixture-secret-value-123'
        script = ('import sys, os\n'
                  'sys.stderr.write("FAIL: leak_" + os.environ["FRONTLIGHTS_FAKE_TOKEN"] + " (m.C.t)" + chr(10))\n'
                  'sys.stderr.write("Ran 1 test in 0s" + chr(10))\n'
                  'sys.exit(1)\n')
        self.use_argv(self.script(script))
        os.environ['FRONTLIGHTS_FAKE_TOKEN'] = secret
        self.addCleanup(os.environ.pop, 'FRONTLIGHTS_FAKE_TOKEN', None)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            checks.main(['regression', '--config', str(self.config), '--root', str(self.root),
                         '--base', str(self.base), '--issue', '10'])
        self.assertIn('leak_', out.getvalue())
        self.assertNotIn(secret, out.getvalue())
        records = list((self.root / '.frontlights' / 'issues' / '10' / 'checks').glob('*.json'))
        self.assertEqual(len(records), 2)
        for record in records:
            self.assertNotIn(secret, record.read_text(encoding='utf-8'))


class CommandLineTest(ChecksTestCase):
    def test_script_runs_as_a_program_and_prints_json(self):
        make_suite(self.base, {'alpha': False})
        make_suite(self.root, {'alpha': False, 'fresh': True})
        done = subprocess.run([sys.executable, checks.__file__, 'regression', '--config', str(self.config),
                               '--root', str(self.root), '--base', str(self.base), '--issue', '10'],
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 1)
        self.assertEqual(len(json.loads(done.stdout)['new_failures']), 1)


if __name__ == '__main__':
    unittest.main()
