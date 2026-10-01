import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import checks

SUITE_ARGV = [sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-v']
GIT_ENV = {**os.environ, 'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.invalid',
           'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@example.invalid'}


def git(path, *args):
    subprocess.run(['git', '-C', str(path), *args], check=True, capture_output=True, env=GIT_ENV)


def make_suite(path, tests, message='suite'):
    """Tiny fake suite: `tests` maps a test name to True when it must fail."""
    (path / 'tests').mkdir(parents=True, exist_ok=True)
    body = ['import unittest', 'class FakeCase(unittest.TestCase):']
    for name, fails in sorted(tests.items()):
        body += [f'    def test_{name}(self):', f'        self.assertEqual({1 if fails else 0}, 0)']
    (path / 'tests' / 'test_fake.py').write_text('\n'.join(body) + '\n', encoding='utf-8')
    if not (path / '.git').exists():
        git(path, 'init', '-q')
    git(path, 'add', '-A')
    git(path, 'commit', '-q', '--allow-empty', '-m', message)


class ChecksTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name) / 'base'
        self.root = Path(tmp.name) / 'branch'
        self.base.mkdir()
        self.root.mkdir()
        self.config = Path(tmp.name) / 'config.json'
        self.write_config({'regression': {'argv': SUITE_ARGV, 'timeoutSeconds': 60}})

    def write_config(self, checks_block):
        self.config.write_text(json.dumps({'checks': checks_block}), encoding='utf-8')

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
        self.use_argv([sys.executable, '-c', 'import time; time.sleep(30)'], timeout=1)
        code, result = self.regression()
        self.assertInfrastructure(code, result, 'timeout')

    def test_nonzero_exit_without_any_result_is_infrastructure(self):
        self.two_trees()
        self.use_argv([sys.executable, '-c', 'import sys; sys.exit(3)'])
        code, result = self.regression()
        self.assertInfrastructure(code, result, 'no_result')

    def test_clean_exit_without_any_test_run_is_not_a_pass(self):
        self.two_trees()
        self.use_argv([sys.executable, '-c', 'pass'])
        code, result = self.regression()
        self.assertInfrastructure(code, result, 'no_result')

    def test_base_infrastructure_failure_blocks_even_if_branch_is_green(self):
        make_suite(self.root, {'alpha': False})
        make_suite(self.base, {'alpha': False})
        script = ('import os, sys; sys.stderr.write("Ran 1 test in 0s" + chr(10)); '
                  'sys.exit(5 if os.path.basename(os.getcwd()) == "base" and False else 0)')
        # the base run is made to die before producing any result
        script = ('import os, sys\n'
                  'if os.path.basename(os.getcwd()) == "base": sys.exit(5)\n'
                  'sys.stderr.write("Ran 1 test in 0s" + chr(10))\n')
        self.use_argv([sys.executable, '-c', script])
        code, result = self.regression()
        self.assertEqual(code, 3)
        self.assertTrue(result['blocking'])
        self.assertEqual([i['side'] for i in result['infrastructure']], ['base'])

    def test_nonzero_exit_with_unnamed_failure_still_counts_as_product_failure(self):
        self.two_trees()
        script = ('import sys; sys.stderr.write("Ran 4 tests in 0.1s" + chr(10) + "FAILED (failures=1)" + chr(10)); '
                  'sys.exit(1)')
        self.use_argv([sys.executable, '-c', script])
        code, result = self.regression()
        self.assertEqual(code, 0)
        self.assertEqual(len(result['existing_failures']), 1)
        self.assertEqual(self.record('branch')['classification'], 'product_failure')

    def test_pytest_style_failures_are_extracted(self):
        self.two_trees()
        script = ('import sys, os\n'
                  'if os.path.basename(os.getcwd()) == "branch":\n'
                  '    print("FAILED tests/test_a.py::test_x - assert 1 == 2")\n'
                  '    print("ERROR tests/test_b.py::test_y")\n'
                  '    print("1 failed, 1 error, 3 passed in 0.12s")\n'
                  '    sys.exit(1)\n'
                  'print("5 passed in 0.1s")\n')
        self.use_argv([sys.executable, '-c', script])
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
        self.write_config({'regression': {'argv': [sys.executable, '-c', script], 'cwd': 'sub'}})
        code, result = self.regression()
        self.assertEqual(code, 0, result)

    def test_secrets_from_the_environment_never_reach_the_output_or_records(self):
        self.two_trees()
        secret = 'fixture-secret-value-123'
        script = ('import sys, os\n'
                  'sys.stderr.write("FAIL: leak_" + os.environ["FRONTLIGHTS_FAKE_TOKEN"] + " (m.C.t)" + chr(10))\n'
                  'sys.stderr.write("Ran 1 test in 0s" + chr(10))\n'
                  'sys.exit(1)\n')
        self.use_argv([sys.executable, '-c', script])
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
