import datetime as dt
import http.client
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import roadmap_sync as rs

FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 'roadmap-sync'
FIXTURE = FIXTURES / 'pending-changes.json'
MONDAY = dt.date(2026, 9, 28)
SECRET = 'fixture-secret-value'
CURRENT = 'sprint:sprint-2026-09-28'
NEXT = 'sprint:sprint-2026-10-05'
ADD_ID = '7f1c2d3e-0000-4000-8000-000000000001'
REMOVE_ID = '7f1c2d3e-0000-4000-8000-000000000003'
MOVE_ID = '7f1c2d3e-0000-4000-8000-000000000004'


class Transport:
    """Records every request and answers from a queue of (status, body); an exception in the
    queue is raised instead, as the real transport does for a network error."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append({'method': method, 'url': url, 'headers': headers, 'body': body})
        answer = self.answers.pop(0) if self.answers else (200, '{"acked": 1}')
        if isinstance(answer, Exception):
            raise answer
        status, body_out = answer
        return status, body_out if isinstance(body_out, str) else json.dumps(body_out)


def fixture():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))


def state_fixture():
    return json.loads((FIXTURES / 'roadmap-state.json').read_text(encoding='utf-8'))


def board_fixture():
    return json.loads((FIXTURES / 'sync-board.json').read_text(encoding='utf-8'))


def fetch_answers(payload=None, state=None, board=None):
    """What one fetch reads, in order: sync-board, roadmap-state, pending-changes."""
    return [board if isinstance(board, (tuple, Exception)) else (200, board if board is not None else board_fixture()),
            state if isinstance(state, tuple) else (200, state if state is not None else state_fixture()),
            (200, payload if payload is not None else fixture())]


class RoadmapSyncTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.project = base / 'project'
        self.scrum = base / 'scrum'
        (self.project / '.frontlights').mkdir(parents=True)
        (self.scrum / '2026' / '28_09').mkdir(parents=True)
        self.roadmap = self.scrum / '2026' / 'ROADMAP.md'
        self.sprint = self.scrum / '2026' / '28_09' / 'SPRINT_28_09_a_02_10.md'
        self.roadmap.write_text('# Roadmap 2026\n\n' + 'Linha histórica escrita à mão.\n' * 20, encoding='utf-8')
        self.sprint.write_text('# Sprint 28/09 a 02/10\n\n' + 'Item já planejado.\n' * 10, encoding='utf-8')
        self.write_config()
        env = patch.dict(os.environ, {'FRONTLIGHTS_API_SECRET': SECRET})
        env.start()
        self.addCleanup(env.stop)
        user_env = patch.object(rs, 'windows_user_env', return_value=None)
        user_env.start()
        self.addCleanup(user_env.stop)
        rs._SECRETS.clear()

    def write_config(self, **overrides):
        sync = {'enabled': True, 'endpoint': 'https://roads.example.test/api/frontlights',
                'secretEnvVar': 'FRONTLIGHTS_API_SECRET', 'scrumRoot': str(self.scrum),
                'roadmapFile': '{yyyy}/ROADMAP.md', 'weekFolderPattern': '{yyyy}/{dd_MM}',
                'sprintFilePattern': 'SPRINT_{dd_MM}_a_{dd_MM}.md', 'maxSprintItems': 4,
                'issueTargets': {'Example Product': {'repository': 'OWNER/REPOSITORY',
                                                     'project': {'owner': 'OWNER', 'number': 7}}}}
        sync.update(overrides)
        config = {'repository': 'OWNER/REPOSITORY', 'project': None, 'roads': None, 'roadmapSync': sync}
        (self.project / '.frontlights' / 'config.json').write_text(json.dumps(config), encoding='utf-8')

    def run_op(self, operation, transport=None, today=MONDAY, **kwargs):
        return rs.run(operation, str(self.project), today=today, transport=transport or Transport(), **kwargs)

    def approve_and_fetch(self, payload=None, state=None, board=None, **kwargs):
        self.assertTrue(self.run_op('approve')['ok'])
        transport = Transport(*fetch_answers(payload, state, board))
        result = self.run_op('fetch', transport, **kwargs)
        return result, transport

    def draft(self, plan, declined=(), skip=()):
        """What the session does in step 4: append prose and paste each marker from the plan, in the
        roadmap for every change and in each sprint file for the changes it lists."""
        for name, target in plan['targets'].items():
            staged = Path(target['staged'])
            # Bytes, not text mode: write_text turns a CRLF draft into LF on Linux.
            text = staged.read_bytes().decode('utf-8')
            newline = '\r\n' if '\r\n' in text else '\n'
            added = ''
            for change in plan['changes']:
                if change['id'] in skip or change['alreadyApplied']:
                    continue
                if name == 'roadmap' or change['id'] in target['changeIds']:
                    marker = change['declinedMarker'] if change['id'] in declined else change['marker']
                    added += f"\n## {change['item']['title']}\n\nProsa sobre a mudança. {marker}\n"
            staged.write_bytes((text + added.replace('\n', newline)).encode('utf-8'))


class ConfigurationTests(RoadmapSyncTestCase):
    def test_status_without_configuration_is_not_an_error(self):
        (self.project / '.frontlights' / 'config.json').unlink()
        result = self.run_op('status')
        self.assertTrue(result['ok'])
        self.assertFalse(result['configured'])
        self.assertFalse(result['ready'])

    def test_status_resolves_the_week_files_and_never_touches_the_network(self):
        transport = Transport()
        result = self.run_op('status', transport)
        self.assertTrue(result['configured'])
        self.assertEqual(result['week'], {'start': '2026-09-28', 'end': '2026-10-02'})
        self.assertEqual(result['roadmap']['path'], str(self.roadmap))
        self.assertEqual(result['sprint']['path'], str(self.sprint))
        self.assertEqual((result['secret'], result['approval']), ('present', 'unapproved'))
        self.assertFalse(result['ready'])
        self.assertEqual(transport.calls, [])

    def test_weekend_belongs_to_the_week_that_is_closing(self):
        result = self.run_op('status', today=dt.date(2026, 10, 4))
        self.assertEqual(result['week']['start'], '2026-09-28')

    def test_secret_variable_must_carry_the_frontlights_prefix(self):
        self.write_config(secretEnvVar='GUARDIANS_API_SECRET')
        result = self.run_op('status')
        self.assertFalse(result['configured'])
        self.assertIn('^FRONTLIGHTS_', result['message'])

    def test_endpoint_must_be_https_without_credentials(self):
        for endpoint in ('http://roads.example.test/api', 'https://user:pw@roads.example.test/api',
                         'https://roads.example.test/api?x=1'):
            with self.subTest(endpoint=endpoint):
                self.write_config(endpoint=endpoint)
                self.assertFalse(self.run_op('status')['configured'])
        self.write_config(endpoint='http://localhost:3000/api/frontlights')
        self.assertTrue(self.run_op('status')['configured'])

    def test_scrum_root_expands_environment_variables(self):
        with patch.dict(os.environ, {'FIXTURE_SCRUM': str(self.scrum)}):
            self.write_config(scrumRoot='%FIXTURE_SCRUM%' if sys.platform.startswith('win') else '$FIXTURE_SCRUM')
            self.assertEqual(self.run_op('status')['roadmap']['path'], str(self.roadmap))

    def test_approval_covers_the_full_url_and_the_variable(self):
        self.run_op('approve')
        self.assertEqual(self.run_op('status')['approval'], 'approved')
        self.write_config(endpoint='https://roads.example.test/api/other-tenant')
        self.assertEqual(self.run_op('status')['approval'], 'changed')


class FetchTests(RoadmapSyncTestCase):
    def test_fetch_refuses_before_approval_without_calling_the_service(self):
        transport = Transport()
        result = self.run_op('fetch', transport)
        self.assertFalse(result['ok'])
        self.assertIn('not approved', result['message'])
        self.assertEqual(transport.calls, [])

    def test_fetch_sends_the_bearer_only_to_the_approved_path_and_builds_the_plan(self):
        result, transport = self.approve_and_fetch()
        self.assertTrue(result['ok'], result['message'])
        base = 'https://roads.example.test/api/frontlights/'
        self.assertEqual([(c['method'], c['url'], c['body']) for c in transport.calls],
                         [('POST', base + 'sync-board', None), ('GET', base + 'roadmap-state', None),
                          ('GET', base + 'pending-changes', None)])
        for call in transport.calls:
            self.assertEqual(call['headers']['Authorization'], f'Bearer {SECRET}')
        plan = result['plan']
        self.assertEqual(list(plan['targets']), ['roadmap', CURRENT])
        self.assertEqual(plan['targets'][CURRENT]['path'], str(self.sprint))
        self.assertEqual(plan['targets']['roadmap']['path'], str(self.roadmap))
        self.assertEqual(plan['asOf'], '2026-09-28T12:00:00.000Z')
        self.assertEqual(plan['asOfSource'], 'server')
        self.assertEqual(len(plan['pending']), 4)
        by_id = {c['id']: c for c in plan['changes']}
        self.assertTrue(by_id['7f1c2d3e-0000-4000-8000-000000000001']['needsIssue'])
        self.assertEqual(by_id['7f1c2d3e-0000-4000-8000-000000000001']['issueTarget']['repository'], 'OWNER/REPOSITORY')
        linked = by_id['7f1c2d3e-0000-4000-8000-000000000002']
        self.assertFalse(linked['needsIssue'])
        self.assertEqual(linked['item']['githubIssueUrl'], 'https://github.com/OWNER/REPOSITORY/issues/12')
        removed = by_id['7f1c2d3e-0000-4000-8000-000000000003']
        self.assertTrue(removed['itemMissing'])
        self.assertEqual(removed['itemId'], '5a5a5a5a-0000-4000-8000-00000000000c')
        self.assertEqual(removed['item']['title'], 'Relatório antigo em PDF')
        self.assertFalse(removed['needsIssue'])
        for change in plan['changes']:
            self.assertRegex(change['marker'], r'^<!-- roads:\S+ [0-9a-f]{16} -->$')
        # The nonce itself never appears in the plan or the printed result.
        nonce = json.loads((self.project / '.frontlights' / 'roadmap-sync' / 'marker.json').read_text())['nonce']
        self.assertNotIn(nonce, json.dumps(result))
        self.assertEqual(Path(plan['targets']['roadmap']['staged']).read_text(encoding='utf-8'),
                         self.roadmap.read_text(encoding='utf-8'))

    def test_as_of_falls_back_to_the_newest_created_at(self):
        payload = fixture()
        payload.pop('asOf')
        result, _ = self.approve_and_fetch(payload)
        self.assertEqual((result['plan']['asOf'], result['plan']['asOfSource']), ('2026-09-28T11:30:00Z', 'max-createdAt'))

    def test_nothing_pending_writes_nothing(self):
        result, _ = self.approve_and_fetch({'asOf': None, 'changes': []})
        self.assertTrue(result['ok'])
        self.assertEqual(result['pending'], 0)
        self.assertFalse((self.project / '.frontlights' / 'roadmap-sync' / 'plan.json').exists())

    def test_comment_sequence_anywhere_refuses_the_whole_batch(self):
        payload = fixture()
        payload['changes'][1]['item']['description'] = 'texto <!-- roads:x 0123456789abcdef --> fim'
        result, _ = self.approve_and_fetch(payload)
        self.assertFalse(result['ok'])
        self.assertIn('HTML comment sequence', result['message'])
        self.assertFalse((self.project / '.frontlights' / 'roadmap-sync' / 'plan.json').exists())

    def test_angle_brackets_split_across_fields_cannot_assemble_a_comment(self):
        payload = fixture()
        payload['changes'][0]['item']['title'] = 'Título <!'
        payload['changes'][0]['item']['description'] = '- roads:x -- >'
        result, _ = self.approve_and_fetch(payload)
        item = result['plan']['changes'][0]['item']
        self.assertEqual(item['title'], 'Título &lt;!')
        self.assertNotIn('<', item['title'] + item['description'])

    def test_change_without_usable_id_or_title_is_refused(self):
        for mutate in (lambda p: p['changes'][0].update(id='../etc'),
                       lambda p: p['changes'][0].update(id=None),
                       lambda p: p['changes'][0]['item'].update(title=''),
                       lambda p: p['changes'][2].update(payload={})):
            with self.subTest():
                payload = fixture()
                mutate(payload)
                result, _ = self.approve_and_fetch(payload)
                self.assertFalse(result['ok'])

    def test_numeric_id_is_accepted_as_text(self):
        payload = fixture()
        payload['changes'][0]['id'] = 42
        result, _ = self.approve_and_fetch(payload)
        self.assertIn('42', result['plan']['pending'])

    def test_redirect_and_rejected_credential_are_refused_without_leaking_the_secret(self):
        self.run_op('approve')
        for status, fragment in ((302, 'redirect'), (401, 'rejected the credential'), (500, 'HTTP 500')):
            with self.subTest(status=status):
                result = self.run_op('fetch', Transport((status, ''), (status, ''), (status, '')))
                self.assertFalse(result['ok'])
                self.assertIn(fragment, result['message'])
                self.assertNotIn(SECRET, rs.protect(json.dumps(result)))

    def test_protect_redacts_the_secret_once_it_was_read(self):
        rs.read_secret('FRONTLIGHTS_API_SECRET')
        self.assertEqual(rs.protect(f'token {SECRET} here'), 'token [redacted] here')

    def test_fetch_keeps_a_draft_that_was_never_applied(self):
        result, _ = self.approve_and_fetch()
        staged = Path(result['plan']['targets']['roadmap']['staged'])
        staged.write_text(staged.read_text(encoding='utf-8') + '\nRascunho.\n', encoding='utf-8')
        refused_transport = Transport(*fetch_answers())
        again = self.run_op('fetch', refused_transport)
        self.assertFalse(again['ok'])
        self.assertIn('drafted prose', again['message'])
        self.assertEqual(refused_transport.calls, [])
        self.assertIn('Rascunho.', staged.read_text(encoding='utf-8'))
        discarded = self.run_op('fetch', Transport(*fetch_answers()), discard_staged=True)
        self.assertTrue(discarded['ok'])
        self.assertNotIn('Rascunho.', staged.read_text(encoding='utf-8'))


class ApplyTests(RoadmapSyncTestCase):
    def test_apply_writes_with_backup_verifies_markers_and_acknowledges(self):
        result, _ = self.approve_and_fetch()
        before = self.roadmap.read_text(encoding='utf-8')
        self.draft(result['plan'])
        transport = Transport((200, {'acked': 4}))
        applied = self.run_op('apply', transport)
        self.assertTrue(applied['ok'], applied['message'])
        self.assertEqual(applied['exitCode'], 0)
        self.assertIn(str(self.roadmap), applied['written'])
        backup = Path(applied['backups'][0])
        self.assertEqual(backup.read_text(encoding='utf-8'), before)
        self.assertEqual(transport.calls[0]['url'], 'https://roads.example.test/api/frontlights/ack')
        self.assertEqual(json.loads(transport.calls[0]['body']), {'asOf': '2026-09-28T12:00:00.000Z'})
        self.assertEqual(applied['acked'], 4)
        state = json.loads((self.project / '.frontlights' / 'roadmap-sync' / 'state.json').read_text())
        self.assertEqual(state['lastAckAsOf'], '2026-09-28T12:00:00.000Z')
        # A second fetch of the same changes sees them as already applied.
        second = self.run_op('fetch', Transport(*fetch_answers()))
        self.assertEqual(second['plan']['pending'], [])

    def test_missing_marker_writes_but_does_not_acknowledge_and_the_retry_works(self):
        result, _ = self.approve_and_fetch()
        skipped = result['plan']['changes'][0]['id']
        self.draft(result['plan'], skip={skipped})
        transport = Transport()
        applied = self.run_op('apply', transport)
        self.assertEqual(applied['exitCode'], 1)
        self.assertEqual(applied['missingMarkers'], [skipped])
        self.assertTrue(applied['written'])
        self.assertEqual(transport.calls, [])
        staged = Path(result['plan']['targets']['roadmap']['staged'])
        staged.write_text(staged.read_text(encoding='utf-8') + f"\n{result['plan']['changes'][0]['marker']}\n", encoding='utf-8')
        retry = self.run_op('apply', Transport((200, {'acked': 4})))
        self.assertTrue(retry['ok'], retry['message'])

    def test_shrinking_or_dropping_a_marker_is_refused_and_writes_nothing(self):
        result, _ = self.approve_and_fetch()
        self.draft(result['plan'])
        self.assertTrue(self.run_op('apply', Transport((200, {'acked': 4})))['ok'])
        written = self.roadmap.read_text(encoding='utf-8')
        payload = fixture()
        payload['changes'] = [dict(payload['changes'][0], id='7f1c2d3e-0000-4000-8000-0000000000ff')]
        second = self.run_op('fetch', Transport(*fetch_answers(payload)))
        staged = Path(second['plan']['targets']['roadmap']['staged'])
        staged.write_text('# Roadmap 2026\n', encoding='utf-8')
        shrink = self.run_op('apply')
        self.assertFalse(shrink['ok'])
        self.assertIn('dropping', shrink['message'])
        self.assertEqual(self.roadmap.read_text(encoding='utf-8'), written)
        # Same length, but a marker a previous run wrote is gone.
        first_marker = result['plan']['changes'][0]['marker']
        staged.write_text(written.replace(first_marker, ' ' * len(first_marker)), encoding='utf-8')
        lost = self.run_op('apply')
        self.assertFalse(lost['ok'])
        self.assertIn('lost the marker', lost['message'])
        self.assertEqual(self.roadmap.read_text(encoding='utf-8'), written)

    def test_marker_for_a_change_outside_the_plan_is_refused(self):
        result, _ = self.approve_and_fetch()
        self.draft(result['plan'])
        nonce = json.loads((self.project / '.frontlights' / 'roadmap-sync' / 'marker.json').read_text())['nonce']
        staged = Path(result['plan']['targets'][CURRENT]['staged'])
        staged.write_text(staged.read_text(encoding='utf-8') + rs.marker_text('outra-mudanca', nonce), encoding='utf-8')
        applied = self.run_op('apply')
        self.assertFalse(applied['ok'])
        self.assertIn('not in this plan', applied['message'])
        self.assertEqual(applied['written'], [])

    def test_target_edited_after_fetch_is_refused(self):
        result, _ = self.approve_and_fetch()
        self.draft(result['plan'])
        self.roadmap.write_text(self.roadmap.read_text(encoding='utf-8') + 'Edição manual.\n', encoding='utf-8')
        applied = self.run_op('apply')
        self.assertFalse(applied['ok'])
        self.assertIn('changed after the plan', applied['message'])

    def test_declined_change_needs_a_separate_confirmation_before_the_ack(self):
        result, _ = self.approve_and_fetch()
        declined = result['plan']['changes'][2]['id']
        self.draft(result['plan'], declined={declined})
        transport = Transport()
        applied = self.run_op('apply', transport)
        self.assertEqual(applied['exitCode'], 1)
        self.assertEqual(applied['declined'], [declined])
        self.assertEqual(transport.calls, [])
        confirmed = self.run_op('ack', Transport((200, {'acked': 4})), confirm_declined=True)
        self.assertTrue(confirmed['ok'], confirmed['message'])
        self.assertTrue(confirmed['declinedConfirmed'])

    def test_failed_ack_keeps_the_files_and_is_retryable(self):
        result, _ = self.approve_and_fetch()
        self.draft(result['plan'])
        applied = self.run_op('apply', Transport((503, '')))
        self.assertEqual(applied['exitCode'], 2)
        self.assertTrue(applied['retryable'])
        self.assertTrue(applied['written'])
        self.assertTrue(self.run_op('ack', Transport((200, {'acked': 4})))['ok'])

    def test_no_as_of_skips_the_ack_as_not_retryable(self):
        payload = fixture()
        payload.pop('asOf')
        for change in payload['changes']:
            change.pop('createdAt')
        result, _ = self.approve_and_fetch(payload)
        self.draft(result['plan'])
        applied = self.run_op('apply')
        self.assertEqual((applied['exitCode'], applied['ack'], applied['retryable']), (2, 'skipped', False))

    def test_plan_edited_to_point_elsewhere_is_refused(self):
        result, _ = self.approve_and_fetch()
        self.draft(result['plan'])
        plan_path = self.project / '.frontlights' / 'roadmap-sync' / 'plan.json'
        plan = json.loads(plan_path.read_text(encoding='utf-8'))
        plan['targets']['roadmap']['staged'] = str(self.project / 'elsewhere.md')
        plan_path.write_text(json.dumps(plan), encoding='utf-8')
        applied = self.run_op('apply')
        self.assertFalse(applied['ok'])
        self.assertIn('outside the staging directory', applied['message'])

    def test_backups_keep_a_bounded_number_of_generations(self):
        for index in range(rs.BACKUP_GENERATIONS + 3):
            rs.write_text_atomic(self.roadmap, self.roadmap.read_text(encoding='utf-8') + f'{index}\n', False,
                                 rs.new_backup_path(self.roadmap).with_name(rs.backup_prefix(self.roadmap) + f'-{index:03}'))
        self.assertEqual(len(rs.backup_generations(self.roadmap)), rs.BACKUP_GENERATIONS)

    def test_bom_and_line_endings_are_preserved(self):
        self.roadmap.write_bytes(b'\xef\xbb\xbf# Roadmap\r\n' + 'Linha\r\n'.encode() * 30)
        result, _ = self.approve_and_fetch()
        self.draft(result['plan'])
        self.assertTrue(self.run_op('apply', Transport((200, {'acked': 4})))['ok'])
        data = self.roadmap.read_bytes()
        self.assertTrue(data.startswith(b'\xef\xbb\xbf# Roadmap\r\n'))


class PathAndMarkerTests(RoadmapSyncTestCase):
    def test_pattern_expansion_uses_start_then_end(self):
        start, end = rs.sprint_week(MONDAY)
        # Each pattern counts on its own: the folder's only {dd_MM} is the start, and the file's
        # first is the start and its second the end.
        self.assertEqual(rs.expand_pattern('{yyyy}/{dd_MM}', start, end), '2026/28_09')
        self.assertEqual(rs.expand_pattern('SPRINT_{dd_MM}_a_{dd_MM}.md', start, end), 'SPRINT_28_09_a_02_10.md')

    def test_unsafe_relative_paths_are_refused(self):
        for relative in ('../fora.md', '2026/ROADMAP.md.', '2026/ /x.md', 'a:b.md'):
            with self.subTest(relative=relative):
                with self.assertRaises(rs.Refusal):
                    rs.safe_target(str(self.scrum), relative)

    def test_relative_scrum_root_is_refused(self):
        with self.assertRaises(rs.Refusal):
            rs.safe_target('scrum', '2026/ROADMAP.md')

    @unittest.skipUnless(sys.platform.startswith('win'), 'drive roots are a Windows concept')
    def test_drive_root_is_refused(self):
        with self.assertRaises(rs.Refusal):
            rs.safe_target('C:\\', '2026/ROADMAP.md')

    def test_cloud_reparse_tags_are_storage_not_redirection(self):
        self.assertTrue(rs.cloud_tag(0x9000001A))
        self.assertTrue(rs.cloud_tag(0x9000601A))
        self.assertFalse(rs.cloud_tag(0xA0000003))  # mount point / junction
        self.assertFalse(rs.cloud_tag(0xA000000C))  # symbolic link

    def test_marker_needs_the_local_nonce(self):
        nonce, other = rs.new_nonce(), rs.new_nonce()
        text = rs.marker_text('abc', nonce)
        self.assertEqual(rs.marker_inventory(text, nonce), {'abc': 'applied'})
        self.assertEqual(rs.marker_inventory(text, other), {})
        self.assertEqual(rs.marker_state(rs.marker_text('abc', nonce, True), 'abc', nonce), 'declined')

    def test_rotation_rewrites_markers_under_a_new_nonce(self):
        result, _ = self.approve_and_fetch()
        self.draft(result['plan'])
        self.assertTrue(self.run_op('apply', Transport((200, {'acked': 4})))['ok'])
        rotated = self.run_op('rotate-markers')
        self.assertTrue(rotated['ok'], rotated['message'])
        self.assertEqual(len(rotated['rotatedMarkers']), 4)
        nonce = json.loads((self.project / '.frontlights' / 'roadmap-sync' / 'marker.json').read_text())['nonce']
        self.assertEqual(len(rs.marker_inventory(self.roadmap.read_text(encoding='utf-8'), nonce)), 4)


class SyncBoardTests(RoadmapSyncTestCase):
    def test_board_sync_runs_first_and_its_counts_are_reported(self):
        result, transport = self.approve_and_fetch()
        self.assertTrue(result['ok'], result['message'])
        self.assertEqual(result['syncBoard'], {'ok': True, 'ran': True, 'syncedAt': '2026-09-28T11:59:00Z',
                                               'added': 2, 'removed': 1, 'issuesCreated': 0})
        self.assertFalse(result['syncBoardFailed'])
        self.assertTrue(transport.calls[0]['url'].endswith('/sync-board'))

    def test_cooldown_only_informs(self):
        board = {'ok': True, 'ran': False, 'syncedAt': '2026-09-28T11:50:00Z',
                 'roadmap': {'added': 0, 'removed': 0, 'issuesCreated': 0}}
        result, _ = self.approve_and_fetch(board=board)
        self.assertTrue(result['ok'], result['message'])
        self.assertFalse(result['syncBoard']['ran'])
        self.assertFalse(result['syncBoardFailed'])
        self.assertIn('plan', result)

    def test_a_failed_board_sync_does_not_abort_the_fetch(self):
        failures = {
            'refused': ({'ok': False, 'reason': 'no_access', 'message': 'Sem acesso ao quadro.'}, 'no_access'),
            'unauthorized': ((401, {'error': 'unauthorized'}), 'request_failed'),
            'server error': ((500, ''), 'request_failed'),
            'network': (rs.Refusal('network error contacting RoadS (URLError)'), 'request_failed'),
            'not json': ((200, 'oops'), 'invalid_response'),
            'comment sequence': ({'ok': False, 'reason': 'error', 'message': 'x <!-- y'}, 'invalid_response'),
        }
        for label, (board, reason) in failures.items():
            with self.subTest(label):
                rs._SECRETS.clear()
                result, transport = self.approve_and_fetch(board=board, discard_staged=True)
                self.assertTrue(result['ok'], result['message'])
                self.assertTrue(result['syncBoardFailed'])
                self.assertFalse(result['syncBoard']['ok'])
                self.assertEqual(result['syncBoard']['reason'], reason)
                self.assertIn('ask the user', result['message'])
                self.assertEqual(len(transport.calls), 3)
                self.assertIn('plan', result)
                self.assertNotIn(SECRET, rs.protect(json.dumps(result)))
        self.assertIn('rejected the credential', self.approve_and_fetch(
            board=(401, {'error': 'unauthorized'}), discard_staged=True)[0]['syncBoard']['message'])

    def test_a_transport_exception_on_the_board_sync_does_not_abort_the_fetch(self):
        for error in (TimeoutError('timed out'), http.client.IncompleteRead(b'parcial'), ConnectionResetError()):
            with self.subTest(error=type(error).__name__):
                rs._SECRETS.clear()
                result, transport = self.approve_and_fetch(board=error, discard_staged=True)
                self.assertTrue(result['ok'], result['message'])
                self.assertTrue(result['syncBoardFailed'])
                self.assertEqual(result['syncBoard']['reason'], 'request_failed')
                self.assertIn(type(error).__name__, result['syncBoard']['message'])
                self.assertNotIn('parcial', result['syncBoard']['message'])
                self.assertEqual(len(transport.calls), 3)
                self.assertIn('plan', result)

    def test_board_error_inside_a_successful_answer_is_reported(self):
        board = board_fixture()
        board['roadmap']['error'] = 'Falha ao ler o Project.'
        result, _ = self.approve_and_fetch(board=board)
        self.assertEqual(result['syncBoard']['error'], 'Falha ao ler o Project.')


class RoadmapStateTests(RoadmapSyncTestCase):
    def test_valid_state_is_exposed_per_sprint(self):
        result, _ = self.approve_and_fetch()
        self.assertTrue(result['ok'], result['message'])
        plan = result['plan']
        self.assertEqual(plan['maxSprintItems'], 4)
        self.assertEqual(plan['stateAsOf'], '2026-09-28T12:00:00Z')
        sprints = {s['sprintId']: s for s in plan['sprints']}
        current = sprints['sprint-2026-09-28']
        self.assertEqual((current['startDate'], current['endDate'], current['target']), ('2026-09-28', '2026-10-02', CURRENT))
        self.assertEqual([i['position'] for i in current['items']], [1, 2, 3])
        self.assertEqual(current['outOfLimit'], [])
        self.assertIsNone(sprints['sprint-2026-10-05']['target'])
        self.assertEqual(plan['targets'][CURRENT]['changeIds'],
                         ['7f1c2d3e-0000-4000-8000-000000000002', '7f1c2d3e-0000-4000-8000-000000000004'])
        by_id = {c['id']: c for c in plan['changes']}
        self.assertEqual(by_id['7f1c2d3e-0000-4000-8000-000000000004']['sprintTargets'], [CURRENT])
        self.assertEqual(by_id[ADD_ID]['sprintTargets'], [])
        self.assertFalse(result['snapshotStale'])

    def test_unknown_schema_version_is_refused(self):
        for version in (2, '1', True, None):
            with self.subTest(version=version):
                state = state_fixture()
                state['schemaVersion'] = version
                result, transport = self.approve_and_fetch(state=state)
                self.assertFalse(result['ok'])
                self.assertIn('schemaVersion', result['message'])
                self.assertEqual(len(transport.calls), 2)
                self.assertFalse((self.project / '.frontlights' / 'roadmap-sync' / 'plan.json').exists())

    def test_an_unknown_schema_version_is_not_echoed(self):
        state = state_fixture()
        state['schemaVersion'] = 'v' * 5000 + '<!-- injetado -->'
        result, _ = self.approve_and_fetch(state=state)
        self.assertFalse(result['ok'])
        self.assertIn('schemaVersion', result['message'])
        self.assertNotIn('<!--', result['message'])
        self.assertNotIn('vvvvvvvvvv', result['message'])
        self.assertLess(len(result['message']), 400)

    def test_dates_outside_the_supported_years_are_refused(self):
        for start, end in (('0001-01-01', '0001-01-05'), ('9999-12-27', '9999-12-31'),
                           ('1999-12-27', '1999-12-31'), ('2101-01-03', '2101-01-07')):
            with self.subTest(start=start):
                state = state_fixture()
                state['sprints'][1].update(startDate=start, endDate=end)
                result, _ = self.approve_and_fetch(state=state, discard_staged=True)
                self.assertFalse(result['ok'])
                self.assertIn('roadmap-state', result['message'])
                self.assertIn('2000', result['message'])
                self.assertFalse((self.project / '.frontlights' / 'roadmap-sync' / 'plan.json').exists())

    def test_invalid_dates_are_refused(self):
        for start, end in (('2026-13-01', '2026-13-05'), ('28/09/2026', '02/10/2026'), ('2026-09-28', '2026-09-27'),
                           ('2026-09-28T00:00:00Z', '2026-10-02'), (None, '2026-10-02')):
            with self.subTest(start=start, end=end):
                state = state_fixture()
                state['sprints'][0].update(startDate=start, endDate=end)
                result, _ = self.approve_and_fetch(state=state)
                self.assertFalse(result['ok'])
                self.assertIn('roadmap-state', result['message'])

    def test_fields_of_the_wrong_type_are_refused(self):
        mutations = {
            'sprints not a list': lambda s: s.update(sprints={}),
            'items not a list': lambda s: s['sprints'][0].update(items=None),
            'position as text': lambda s: s['sprints'][0]['items'][0].update(position='1'),
            'position zero': lambda s: s['sprints'][0]['items'][0].update(position=0),
            'overLimit as text': lambda s: s['sprints'][0]['items'][0].update(overLimit='false'),
            'overLimit missing': lambda s: s['sprints'][0]['items'][0].pop('overLimit'),
            'done as number': lambda s: s['sprints'][0]['items'][0].update(done=0),
            'pendingChangeIds as text': lambda s: s['sprints'][0]['items'][0].update(pendingChangeIds='x'),
            'pending id unusable': lambda s: s['sprints'][0]['items'][0].update(pendingChangeIds=['../x']),
            'title as number': lambda s: s['sprints'][0]['items'][0].update(title=5),
            'unknown status': lambda s: s['sprints'][0]['items'][0].update(status='archived'),
            'issueNumber as text': lambda s: s['sprints'][0]['items'][0].update(issueNumber='12'),
            'bad updatedAt': lambda s: s['sprints'][0]['items'][0].update(updatedAt='ontem'),
            'bad asOf': lambda s: s.update(asOf='agora'),
            'bad snapshotSyncedAt': lambda s: s.update(snapshotSyncedAt=12),
            'maxSprintItems as text': lambda s: s.update(maxSprintItems='4'),
            'duplicate sprint': lambda s: s['sprints'].append(dict(s['sprints'][0])),
            'removedPending not a list': lambda s: s.update(removedPending={}),
            'comment sequence': lambda s: s['sprints'][0].update(title='a <!-- b'),
        }
        for label, mutate in mutations.items():
            with self.subTest(label):
                state = state_fixture()
                mutate(state)
                result, _ = self.approve_and_fetch(state=state)
                self.assertFalse(result['ok'], label)
                self.assertFalse((self.project / '.frontlights' / 'roadmap-sync' / 'plan.json').exists())

    def test_missing_roadmap_state_is_refused_without_fallback(self):
        for answer in ((404, '{"error":"not_found"}'), (500, ''), (200, 'not json')):
            with self.subTest(answer=answer):
                result, transport = self.approve_and_fetch(state=answer)
                self.assertFalse(result['ok'])
                self.assertIn('roadmap-state', result['message'])
                self.assertIn('no fallback', result['message'])
                self.assertEqual([c['url'].rsplit('/', 1)[1] for c in transport.calls], ['sync-board', 'roadmap-state'])
                self.assertIn('syncBoard', result)
                self.assertFalse((self.project / '.frontlights' / 'roadmap-sync' / 'plan.json').exists())

    def test_snapshot_older_than_a_day_or_missing_is_stale(self):
        for synced, stale in ((None, True), ('2026-09-27T11:59:59Z', True), ('2026-09-27T12:00:00Z', False),
                              ('2026-09-28T11:55:00Z', False)):
            with self.subTest(synced=synced):
                state = state_fixture()
                state['snapshotSyncedAt'] = synced
                result, _ = self.approve_and_fetch(state=state, discard_staged=True)
                self.assertTrue(result['ok'], result['message'])
                self.assertIs(result['snapshotStale'], stale)
                self.assertEqual(result['snapshotSyncedAt'], synced)
                if stale:
                    self.assertIn('snapshot', result['message'])

    def test_snapshot_is_reported_even_when_nothing_is_pending(self):
        state = state_fixture()
        state['snapshotSyncedAt'] = None
        result, _ = self.approve_and_fetch({'asOf': None, 'changes': []}, state=state)
        self.assertEqual(result['pending'], 0)
        self.assertTrue(result['snapshotStale'])
        self.assertIn('syncBoard', result)


class SprintTargetTests(RoadmapSyncTestCase):
    def next_sprint_state(self):
        state = state_fixture()
        state['sprints'][1]['items'][0]['pendingChangeIds'] = [ADD_ID]
        return state

    def test_a_new_sprint_gets_a_new_folder_and_file_inside_the_scrum_root(self):
        result, _ = self.approve_and_fetch(state=self.next_sprint_state())
        self.assertTrue(result['ok'], result['message'])
        plan = result['plan']
        target = plan['targets'][NEXT]
        expected = self.scrum / '2026' / '05_10' / 'SPRINT_05_10_a_09_10.md'
        self.assertEqual(target['path'], str(expected))
        self.assertEqual((target['exists'], target['createsFolder']), (False, True))
        self.assertEqual(target['template'], str(self.sprint))
        self.assertEqual(target['changeIds'], [ADD_ID])
        self.assertEqual(Path(target['staged']).read_bytes(), b'')
        self.assertFalse(expected.parent.exists())
        self.draft(plan)
        applied = self.run_op('apply', Transport((200, {'acked': 4})))
        self.assertTrue(applied['ok'], applied['message'])
        self.assertIn(str(expected), applied['written'])
        self.assertIn(plan['changes'][0]['marker'], expected.read_text(encoding='utf-8'))

    def test_template_is_null_without_an_earlier_sprint_file(self):
        self.sprint.unlink()
        result, _ = self.approve_and_fetch(state=self.next_sprint_state())
        self.assertIsNone(result['plan']['targets'][NEXT]['template'])
        self.assertFalse(result['plan']['targets'][CURRENT]['exists'])

    def test_a_sprint_path_outside_the_scrum_root_is_refused(self):
        self.write_config(weekFolderPattern='../fora_{dd_MM}')
        result, _ = self.approve_and_fetch(state=self.next_sprint_state())
        self.assertFalse(result['ok'])
        self.assertFalse((self.scrum.parent / 'fora_05_10').exists())
        self.assertFalse((self.project / '.frontlights' / 'roadmap-sync' / 'plan.json').exists())

    def test_a_junction_on_the_way_to_a_new_sprint_is_refused(self):
        outside = self.scrum.parent / 'outside'
        outside.mkdir()
        link = self.scrum / '2026' / '05_10'
        if sys.platform.startswith('win'):
            import subprocess
            made = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(outside)], capture_output=True)
            if made.returncode != 0:
                self.skipTest('cannot create a junction here')
        else:
            os.symlink(outside, link, target_is_directory=True)
        result, _ = self.approve_and_fetch(state=self.next_sprint_state())
        self.assertFalse(result['ok'])
        self.assertIn('reparse point', result['message'])
        self.assertEqual(list(outside.iterdir()), [])

    def test_a_removal_from_a_sprint_lane_targets_that_sprint(self):
        state = state_fixture()
        state['removedPending'][0]['laneId'] = 'atual'
        result, _ = self.approve_and_fetch(state=state)
        self.assertIn(REMOVE_ID, result['plan']['targets'][CURRENT]['changeIds'])

    def moved_out(self, lane_to):
        """Change 4 moves item d out of the current sprint: it leaves the sprint's items and, when
        it lands in a group or another sprint, that lane lists the change."""
        state, payload = state_fixture(), fixture()
        moved = state['sprints'][0]['items'].pop(1)
        if lane_to == 'g1':
            state['groups'][0]['items'].append({k: v for k, v in moved.items() if k != 'overLimit'})
        else:
            state['sprints'][1]['items'].append(dict(moved, position=2))
        payload['changes'][3]['payload'] = {'from': 'atual', 'to': lane_to}
        return state, payload

    def test_a_move_out_of_a_sprint_to_a_group_targets_the_origin_sprint(self):
        state, payload = self.moved_out('g1')
        result, _ = self.approve_and_fetch(payload, state=state)
        self.assertTrue(result['ok'], result['message'])
        self.assertIn(MOVE_ID, result['plan']['targets'][CURRENT]['changeIds'])
        self.assertEqual(next(c for c in result['plan']['changes'] if c['id'] == MOVE_ID)['sprintTargets'], [CURRENT])

    def test_a_move_to_another_sprint_targets_both_sprints(self):
        state, payload = self.moved_out('proxima')
        result, _ = self.approve_and_fetch(payload, state=state)
        self.assertTrue(result['ok'], result['message'])
        targets = result['plan']['targets']
        self.assertIn(MOVE_ID, targets[CURRENT]['changeIds'])
        self.assertEqual(targets[NEXT]['changeIds'], [MOVE_ID])
        self.assertEqual(next(c for c in result['plan']['changes'] if c['id'] == MOVE_ID)['sprintTargets'], [CURRENT, NEXT])

    def test_a_move_from_a_group_into_a_sprint_targets_the_destination(self):
        state, payload = state_fixture(), fixture()
        state['sprints'][0]['items'].pop(1)
        payload['changes'][3]['payload'] = {'from': 'g1', 'to': 'proxima'}
        for listed in (False, True):
            with self.subTest(listed=listed):
                if listed:
                    state['sprints'][1]['items'].append(dict(state_fixture()['sprints'][0]['items'][1], position=2))
                result, _ = self.approve_and_fetch(payload, state=state, discard_staged=True)
                self.assertTrue(result['ok'], result['message'])
                self.assertEqual(result['plan']['targets'][NEXT]['changeIds'], [MOVE_ID])
                self.assertNotIn(MOVE_ID, result['plan']['targets'][CURRENT]['changeIds'])

    def test_unsafe_patterns_are_refused_before_any_network_call(self):
        for overrides in ({'weekFolderPattern': '../fora_{dd_MM}'},
                          {'weekFolderPattern': '{yyyy}', 'sprintFilePattern': 'ROADMAP.md'}):
            with self.subTest(overrides=overrides):
                self.write_config(**overrides)
                result, transport = self.approve_and_fetch()
                self.assertFalse(result['ok'])
                self.assertEqual(transport.calls, [])
                self.assertNotIn('syncBoard', result)

    def test_two_sprints_on_the_same_file_are_refused(self):
        self.write_config(weekFolderPattern='{yyyy}/fixa', sprintFilePattern='SPRINT.md')
        result, _ = self.approve_and_fetch(state=self.next_sprint_state())
        self.assertFalse(result['ok'])
        self.assertIn('same', result['message'])


class OverLimitTests(RoadmapSyncTestCase):
    def over_limit_state(self):
        state = state_fixture()
        items = state['sprints'][0]['items']
        items.append(dict(items[2], id='5a5a5a5a-0000-4000-8000-00000000000f', position=4, title='Quarto item'))
        items.append(dict(items[2], id='5a5a5a5a-0000-4000-8000-00000000000a', position=5, title='Exportar em CSV',
                          overLimit=True, pendingChangeIds=[ADD_ID]))
        return state

    def test_an_over_limit_item_is_listed_as_left_out_and_not_written(self):
        result, _ = self.approve_and_fetch(state=self.over_limit_state())
        self.assertTrue(result['ok'], result['message'])
        plan = result['plan']
        current = next(s for s in plan['sprints'] if s['sprintId'] == 'sprint-2026-09-28')
        self.assertEqual([i['position'] for i in current['items']], [1, 2, 3, 4])
        self.assertEqual([(i['position'], i['title']) for i in current['outOfLimit']], [(5, 'Exportar em CSV')])
        self.assertNotIn(ADD_ID, plan['targets'][CURRENT]['changeIds'])
        self.assertEqual(plan['targets'][CURRENT]['outOfLimitChangeIds'], [ADD_ID])
        self.assertEqual(next(c for c in plan['changes'] if c['id'] == ADD_ID)['sprintTargets'], [])
        self.assertIn('outside the sprint limit', result['message'])

    def test_a_marker_for_an_over_limit_change_in_that_sprint_file_is_refused(self):
        result, _ = self.approve_and_fetch(state=self.over_limit_state())
        plan = result['plan']
        self.draft(plan)
        staged = Path(plan['targets'][CURRENT]['staged'])
        marker = next(c for c in plan['changes'] if c['id'] == ADD_ID)['marker']
        staged.write_text(staged.read_text(encoding='utf-8') + f'\nFora do limite. {marker}\n', encoding='utf-8')
        applied = self.run_op('apply')
        self.assertFalse(applied['ok'])
        self.assertIn('limit', applied['message'])
        self.assertEqual(applied['written'], [])

    def test_the_service_limit_wins_over_the_configuration(self):
        state = state_fixture()
        state['maxSprintItems'] = 3
        result, _ = self.approve_and_fetch(state=state)
        self.assertEqual(result['plan']['maxSprintItems'], 3)


if __name__ == '__main__':
    unittest.main()
