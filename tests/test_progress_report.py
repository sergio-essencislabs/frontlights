import json
import os
import socket
import sys
import tempfile
import textwrap
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import progress_report as pr
import roadmap_sync as rs

ROOT = Path(__file__).resolve().parents[1]
SECRET = 'sentinel-secret-value-9f3a'
LEAK = 'LEAKED-BODY-TEXT'

COLLECTOR = textwrap.dedent('''
    import json, sys
    log, name = sys.argv[1], sys.argv[2]
    with open(log, 'a', encoding='utf-8') as handle:
        handle.write(json.dumps({'name': name, 'argv': sys.argv[3:], 'hasSecret': bool(__import__('os').environ.get('FRONTLIGHTS_API_SECRET'))}) + '\\n')
    print('conferencia ' + name)
''')
FAILING = 'import sys\nprint("falhou por causa de X")\nsys.exit(3)\n'
SLEEPING = 'import time\nprint("comecei", flush=True)\ntime.sleep(30)\n'
BIG = 'print("a" * 100000 + "FIM")\n'
ECHO_SECRET = 'import os\nprint("eco " + os.environ["FRONTLIGHTS_API_SECRET"])\n'
PUSH = textwrap.dedent('''
    import json, os, sys
    with open(sys.argv[1], 'w', encoding='utf-8') as handle:
        handle.write(json.dumps({'argv': sys.argv[2:], 'secretInEnv': os.environ.get('FRONTLIGHTS_API_SECRET')}))
    print('Revisar em https://roads.example.test/review/1')
''')


