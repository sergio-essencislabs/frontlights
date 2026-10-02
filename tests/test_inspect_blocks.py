"""`inspect` valida os blocos browserTest e checks com as mesmas regras do serve e do checks.

Sem os blocos, a saída do `inspect` não muda. Só marcadores genéricos (repositório público).
"""
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import frontlights

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'frontlights.py'
EXAMPLE = json.loads((ROOT / 'examples' / 'config.json').read_text(encoding='utf-8'))
PASSWORD = 'senha-ficticia-longa'


def local(config):
    """Exemplo sem GitHub, quadro nem RoadS: o inspect não faz leitura de rede."""
    config = copy.deepcopy(config)
    config.update(repository=None, project=None, roads=None)
    return config


def process(**overrides):
    item = {'name': 'web', 'argv': ['python', 'APP_SCRIPT', '{port}'], 'port': 'auto',
            'health': 'http://127.0.0.1:{port}/health'}
    item.update(overrides)
    return item


def with_browser(*processes, users=None, base_url=None):
    block = {'processes': list(processes) or [process()],
             'users': users if users is not None else [{'login': 'usuario@exemplo.test', 'password': PASSWORD}]}
    if base_url is not None:
        block['baseUrl'] = base_url
    return {'repository': None, 'browserTest': block}


def with_checks(**checks):
    return {'repository': None, 'checks': checks}


def stable(result):
    """Tira o horário da leitura, o único campo que muda entre duas execuções."""
    result = dict(result)
    result.pop('fetched_at')
    return result


class InspectWithoutBlocksTests(unittest.TestCase):
    """Sem browserTest nem checks, nada muda: mesma saída e nenhum validador chamado."""

    def test_output_without_blocks_is_unchanged(self):
        with tempfile.TemporaryDirectory() as root:
            result = frontlights.inspect({'repository': None}, root)
            path_risk = frontlights.path_risk(Path(root).absolute(), frontlights.os.path.realpath(root))
        self.assertEqual(stable(result), {
            'repository': None, 'sources': {'github': {'status': 'unconfigured'},
                                            'project': {'status': 'unconfigured'},
                                            'roads': {'status': 'unconfigured'}},
            'verification_commands': {}, 'verification_candidates': [], 'path_risk': path_risk})

    def test_null_blocks_count_as_absent(self):
        with tempfile.TemporaryDirectory() as root:
            plain = frontlights.inspect({'repository': None}, root)
            nulls = frontlights.inspect({'repository': None, 'browserTest': None, 'checks': None}, root)
        self.assertEqual(stable(nulls), stable(plain))

    def test_cli_output_without_blocks_has_the_same_keys(self):
        with tempfile.TemporaryDirectory() as root:
            config = Path(root) / 'config.json'
            config.write_text(json.dumps({'repository': None}), encoding='utf-8')
            run = subprocess.run([sys.executable, str(SCRIPT), 'inspect', '--config', str(config), '--root', root],
                                 capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(sorted(json.loads(run.stdout)), ['fetched_at', 'path_risk', 'repository', 'sources',
                                                          'verification_candidates', 'verification_commands'])


class InspectValidBlocksTests(unittest.TestCase):
    def test_example_config_is_accepted_and_does_not_change_the_output(self):
        with tempfile.TemporaryDirectory() as root:
            plain = {k: v for k, v in local(EXAMPLE).items() if k not in ('browserTest', 'checks')}
            self.assertEqual(stable(frontlights.inspect(local(EXAMPLE), root)),
                             stable(frontlights.inspect(plain, root)))

    def test_example_config_through_the_cli(self):
        with tempfile.TemporaryDirectory() as root:
            config = Path(root) / 'config.json'
            config.write_text(json.dumps(local(EXAMPLE)), encoding='utf-8')
            run = subprocess.run([sys.executable, str(SCRIPT), 'inspect', '--config', str(config), '--root', root],
                                 capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIsNone(json.loads(run.stdout)['repository'])


class InspectRefusesInvalidBlocksTests(unittest.TestCase):
    """Bloco inválido é recusado como as outras recusas de config do inspect (ValueError; erro JSON na CLI)."""

    def refuse(self, config, pattern):
        with tempfile.TemporaryDirectory() as root, patch.object(frontlights, 'run') as network, \
                self.assertRaisesRegex(ValueError, pattern) as caught:
            frontlights.inspect(config, root)
        network.assert_not_called()
        return str(caught.exception)

    def test_auto_port_without_placeholder_in_health(self):
        self.refuse(with_browser(process(health='http://127.0.0.1:8080/health')), r'\{port\}')

    def test_short_test_secret(self):
        message = self.refuse(with_browser(users=[{'login': 'usuario@exemplo.test', 'password': 'abc'}]),
                              'menos de 4 caracteres')
        self.assertNotIn('abc', message)

    def test_secret_with_the_checks_mask_marker(self):
        self.refuse(with_browser(users=[{'login': 'usuario@exemplo.test', 'password': 'x[oculto]x'}]),
                    'marcador de máscara')

    def test_processes_must_be_a_list(self):
        self.refuse({'repository': None, 'browserTest': {'processes': 'web'}}, 'browserTest.processes')

    def test_non_local_health_host(self):
        self.refuse(with_browser(process(port=8080, health='http://10.0.0.5:8080/health')), 'não é local')

    def test_non_local_base_url(self):
        self.refuse(with_browser(base_url='http://app.example.com/'), 'baseUrl')

    def test_embedded_shell_in_checks(self):
        for argv in (['sh', '-c', 'x'], ['cmd', '/c', 'x'], ['python', '-c', 'x'], ['npm', 'test', '&&', 'x']):
            with self.subTest(argv=argv):
                self.refuse(with_checks(regression={'argv': argv}), 'shell embutido')
                self.refuse(with_checks(integration={'argv': argv}), 'shell embutido')

    def test_argv_as_a_string(self):
        self.refuse(with_checks(regression={'argv': 'python -m unittest'}), 'lista de argumentos')

    def test_bad_smoke_paths(self):
        self.refuse(with_checks(smoke={'paths': ['http://10.0.0.5/']}), 'checks.smoke.paths')

    def test_bad_backend_name(self):
        self.refuse(with_checks(backend=7), 'checks.backend')

    def test_checks_must_be_an_object(self):
        self.refuse(with_checks() | {'checks': ['regression']}, 'checks')

    def test_refusal_never_shows_the_test_password(self):
        # o nome do processo repete a senha, e a mensagem de recusa cita o nome
        config = with_browser(process(name=PASSWORD, health='http://127.0.0.1:8080/health'))
        message = self.refuse(config, r'\{port\}')
        self.assertNotIn(PASSWORD, message)

    def test_cli_reports_the_refusal_as_json_without_the_password(self):
        config = with_browser(process(name=PASSWORD, health='http://127.0.0.1:8080/health'))
        config['checks'] = {'regression': {'argv': ['sh', '-c', 'x']}}
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'config.json'
            path.write_text(json.dumps(config), encoding='utf-8')
            run = subprocess.run([sys.executable, str(SCRIPT), 'inspect', '--config', str(path), '--root', root],
                                 capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(run.returncode, 1)
        self.assertEqual(run.stdout, '')
        self.assertNotIn('Traceback', run.stderr)
        self.assertNotIn(PASSWORD, run.stderr)
        self.assertIn('error', json.loads(run.stderr))


if __name__ == '__main__':
    unittest.main()
