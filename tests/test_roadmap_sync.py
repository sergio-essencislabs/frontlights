import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import roadmap_sync as rs

FIXTURE = Path(__file__).resolve().parent / 'fixtures' / 'roadmap-sync' / 'pending-changes.json'
MONDAY = dt.date(2026, 9, 28)
SECRET = 'fixture-secret-value'


class Transport:
    """Records every request and answers from a queue of (status, body)."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append({'method': method, 'url': url, 'headers': headers, 'body': body})
        status, body_out = self.answers.pop(0) if self.answers else (200, '{"acked": 1}')
        return status, body_out if isinstance(body_out, str) else json.dumps(body_out)


def fixture():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))


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

    def approve_and_fetch(self, payload=None, **kwargs):
        self.assertTrue(self.run_op('approve')['ok'])
        transport = Transport((200, payload if payload is not None else fixture()))
        result = self.run_op('fetch', transport, **kwargs)
        return result, transport

    def draft(self, plan, declined=(), skip=()):
        """What the session does in step 4: append prose and paste each marker from the plan."""
        for name in ('roadmap', 'sprint'):
            staged = Path(plan['targets'][name]['staged'])
            # Bytes, not text mode: write_text turns a CRLF draft into LF on Linux.
            text = staged.read_bytes().decode('utf-8')
            newline = '\r\n' if '\r\n' in text else '\n'
            added = ''
            for change in plan['changes']:
                if change['id'] in skip or change['alreadyApplied']:
                    continue
                if name == 'roadmap' or change['action'] == 'move_lane':
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
        call = transport.calls[0]
        self.assertEqual(call['method'], 'GET')
        self.assertEqual(call['url'], 'https://roads.example.test/api/frontlights/pending-changes')
        self.assertEqual(call['headers']['Authorization'], f'Bearer {SECRET}')
        plan = result['plan']
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
                result = self.run_op('fetch', Transport((status, '')))
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
        again = self.run_op('fetch', Transport((200, fixture())))
        self.assertFalse(again['ok'])
        self.assertIn('drafted prose', again['message'])
        self.assertIn('Rascunho.', staged.read_text(encoding='utf-8'))
        discarded = self.run_op('fetch', Transport((200, fixture())), discard_staged=True)
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
        second = self.run_op('fetch', Transport((200, fixture())))
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
        second = self.run_op('fetch', Transport((200, payload)))
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
        staged = Path(result['plan']['targets']['sprint']['staged'])
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


if __name__ == '__main__':
    unittest.main()