class FakeRoads:
    """A real local HTTP server standing in for the RoadS route."""

    def __init__(self):
        self.requests = []
        self.answer = (200, {})
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                owner.requests.append({'path': self.path, 'authorization': self.headers.get('Authorization')})
                status, body = owner.answer
                data = body if isinstance(body, str) else json.dumps(body)
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(data.encode('utf-8'))

            def log_message(self, *args):
                pass

        self.server = HTTPServer(('127.0.0.1', 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def state(start='2026-09-28T00:00:00-03:00', end='2026-10-01T00:00:00-03:00', draft=None, last_sent=None):
    return {'window': {'start': start, 'end': end}, 'draft': draft, 'lastSent': last_sent}


class ProgressTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.project = base / 'project'
        self.tools = base / 'tools'
        self.out = base / 'out'
        for folder in (self.project / '.frontlights', self.tools, self.out):
            folder.mkdir(parents=True)
        for name, text in (('collector.py', COLLECTOR), ('failing.py', FAILING), ('sleeping.py', SLEEPING),
                           ('big.py', BIG), ('echo_secret.py', ECHO_SECRET), ('push.py', PUSH)):
            (self.tools / name).write_text(text, encoding='utf-8')
        self.log = self.out / 'log.jsonl'
        self.roads = FakeRoads()
        self.addCleanup(self.roads.close)
        self.endpoint = f'http://127.0.0.1:{self.roads.port}/api/frontlights'
        self.write_config()
        env = patch.dict(os.environ, {'FRONTLIGHTS_API_SECRET': SECRET})
        env.start()
        self.addCleanup(env.stop)
        for module in (pr.rs, rs):
            patcher = patch.object(module, 'windows_user_env', return_value=None)
            patcher.start()
            self.addCleanup(patcher.stop)
        key = patch.object(pr, 'user_key_path', return_value=base / 'home' / 'approval.key')
        key.start()
        self.addCleanup(key.stop)
        rs._SECRETS.clear()

    def collector(self, name='usage', script='collector.py', **extra):
        return {'name': name, 'command': [sys.executable, str(self.tools / script), str(self.log), name,
                                          '--from', '{from}', '--to', '{to}'], **extra}

    def block(self, **overrides):
        block = {'enabled': True, 'path': 'progress-report', 'collectors': [self.collector()],
                 'pushCommand': [sys.executable, str(self.tools / 'push.py'), str(self.out / 'push.json'),
                                 '--draft', '{draft}']}
        block.update(overrides)
        return block

    def write_config(self, progress='default', **sync_overrides):
        sync = {'enabled': True, 'endpoint': self.endpoint, 'secretEnvVar': 'FRONTLIGHTS_API_SECRET',
                'scrumRoot': str(self.out), 'roadmapFile': '{yyyy}/ROADMAP.md',
                'weekFolderPattern': '{yyyy}/{dd_MM}', 'sprintFilePattern': 'SPRINT_{dd_MM}_a_{dd_MM}.md',
                'maxSprintItems': 4, 'issueTargets': {}}
        if progress == 'default':
            progress = self.block()
        if progress is not None:
            sync['progress'] = progress
        sync.update(sync_overrides)
        config = {'repository': 'OWNER/REPOSITORY', 'project': None, 'roads': None, 'roadmapSync': sync}
        (self.project / '.frontlights' / 'config.json').write_text(json.dumps(config), encoding='utf-8')

    def edit_block(self, **changes):
        config_file = self.project / '.frontlights' / 'config.json'
        config = json.loads(config_file.read_text(encoding='utf-8'))
        config['roadmapSync']['progress'].update(changes)
        config_file.write_text(json.dumps(config), encoding='utf-8')

    def run_op(self, operation, **kwargs):
        return pr.run(operation, str(self.project), **kwargs)

    def approved(self):
        self.assertTrue(self.run_op('approve')['ok'])

    def window(self, body=None, status=200):
        self.roads.answer = (status, state() if body is None else body)
        return self.run_op('window')

    def logged(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding='utf-8').splitlines()]

    def assert_never_leaks(self, result):
        text = json.dumps(result)
        self.assertNotIn(SECRET, text)
        self.assertNotIn(LEAK, text)


class StatusTests(ProgressTestCase):
    def test_not_configured_when_there_is_no_config_or_no_block(self):
        (self.project / '.frontlights' / 'config.json').unlink()
        result = self.run_op('status')
        self.assertTrue(result['ok'])
        self.assertEqual((result['configured'], result['enabled'], result['ready'], result['ask']),
                         (False, False, False, False))
        self.write_config(progress=None)
        result = self.run_op('status')
        self.assertEqual((result['configured'], result['ask']), (False, False))

    def test_disabled_block_does_not_ask(self):
        self.write_config(progress=self.block(enabled=False))
        result = self.run_op('status')
        self.assertEqual((result['configured'], result['enabled'], result['ready'], result['ask']),
                         (True, False, False, False))

    def test_secret_absent_is_reported_without_the_value(self):
        with patch.dict(os.environ):
            os.environ.pop('FRONTLIGHTS_API_SECRET')
            result = self.run_op('status')
        self.assertEqual((result['secret'], result['ready'], result['ask']), ('absent', False, True))
        self.assert_never_leaks(result)

    def test_unapproved_then_approved_then_changed_after_editing_a_command(self):
        result = self.run_op('status')
        self.assertEqual((result['approval'], result['ready']), ('unapproved', False))
        self.approved()
        result = self.run_op('status')
        self.assertEqual((result['approval'], result['ready']), ('approved', True))
        self.assertEqual(result['collectors'], ['usage'])
        self.assertEqual(result['secret'], 'present')
        self.assert_never_leaks(result)
        collector = self.collector()
        collector['command'].append('--extra')
        self.edit_block(collectors=[collector])
        result = self.run_op('status')
        self.assertEqual((result['approval'], result['ready']), ('changed', False))

    def test_changing_the_path_the_push_command_or_the_endpoint_needs_a_new_approval(self):
        self.approved()
        for changes in ({'path': 'other-route'}, {'pushCommand': ['node', 'other.js', '{draft}']},
                        {'collectors': [self.collector(name='renamed')]}, {'factsFile': 'facts.json'}):
            with self.subTest(changes=changes):
                self.write_config()
                self.edit_block(**changes)
                self.assertEqual(self.run_op('status')['approval'], 'changed')
        self.write_config(endpoint=self.endpoint + '/v2')
        self.assertEqual(self.run_op('status')['approval'], 'changed')

    def test_enabled_flag_alone_does_not_invalidate_the_approval(self):
        self.approved()
        self.edit_block(enabled=False)
        self.edit_block(enabled=True)
        self.assertEqual(self.run_op('status')['approval'], 'approved')

    def test_a_forged_approval_file_carried_by_a_clone_is_not_trusted(self):
        self.approved()
        approval = next((self.project / '.frontlights').rglob('approval.json'))
        record = json.loads(approval.read_text(encoding='utf-8'))
        pr.user_key_path().unlink()
        self.assertEqual(self.run_op('status')['approval'], 'unapproved')
        pr.user_key_path().parent.mkdir(parents=True, exist_ok=True)
        pr.user_key_path().write_text('another-machine-key', encoding='utf-8')
        self.assertEqual(self.run_op('status')['approval'], 'unapproved')
        record['signature'] = 'f' * 64
        approval.write_text(json.dumps(record), encoding='utf-8')
        self.assertEqual(self.run_op('status')['approval'], 'unapproved')

    def test_status_passes_the_facts_files_and_the_summary_of_what_would_run(self):
        self.write_config(progress=self.block(factsFile='.frontlights/facts.json', usageFile='.frontlights/usage.json'))
        result = self.run_op('status')
        self.assertEqual((result['factsFile'], result['usageFile']), ('.frontlights/facts.json', '.frontlights/usage.json'))
        self.assertEqual(result['url'], self.endpoint + '/progress-report')
        self.assertEqual(result['secretEnvVar'], 'FRONTLIGHTS_API_SECRET')
        self.assertEqual(result['pushCommand'][0], sys.executable)
        self.assertEqual(result['collectorCommands'][0]['name'], 'usage')

    def test_status_never_touches_the_network(self):
        self.run_op('status')
        self.assertEqual(self.roads.requests, [])

    def test_a_path_that_escapes_the_approved_origin_is_invalid(self):
        for bad in ('../other', '/absolute', '//evil.example/x', 'https://evil.example/x', 'a/../../b', 'a?x=1',
                    'a#frag', 'a\\b', '%2e%2e/x', '', 'a b', 'x/./y'):
            with self.subTest(path=bad):
                self.write_config(progress=self.block(path=bad))
                result = self.run_op('status')
                self.assertEqual((result['ready'], result['valid']), (False, False))
                self.assertIn('progress.path', result['message'])
                self.assertFalse(self.run_op('approve')['ok'])

    def test_a_malformed_block_is_reported_not_raised(self):
        for bad in ({'collectors': 'node x'}, {'collectors': [{'name': 'a', 'command': 'node x'}]},
                    {'collectors': [{'name': '', 'command': ['node']}]}, {'pushCommand': 'node push'},
                    {'pushCommand': []}, {'collectors': [self.collector(timeoutSeconds=0)]},
                    {'collectors': [self.collector(), self.collector()]}):
            with self.subTest(bad=bad):
                self.write_config(progress=self.block(**bad))
                result = self.run_op('status')
                self.assertEqual((result['valid'], result['ready']), (False, False))

    def test_roadmap_sync_behaves_the_same_with_or_without_the_progress_block(self):
        def roadmap_status():
            result = rs.run('status', str(self.project))
            return {key: value for key, value in result.items() if key != 'markerNonceAgeDays'}
        with_block = roadmap_status()
        self.write_config(progress=None)
        self.assertEqual(roadmap_status(), with_block)


class ApproveAndGuardTests(ProgressTestCase):
    def test_window_collect_and_push_refuse_before_approval_and_make_no_call(self):
        draft = self.out / 'draft.json'
        draft.write_text('{}', encoding='utf-8')
        for result in (self.run_op('window'),
                       self.run_op('collect', date_from='2026-09-28', date_to='2026-09-30'),
                       self.run_op('push', draft=str(draft))):
            self.assertFalse(result['ok'])
            self.assertEqual(result['exitCode'], 1)
            self.assertIn('approve', result['message'])
        self.assertEqual(self.roads.requests, [])
        self.assertEqual(self.logged(), [])
        self.assertFalse((self.out / 'push.json').exists())

    def test_operations_refuse_after_the_block_changed(self):
        self.approved()
        self.edit_block(pushCommand=['node', 'other.js', '{draft}'])
        result = self.run_op('window')
        self.assertFalse(result['ok'])
        self.assertIn('changed', result['message'])
        self.assertEqual(self.roads.requests, [])

    def test_unconfigured_or_disabled_projects_refuse_every_operation(self):
        self.write_config(progress=None)
        for operation in ('approve', 'window', 'collect', 'push'):
            result = self.run_op(operation)
            self.assertFalse(result['ok'], operation)
            self.assertEqual(result['exitCode'], 1)
        self.write_config(progress=self.block(enabled=False))
        self.assertFalse(self.run_op('window')['ok'])
        self.assertEqual(self.roads.requests, [])


class WindowTests(ProgressTestCase):
    def setUp(self):
        super().setUp()
        self.approved()

    def test_window_asks_the_route_with_the_secret_in_the_header_only(self):
        result = self.window()
        self.assertTrue(result['ok'], result)
        self.assertEqual(self.roads.requests[0]['path'], '/api/frontlights/progress-report')
        self.assertEqual(self.roads.requests[0]['authorization'], f'Bearer {SECRET}')
        self.assert_never_leaks(result)

    def test_end_at_local_midnight_makes_to_the_day_before(self):
        result = self.window()
        self.assertEqual((result['from'], result['to']), ('2026-09-28', '2026-09-30'))
        self.assertEqual(result['start'], '2026-09-28T00:00:00-03:00')
        self.assertEqual(result['end'], '2026-10-01T00:00:00-03:00')

    def test_send_day_end_is_now_and_to_is_that_day(self):
        result = self.window(state(end='2026-10-01T14:35:12-03:00'))
        self.assertEqual((result['from'], result['to']), ('2026-09-28', '2026-10-01'))

    def test_end_in_another_offset_is_read_in_the_offset_of_the_start(self):
        result = self.window(state(end='2026-10-01T03:00:00Z'))
        self.assertEqual(result['to'], '2026-09-30')
        result = self.window(state(end='2026-10-01T05:00:00Z'))
        self.assertEqual(result['to'], '2026-10-01')

    def test_draft_and_last_sent_are_reported(self):
        result = self.window()
        self.assertEqual((result['draftExists'], result['lastSentPeriod']), (False, None))
        result = self.window(state(draft={'id': 'd1', 'pushed_at': '2026-09-29T10:00:00-03:00', 'rev': 2},
                                   last_sent={'period_start': '2026-09-21', 'period_end': '2026-09-27', 'entries': []}))
        self.assertTrue(result['draftExists'])
        self.assertEqual(result['draftPushedAt'], '2026-09-29T10:00:00-03:00')
        self.assertEqual(result['lastSentPeriod'], {'period_start': '2026-09-21', 'period_end': '2026-09-27'})

    def test_a_bad_secret_is_reported_and_the_body_is_never_echoed(self):
        result = self.window(LEAK, status=401)
        self.assertFalse(result['ok'])
        self.assertEqual(result['exitCode'], 1)
        self.assertIn('401', result['message'])
        self.assert_never_leaks(result)

    def test_route_down_with_an_error_status_names_the_status_not_the_body(self):
        result = self.window(LEAK, status=503)
        self.assertEqual(result['exitCode'], 1)
        self.assertIn('503', result['message'])
        self.assert_never_leaks(result)

    def test_unreachable_server_is_reported_with_its_cause(self):
        probe = socket.socket()
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
        probe.close()
        self.write_config(endpoint=f'http://127.0.0.1:{port}/api/frontlights')
        self.approved()
        result = self.run_op('window')
        self.assertFalse(result['ok'])
        self.assertIn('network error', result['message'])

    def test_malformed_bodies_are_refused_without_echoing_them(self):
        for body in (LEAK, '[1, 2]', json.dumps({'window': LEAK}), json.dumps({'window': {'start': LEAK, 'end': LEAK}}),
                     json.dumps({'window': {'start': '2026-09-28T00:00:00', 'end': '2026-10-01T00:00:00'}}),
                     json.dumps(state(start='2026-10-01T00:00:00-03:00', end='2026-09-28T00:00:00-03:00')),
                     json.dumps({'draft': None, 'lastSent': None})):
            with self.subTest(body=body):
                result = self.window(body)
                self.assertFalse(result['ok'])
                self.assertEqual(result['exitCode'], 1)
                self.assert_never_leaks(result)

    def test_a_redirect_is_never_followed(self):
        result = self.window({}, status=302)
        self.assertFalse(result['ok'])
        self.assertEqual(len(self.roads.requests), 1)


class CollectTests(ProgressTestCase):
    def setUp(self):
        super().setUp()
        self.span = {'date_from': '2026-09-28', 'date_to': '2026-09-30'}

    def collect(self, collectors, **span):
        self.write_config(progress=self.block(collectors=collectors))
        self.approved()
        return self.run_op('collect', **(span or self.span))

    def test_runs_the_collectors_in_order_with_the_substitutions(self):
        result = self.collect([self.collector('um'), self.collector('dois')])
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['exitCode'], 0)
        self.assertEqual([c['name'] for c in result['collectors']], ['um', 'dois'])
        self.assertEqual([c['exitCode'] for c in result['collectors']], [0, 0])
        self.assertIn('conferencia um', result['collectors'][0]['stdoutTail'])
        logged = self.logged()
        self.assertEqual([entry['name'] for entry in logged], ['um', 'dois'])
        self.assertEqual(logged[0]['argv'], ['--from', '2026-09-28', '--to', '2026-09-30'])
        self.assertIn('seconds', result['collectors'][0])

    def test_argv_never_goes_through_a_shell(self):
        tricky = 'com espacos; & echo pwned | "aspas" $(x) `y` {from}'
        collector = self.collector('shell')
        collector['command'] += [tricky]
        result = self.collect([collector])
        self.assertTrue(result['ok'], result)
        self.assertEqual(self.logged()[0]['argv'][-1], tricky.replace('{from}', '2026-09-28'))
        self.assertNotIn('pwned', result['collectors'][0]['stdoutTail'])

    def test_stops_at_the_first_failure_and_describes_nothing_as_updated(self):
        failing = {'name': 'quebra', 'command': [sys.executable, str(self.tools / 'failing.py')]}
        result = self.collect([self.collector('um'), failing, self.collector('tres')])
        self.assertFalse(result['ok'])
        self.assertEqual(result['exitCode'], 1)
        self.assertEqual(result['failed'], 'quebra')
        self.assertEqual([c['name'] for c in result['collectors']], ['um', 'quebra'])
        self.assertEqual(result['collectors'][1]['exitCode'], 3)
        self.assertIn('falhou por causa de X', result['collectors'][1]['stdoutTail'])
        self.assertEqual([entry['name'] for entry in self.logged()], ['um'])

    def test_a_missing_executable_is_a_failure_with_a_cause(self):
        result = self.collect([{'name': 'ausente', 'command': [str(self.tools / 'nao-existe-xyz')]}])
        self.assertEqual(result['exitCode'], 1)
        self.assertEqual(result['failed'], 'ausente')
        self.assertIn('could not start', result['message'])

    def test_timeout_kills_the_collector_and_reports_it(self):
        slow = {'name': 'lento', 'command': [sys.executable, str(self.tools / 'sleeping.py')], 'timeoutSeconds': 1}
        result = self.collect([slow])
        self.assertEqual(result['exitCode'], 1)
        self.assertEqual(result['failed'], 'lento')
        self.assertTrue(result['collectors'][0]['timedOut'])
        self.assertIsNone(result['collectors'][0]['exitCode'])
        self.assertLess(result['collectors'][0]['seconds'], 20)
        self.assertIn('comecei', result['collectors'][0]['stdoutTail'])

    def test_stdout_tail_is_capped_and_keeps_the_end(self):
        result = self.collect([{'name': 'grande', 'command': [sys.executable, str(self.tools / 'big.py')]}])
        tail = result['collectors'][0]['stdoutTail']
        self.assertLessEqual(len(tail.encode('utf-8')), pr.TAIL_BYTES)
        self.assertTrue(tail.rstrip().endswith('FIM'))
        self.assertTrue(result['collectors'][0]['truncated'])

    def test_collectors_do_not_receive_the_secret_and_output_is_redacted(self):
        self.collect([self.collector('um')])
        self.assertFalse(self.logged()[0]['hasSecret'])
        echo = {'name': 'eco', 'command': [sys.executable, str(self.tools / 'echo_secret.py')]}
        self.write_config(progress=self.block(collectors=[echo]))
        self.approved()
        with patch.object(pr, 'collector_environment', side_effect=lambda ctx: dict(os.environ)):
            result = self.run_op('collect', **self.span)
        self.assert_never_leaks(result)

    def test_the_dates_are_validated(self):
        self.write_config()
        self.approved()
        for span in ({'date_from': '28/09/2026', 'date_to': '2026-09-30'}, {'date_from': '2026-09-30', 'date_to': '2026-09-28'},
                     {'date_from': None, 'date_to': '2026-09-30'}, {'date_from': '2026-09-28; rm', 'date_to': '2026-09-30'}):
            with self.subTest(span=span):
                result = self.run_op('collect', **span)
                self.assertFalse(result['ok'])
        self.assertEqual(self.logged(), [])

    def test_the_collectors_run_from_the_project_root(self):
        script = self.tools / 'cwd.py'
        script.write_text('import os\nprint(os.getcwd())\n', encoding='utf-8')
        result = self.collect([{'name': 'cwd', 'command': [sys.executable, str(script)]}])
        self.assertEqual(Path(result['collectors'][0]['stdoutTail'].strip()).resolve(), self.project.resolve())

    def test_no_collectors_configured_is_nothing_to_do(self):
        result = self.collect([])
        self.assertEqual(result['exitCode'], 1)
        self.assertIn('no collectors', result['message'])


