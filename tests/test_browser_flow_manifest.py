"""Manifest of the browser-testing route of stage 6 (issue #14).

Reads the text files (reference, issue template, SKILL.md, development.md) and compares every command,
flag, exit code and output field the reference cites with what `scripts/serve.py` and `scripts/checks.py`
actually expose, so the text cannot drift from the helpers.
"""
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import checks  # noqa: E402
import serve  # noqa: E402

SKILL = ROOT / 'skills' / 'frontlights'
REFERENCE = SKILL / 'references' / 'browser-testing.md'
POINTER = 'references/browser-testing.md'
LOCAL_HOSTS = {'127.0.0.1', 'localhost', '[::1]'}
NAMED_HOSTS = {'127.0.0.1', 'localhost', 'exemplo.test'}
FILE_EXTENSIONS = {'md', 'json', 'py'}
CONFIG_KEYS = ('browsertest.', 'checks.')
CREDENTIAL = re.compile(r'\b(?:senha|password|passwd|login|usu[aá]rio|user)\b(?:\s*([:=])\s*|\s+)([^\s,;)`]+)',
                        re.I)
HOST = re.compile(r'(?<![\w.-])(?:[\w-]+(?:\.[\w-]+)+(?::\d+)?|[\w-]+:\d+\b)')


def read(path):
    return path.read_text(encoding='utf-8')


def help_text(*args):
    done = subprocess.run([sys.executable, *args, '--help'], cwd=ROOT, capture_output=True, encoding='utf-8',
                          errors='replace', check=True)
    return done.stdout


class BrowserFlowManifestTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.reference = read(REFERENCE) if REFERENCE.is_file() else ''

    def test_reference_exists(self):
        self.assertTrue(REFERENCE.is_file(), 'falta skills/frontlights/references/browser-testing.md')

    def test_issue_template_declares_front_account_flow_and_enabled_tests(self):
        template = read(ROOT / 'templates' / 'issue.md')
        self.assertIn('## Navegador e testes ligados', template)
        section = template.split('## Navegador e testes ligados')[1].split('\n## ')[0]
        for field in ('Toca o frontend:', 'Conta:', 'Fluxo:', 'Testes ligados:'):
            with self.subTest(field=field):
                self.assertIn(field, section)
        for kind in ('navegador', 'integração', 'regressão', 'permissões entre contas', 'smoke'):
            with self.subTest(kind=kind):
                self.assertIn(kind, section)

    def test_skill_stage_6_and_development_point_to_the_reference(self):
        stage6 = read(SKILL / 'SKILL.md').split('## 6.')[1]
        self.assertIn(POINTER, stage6)
        self.assertIn('browser-testing.md', read(SKILL / 'SKILL.md').split('## Rules')[0])
        self.assertIn('browser-testing.md', read(SKILL / 'references' / 'development.md'))

    def test_pointers_do_not_duplicate_the_reference_text(self):
        sentences = [s.strip() for s in re.split(r'(?<=[.:])\s+', ' '.join(self.reference.split()))
                     if len(s.strip()) > 60]
        self.assertTrue(sentences)
        for name, text in (('SKILL.md', read(SKILL / 'SKILL.md')),
                           ('development.md', read(SKILL / 'references' / 'development.md'))):
            flat = ' '.join(text.split())
            for sentence in sentences:
                with self.subTest(file=name, sentence=sentence[:50]):
                    self.assertNotIn(sentence, flat)
            with self.subTest(file=name):
                self.assertNotIn('serve.py', text)
                self.assertNotIn('checks.py', text)

    def test_every_failure_is_a_question_and_infrastructure_is_separate_from_product(self):
        failures = self.reference.split('## Every failure is a question')[1].split('\n## ')[0]
        self.assertIn('AskUserQuestion', failures)
        self.assertIn('Never decide alone', failures)
        # each kind of the classification cites the checks code and serve category that really mean it
        expected = {'infrastructure failure': ('infrastructure', serve.INFRASTRUCTURE),
                    'product failure': ('product_failure', None),
                    'config refusal': ('refused', serve.USAGE)}
        bullets = {}
        for bullet in re.split(r'\n- ', failures)[1:]:
            kind = re.match(r'\*\*([a-z ]+)\*\*:', bullet)
            if kind:
                bullets[kind.group(1)] = ' '.join(bullet.split())
        self.assertEqual(set(bullets), set(expected))
        for kind, (key, category) in expected.items():
            with self.subTest(kind=kind):
                self.assertEqual(re.findall(r'`checks` code `(\d)`', bullets[kind]), [str(checks.EXIT_CODES[key])])
                self.assertEqual(re.findall(r'`serve` category `(\w+)`', bullets[kind]),
                                 [category] if category else [])

    def test_cross_account_check_uses_both_accounts_and_says_when_account_2_enters(self):
        section = self.reference.split('## Cross-account permissions')[1].split('\n## ')[0]
        for text in ('account 1', 'account 2', 'browserTest.users', 'product failure', 'Conta:',
                     'permissões entre contas'):
            with self.subTest(text=text):
                self.assertIn(text, section)

    def test_evidence_lives_in_the_issue_folder_with_head_and_diff_hash(self):
        section = self.reference.split('## Evidence')[1].split('\n## ')[0]
        for text in ('.frontlights/issues/<n>/browser/', '.frontlights/issues/<n>/checks/', '`head`',
                     '`diff_sha256`', 'frontlights.py" evidence --root', 'handoff', 'text only',
                     'authoriz', 'screenshot'):
            with self.subTest(text=text):
                self.assertIn(text, section)

    def test_no_browser_or_network_is_reported_and_never_counts_as_passed(self):
        self.assertIn('never counts as passed', self.reference)

    def test_reference_and_template_hold_no_login_password_or_real_url(self):
        for name, text in (('reference', self.reference), ('issue.md', read(ROOT / 'templates' / 'issue.md'))):
            for match in re.finditer(r'\bhttps?://([^/\s`"\')]+)', text):
                host = match.group(1).rsplit(':', 1)[0] if not match.group(1).startswith('[') \
                    else match.group(1).split(']')[0] + ']'
                with self.subTest(file=name, url=match.group(0)):
                    self.assertIn(host, LOCAL_HOSTS)
            self.assertIsNone(re.search(r'[\w.+-]+@[\w-]+\.[\w.]+', text), f'e-mail ou login em {name}')
            self.assertIsNone(re.search(r'\bwww\.|\.com\b|\.com\.br\b', text), f'domínio real em {name}')
            # a value after `senha:`/`login=` (or after a space, when it looks like a secret) must be a placeholder
            for match in CREDENTIAL.finditer(text):
                value = match.group(2)
                placeholder = re.fullmatch(r'<[^>]*>?|\{[^}]*\}?', value)
                secret_like = match.group(1) or re.search(r'[\d@#$%!*&^+=]', value)
                with self.subTest(file=name, credential=match.group(0)):
                    self.assertFalse(secret_like and not placeholder, f'login ou senha em {name}')
            # a dotted name or host:port is a local host, a file name or a config key, never a real host
            for match in HOST.finditer(text):
                token = match.group(0).lower()
                host, _, port = token.partition(':')
                allowed = host in NAMED_HOSTS or not port and (token.rsplit('.', 1)[1] in FILE_EXTENSIONS
                                                               or token.startswith(CONFIG_KEYS))
                with self.subTest(file=name, host=token):
                    self.assertTrue(allowed, f'host real em {name}')

    def test_cited_subcommands_and_flags_exist_in_the_scripts(self):
        serve_help = help_text('scripts/serve.py')
        serve_ops = set(re.search(r'\{([a-z,]+)\}', serve_help).group(1).split(','))
        check_subs = set(checks.SUBCOMMANDS)
        def cited(script):  # `serve stop` in prose, or the argv form scripts/serve.py" stop
            pattern = rf'`{script} (\w+)|{script}\.py" (\w+)'
            return {a or b for a, b in re.findall(pattern, self.reference)}
        cited_serve, cited_checks = cited('serve'), cited('checks')
        self.assertTrue(cited_serve and cited_checks)
        self.assertLessEqual(cited_serve, serve_ops, 'subcomando do serve inexistente na reference')
        self.assertLessEqual(cited_checks, check_subs, 'subcomando do checks inexistente na reference')
        self.assertEqual(cited_serve, serve_ops, 'a reference precisa citar start, status e stop')
        self.assertEqual(cited_checks, check_subs, 'a reference precisa citar todos os subcomandos do checks')
        known = set(re.findall(r'--[a-z][a-z-]*', serve_help))
        for sub in check_subs:
            known |= set(re.findall(r'--[a-z][a-z-]*', help_text('scripts/checks.py', sub)))
        known |= set(re.findall(r'--[a-z][a-z-]*', help_text('scripts/frontlights.py', 'evidence')))
        for flag in set(re.findall(r'(?<![\w-])--[a-z][a-z-]*', self.reference)):
            with self.subTest(flag=flag):
                self.assertIn(flag, known)

    def test_cited_checks_exit_codes_match_the_script(self):
        keys = {'passed': 'passed', 'product failure': 'product_failure',
                'config or usage refused': 'refused', 'infrastructure': 'infrastructure'}
        section = ' '.join(self.reference.split('## Checks: `checks`')[1].split('\n## ')[0].split())
        sentence = section.split('Exit codes:')[1].split('.')[0]
        cited = re.findall(r'`(\d)` ([a-z ]+?)(?= \(|,|$)', sentence)
        self.assertEqual(sorted(label for _, label in cited), sorted(keys))
        for code, label in cited:
            with self.subTest(label=label):
                self.assertEqual(int(code), checks.EXIT_CODES[keys[label]])

    def test_cited_serve_exit_codes_and_categories_match_the_script(self):
        section = self.reference.split('## Environment: `serve`')[1].split('\n## ')[0]
        exits = ' '.join(section.split()).split('Exit ')[1].split(';')[0]
        self.assertEqual(set(re.findall(r'`(\d)`', exits)), {'0', '1'})
        meaning = dict(re.findall(r'^- `(\w+)`: (.+)$', section, re.M))
        self.assertEqual(set(meaning), {serve.USAGE, serve.INFRASTRUCTURE})
        self.assertIn('invalid config', meaning[serve.USAGE])
        self.assertIn('missing config', meaning[serve.INFRASTRUCTURE])
        # the script itself: an invalid config is `uso`, a missing one `infraestrutura`, both exit 1
        with tempfile.TemporaryDirectory() as folder:
            invalid = Path(folder) / 'invalid.json'
            invalid.write_text(json.dumps({'browserTest': {'processes': [
                {'name': 'web', 'argv': ['web'], 'health': 'not-a-url'}]}}), encoding='utf-8')
            for config, category in ((invalid, serve.USAGE), (Path(folder) / 'missing.json', serve.INFRASTRUCTURE)):
                done = subprocess.run([sys.executable, 'scripts/serve.py', 'start', '--config', str(config),
                                       '--root', folder, '--issue', '1'], cwd=ROOT, capture_output=True,
                                      encoding='utf-8', errors='replace')
                with self.subTest(config=config.name):
                    self.assertEqual(done.returncode, 1)
                    self.assertEqual(json.loads(done.stdout)['category'], category)

    def test_cited_output_fields_exist_in_the_scripts(self):
        sources = read(ROOT / 'scripts' / 'serve.py') + read(ROOT / 'scripts' / 'checks.py') \
            + read(ROOT / 'scripts' / 'frontlights.py')
        fields = set(re.findall(r'`([a-z]+(?:_[a-z0-9]+)+)`', self.reference))
        self.assertIn('reservas_compartilhadas', fields)
        for field in fields:
            with self.subTest(field=field):
                self.assertIn(field, sources)


if __name__ == '__main__':
    unittest.main()