class PushTests(ProgressTestCase):
    def setUp(self):
        super().setUp()
        self.approved()
        self.draft = self.out / 'draft.json'
        self.draft.write_text('{"title": "x"}', encoding='utf-8')

    def pushed(self):
        return json.loads((self.out / 'push.json').read_text(encoding='utf-8'))

    def test_runs_the_push_command_with_the_substituted_draft_path(self):
        result = self.run_op('push', draft=str(self.draft))
        self.assertTrue(result['ok'], result)
        self.assertEqual((result['exitCode'], result['commandExitCode']), (0, 0))
        self.assertIn('https://roads.example.test/review/1', result['stdoutTail'])
        self.assertEqual(self.pushed()['argv'], ['--draft', str(self.draft.resolve())])

    def test_the_secret_reaches_the_child_only_through_the_environment(self):
        result = self.run_op('push', draft=str(self.draft))
        pushed = self.pushed()
        self.assertEqual(pushed['secretInEnv'], SECRET)
        self.assertNotIn(SECRET, json.dumps(pushed['argv']))
        self.assert_never_leaks(result)

    def test_a_config_that_puts_the_secret_in_argv_is_refused(self):
        self.write_config(progress=self.block(pushCommand=[sys.executable, str(self.tools / 'push.py'),
                                                           str(self.out / 'push.json'), SECRET, '{draft}']))
        self.approved()
        result = self.run_op('push', draft=str(self.draft))
        self.assertFalse(result['ok'])
        self.assert_never_leaks(result)
        self.assertFalse((self.out / 'push.json').exists())

    def test_output_that_echoes_the_secret_is_redacted(self):
        self.write_config(progress=self.block(pushCommand=[sys.executable, str(self.tools / 'echo_secret.py')]))
        self.approved()
        result = self.run_op('push', draft=str(self.draft))
        self.assertIn('[redacted]', result['stdoutTail'])
        self.assert_never_leaks(result)
        self.assertNotIn(SECRET, pr.render(result))

    def test_shots_and_captions_are_appended_as_given(self):
        shot_a, shot_b = self.out / 'a.png', self.out / 'b.png'
        shot_a.write_bytes(b'x')
        shot_b.write_bytes(b'y')
        result = self.run_op('push', draft=str(self.draft), shots=[str(shot_a), str(shot_b)],
                             captions=['Tela com espaço; e "aspas"'])
        self.assertTrue(result['ok'], result)
        self.assertEqual(self.pushed()['argv'], ['--draft', str(self.draft.resolve()), '--shot', str(shot_a.resolve()),
                                                 '--caption', 'Tela com espaço; e "aspas"', '--shot', str(shot_b.resolve())])

    def test_more_captions_than_shots_or_a_missing_file_is_refused(self):
        self.assertFalse(self.run_op('push', draft=str(self.draft), captions=['x'])['ok'])
        self.assertFalse(self.run_op('push', draft=str(self.draft), shots=[str(self.out / 'nope.png')])['ok'])
        self.assertFalse(self.run_op('push', draft=str(self.out / 'nope.json'))['ok'])
        self.assertFalse(self.run_op('push')['ok'])
        self.assertFalse((self.out / 'push.json').exists())

    def test_a_failing_push_is_exit_1_with_the_tail_and_nothing_claimed(self):
        failing = self.tools / 'push_fail.py'
        failing.write_text('import sys\nprint("recusado: rascunho invalido")\nsys.exit(4)\n', encoding='utf-8')
        self.write_config(progress=self.block(pushCommand=[sys.executable, str(failing)]))
        self.approved()
        result = self.run_op('push', draft=str(self.draft))
        self.assertEqual((result['ok'], result['exitCode'], result['commandExitCode']), (False, 1, 4))
        self.assertIn('recusado', result['stdoutTail'])

    def test_a_push_that_times_out_is_partial(self):
        self.write_config(progress=self.block(pushCommand=[sys.executable, str(self.tools / 'sleeping.py')],
                                              pushTimeoutSeconds=1))
        self.approved()
        result = self.run_op('push', draft=str(self.draft))
        self.assertEqual((result['ok'], result['exitCode']), (False, 2))
        self.assertTrue(result['timedOut'])

    def test_push_needs_the_secret(self):
        with patch.dict(os.environ):
            os.environ.pop('FRONTLIGHTS_API_SECRET')
            result = self.run_op('push', draft=str(self.draft))
        self.assertEqual(result['exitCode'], 1)
        self.assertIn('FRONTLIGHTS_API_SECRET', result['message'])
        self.assertFalse((self.out / 'push.json').exists())

    def test_push_stdout_tail_is_capped(self):
        self.write_config(progress=self.block(pushCommand=[sys.executable, str(self.tools / 'big.py')]))
        self.approved()
        result = self.run_op('push', draft=str(self.draft))
        self.assertLessEqual(len(result['stdoutTail'].encode('utf-8')), pr.TAIL_BYTES)


class CommandLineTests(ProgressTestCase):
    def test_the_script_prints_one_json_object_and_exits_with_the_code(self):
        import subprocess
        script = ROOT / 'scripts' / 'progress_report.py'
        done = subprocess.run([sys.executable, str(script), 'status', '--root', str(self.project)],
                              capture_output=True, text=True, env={**os.environ, 'FRONTLIGHTS_HOME': self.tmp.name})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)['operation'], 'status')
        self.assertNotIn(SECRET, done.stdout + done.stderr)
        done = subprocess.run([sys.executable, str(script), 'window', '--root', str(self.project)],
                              capture_output=True, text=True, env={**os.environ, 'FRONTLIGHTS_HOME': self.tmp.name})
        self.assertEqual(done.returncode, 1)
        self.assertFalse(json.loads(done.stdout)['ok'])


class PluginContractTests(unittest.TestCase):
    """The skill, the reference, the example config and the manifests agree."""

    def read(self, *parts):
        return (ROOT.joinpath(*parts)).read_text(encoding='utf-8')

    def test_skill_asks_the_progress_question_right_after_the_roadmap_question(self):
        stage1 = self.read('skills', 'frontlights', 'SKILL.md').split('## 1.')[1].split('## 2.')[0]
        question = 'Atualizar também o Resumo para a diretoria no RoadS?'
        self.assertIn(question, stage1)
        self.assertIn('progress_report.py', stage1)
        self.assertIn('references/progress-report.md', stage1)
        self.assertLess(stage1.index('Atualizar a sprint e o roadmap de acordo com o RoadS?'), stage1.index(question))
        self.assertLess(stage1.index(question), stage1.index('Then choose the lightest path'))
        self.assertIn('Sim, preparar o resumo agora', stage1)
        self.assertIn('Não, seguir com o pedido', stage1)
        self.assertIn('references/progress-report.md', self.read('skills', 'frontlights', 'SKILL.md').split('## Rules')[0])

    def test_reference_exists_and_covers_every_step(self):
        text = self.read('skills', 'frontlights', 'references', 'progress-report.md')
        for needle in ('progress_report.py', 'status', 'approve', 'window', 'collect', 'push', 'AskUserQuestion',
                       'setx FRONTLIGHTS_API_SECRET', 'Concluído', 'Em validação', 'Em andamento', 'Bloqueado', 'Próximo'):
            self.assertIn(needle, text)

    def test_example_config_has_only_the_generic_disabled_block(self):
        config = json.loads(self.read('examples', 'config.json'))
        progress = config['roadmapSync']['progress']
        self.assertFalse(progress['enabled'])
        self.assertEqual(progress['path'], 'progress-report')
        self.assertEqual(progress['collectors'][0]['command'][1:], ['COLLECTOR_SCRIPT', '--from', '{from}', '--to', '{to}'])
        self.assertEqual(progress['pushCommand'][1:], ['PUSH_SCRIPT', '--draft', '{draft}'])

    def test_manifests_are_bumped_together(self):
        plugin = json.loads(self.read('.claude-plugin', 'plugin.json'))
        market = json.loads(self.read('.claude-plugin', 'marketplace.json'))
        entry = next(p for p in market['plugins'] if p['name'] == plugin['name'])
        self.assertEqual(plugin['version'], '0.12.0')
        self.assertEqual(entry['version'], '0.12.0')

    def test_docs_describe_the_approval(self):
        self.assertIn('progress_report.py', self.read('README.md'))
        self.assertIn('progress_report.py', self.read('docs', 'security.md'))
        self.assertIn('progress', self.read('docs', 'protocol.md'))


if __name__ == '__main__':
    unittest.main()
