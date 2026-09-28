import copy
import json
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import frontlights

ROOT = Path(__file__).resolve().parents[1]


class ReleaseManifestTests(unittest.TestCase):
    """The desktop Update button needs a version it can compare; both manifests must agree."""

    def test_marketplace_entry_declares_the_plugin_json_version(self):
        plugin = json.loads((ROOT / '.claude-plugin' / 'plugin.json').read_text(encoding='utf-8'))
        market = json.loads((ROOT / '.claude-plugin' / 'marketplace.json').read_text(encoding='utf-8'))
        entry = next(p for p in market['plugins'] if p['name'] == plugin['name'])
        self.assertEqual(entry.get('version'), plugin['version'])

    def test_single_hidden_skill_whose_stage_references_exist(self):
        skills = sorted(p.name for p in (ROOT / 'skills').iterdir() if p.is_dir())
        self.assertEqual(skills, ['frontlights'])
        text = (ROOT / 'skills' / 'frontlights' / 'SKILL.md').read_text(encoding='utf-8')
        self.assertIn('user-invocable: false', text.split('---')[1])
        for name in ('grilling', 'prd', 'issues', 'development', 'remote-control', 'roadmap-sync'):
            self.assertIn(f'references/{name}.md', text)
            self.assertTrue((ROOT / 'skills' / 'frontlights' / 'references' / f'{name}.md').is_file())

    def test_every_session_asks_about_the_roadmap_before_the_request(self):
        text = (ROOT / 'skills' / 'frontlights' / 'SKILL.md').read_text(encoding='utf-8')
        stage1 = text.split('## 1.')[1].split('## 2.')[0]
        self.assertIn('every session', stage1)
        self.assertIn('Atualizar a sprint e o roadmap de acordo com o RoadS?', stage1)
        self.assertLess(stage1.index('roadmap_sync.py'), stage1.index('Then choose the lightest path'))

    def test_example_config_carries_only_generic_placeholders(self):
        example = json.loads((ROOT / 'examples' / 'config.json').read_text(encoding='utf-8'))
        sync = example['roadmapSync']
        self.assertRegex(sync['secretEnvVar'], r'^FRONTLIGHTS_[A-Z0-9_]+$')
        self.assertIn('example', sync['endpoint'])
        self.assertTrue(all(t['repository'] == 'OWNER/REPOSITORY' for t in sync['issueTargets'].values()))
        self.assertNotIn('guardian', json.dumps(example).lower())

    def test_work_is_named_by_issue_never_by_retired_gt_ids(self):
        skill = ROOT / 'skills' / 'frontlights'
        text = (skill / 'SKILL.md').read_text(encoding='utf-8')
        self.assertIn('.frontlights/issues/<n>/', text)
        self.assertIn('Never mint `GT-NNNN`', text)
        for path in (skill / 'references').glob('*.md'):
            with self.subTest(path=path.name):
                self.assertNotRegex(path.read_text(encoding='utf-8'), r'\bGTs?\b')


def issue(number, paths, dependencies=()):
    return dict(id=number, title=f'Deliver outcome {number}',
                outcome='User can save and retrieve an observation',
                acceptance=['Saved observation appears after reload'],
                tests=['Save through API then fetch and assert the value'],
                ownership=paths, dependencies=list(dependencies),
                vertical_check='Save in UI, reload, observe persisted value',
                non_goals=['Bulk import'], risks=['Concurrent updates'],
                session_sized=True, status='ready',
                url=f'https://github.com/example/pilot/issues/{number}')


def plan():
    return {'repository': 'example/pilot', 'issues': [
        issue(1, ['src/save']), issue(2, ['src/export']),
        issue(3, ['src/save/controller.py'], [1])]}


def charter(root):
    return dict(repository='example/pilot', issue_ids=[1, 2], concurrency=2,
                approved_by='human', approval_reference='session:turn-12',
                expires_at='2099-01-01T00:00:00+00:00',
                monitoring={'mode': 'local', 'confirmed_by': 'human'},
                protected_branches=['main', 'master', 'production'],
                operations=['edit', 'test', 'checkpoint', 'draft_pr', 'issue_update'],
                worktrees=[{'issue': 1, 'path': str(root),
                            'branch': 'claude/issue-1', 'ownership': ['src']}],
                verification_commands=[['python', '-m', 'unittest']],
                stop_conditions=['New product decision', 'Permission prompt'])


class PlanningTests(unittest.TestCase):
    def test_valid_vertical_plan(self):
        frontlights.validate_plan(plan())

    def test_reject_cycle_missing_dependency_duplicate(self):
        for mutation in ('cycle', 'missing', 'duplicate'):
            p = plan()
            if mutation == 'cycle':
                p['issues'][0]['dependencies'] = [3]
            elif mutation == 'missing':
                p['issues'][0]['dependencies'] = [99]
            else:
                p['issues'].append(p['issues'][0])
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                frontlights.validate_plan(p)

    def test_horizontal_and_oversized_rejected(self):
        for field, value in [('vertical_check', ''), ('session_sized', False),
                             ('ownership', ['../elsewhere']), ('acceptance', [])]:
            p = plan()
            p['issues'][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                frontlights.validate_plan(p)

    def test_refill_respects_dependencies_and_overlap(self):
        p = plan()
        self.assertEqual(frontlights.schedule(p, 2), [1, 2])
        p['issues'][0]['status'] = 'running'
        self.assertEqual(frontlights.schedule(p, 2), [2])
        p['issues'][0]['status'] = 'verified'
        self.assertEqual(frontlights.schedule(p, 2), [2, 3])
        p['issues'][1]['status'] = 'blocked'
        self.assertEqual(frontlights.schedule(p, 2), [3])

    def test_parent_case_and_unknown_ownership_conflict(self):
        for paths in (['SRC'], ['src/save'], ['*']):
            p = plan()
            p['issues'][1]['ownership'] = paths
            self.assertEqual(frontlights.schedule(p, 2), [1])

    def test_selects_maximum_safe_ready_set(self):
        p = plan()
        p['issues'] = [issue(1, ['src']), issue(2, ['src/a']), issue(3, ['src/b'])]
        self.assertEqual(frontlights.schedule(p, 2), [2, 3])

    def test_running_conflict_is_error(self):
        p = plan()
        p['issues'][1]['ownership'] = ['src']
        for i in p['issues'][:2]:
            i['status'] = 'running'
        with self.assertRaises(ValueError):
            frontlights.schedule(p, 2)

    def test_running_dependency_violation_is_error(self):
        p = plan()
        p['issues'][2]['status'] = 'running'
        with self.assertRaisesRegex(ValueError, 'dependency'):
            frontlights.schedule(p, 2)


class AuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'src').mkdir()
        self.c = charter(self.root)
        self.op = dict(repository='example/pilot', issue=1, kind='edit',
                       branch='claude/issue-1', worktree=str(self.root),
                       path=str(self.root / 'src' / 'app.py'))

    def test_positive_local_edit(self):
        frontlights.authorize(self.c, self.op)

    def test_forbidden_operations_repositories_issues_branches(self):
        changes = [('kind', x) for x in ['merge', 'deploy', 'release', 'delete',
                   'shell', 'road_ack', 'push', 'issue_close']]
        changes += [('repository', 'attacker/other'), ('issue', 9),
                    ('branch', 'main'), ('branch', 'production'),
                    ('branch', 'claude/unapproved'), ('issue', True)]
        for key, value in changes:
            op = dict(self.op, **{key: value})
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                frontlights.authorize(self.c, op)

    def test_escape_and_policy_files_denied(self):
        for path in ['../escape', 'src/../../escape', '.git/config',
                     '.frontlights/authorization.json', 'CLAUDE.md', 'other/file']:
            op = dict(self.op, path=str(self.root / path))
            with self.subTest(path=path), self.assertRaises(ValueError):
                frontlights.authorize(self.c, op)

    def test_expired_unapproved_or_unmonitored_denied(self):
        for field, value in [('expires_at', '2020-01-01T00:00:00+00:00'),
                             ('approval_reference', ''), ('monitoring', {}),
                             ('concurrency', 0), ('approved_by', '')]:
            c = dict(self.c, **{field: value})
            with self.subTest(field=field), self.assertRaises(ValueError):
                frontlights.authorize(c, self.op)

    def test_exact_argv_no_shell_syntax(self):
        op = dict(self.op, kind='test', argv=['python', '-m', 'unittest'])
        frontlights.authorize(self.c, op)
        for argv in [['python', '-c', 'print(1)'], ['python -m unittest; git push'],
                     ['python', '-m', 'unittest', ';', 'gh', 'pr', 'merge']]:
            with self.assertRaises(ValueError):
                frontlights.authorize(self.c, dict(op, argv=argv))

    def test_external_writes_always_need_native_permission(self):
        for kind in ['draft_pr', 'issue_update']:
            with self.assertRaisesRegex(ValueError, 'external'):
                frontlights.authorize(self.c, dict(self.op, kind=kind))


class EvidenceTests(unittest.TestCase):
    def test_checkpoint_rejects_missing_handoff_and_detects_drift(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            handoff = root / 'handoff.md'
            snapshot = {'number': 1, 'title': 'Save', 'body': 'Accept A', 'state': 'OPEN',
                        'html_url': 'https://github.com/example/pilot/issues/1'}
            evidence = {'head': 'a' * 40, 'branch': 'codex/issue-1',
                        'diff_sha256': 'a', 'files_sha256': 'b', 'files': {}}
            with patch.object(frontlights, 'git_evidence', return_value=evidence):
                with self.assertRaises(ValueError):
                    frontlights.checkpoint(root, snapshot, handoff, 'Run focused tests')
                handoff.write_text('Acceptance: save persists. Next: run focused tests.')
                checkpoint = frontlights.checkpoint(root, snapshot, handoff, 'Run focused tests')
                self.assertEqual(frontlights.resume(root, checkpoint, snapshot, handoff)['state'], 'unchanged')
                changed = dict(snapshot, body='Accept B')
                self.assertEqual(frontlights.resume(root, checkpoint, changed, handoff)['state'], 'reconcile')
                handoff.write_text('Changed next action')
                self.assertEqual(frontlights.resume(root, checkpoint, snapshot, handoff)['state'], 'reconcile')

    def test_context_budget_fail_closed_and_reserves(self):
        self.assertEqual(frontlights.context_action(80000, 10000), 'continue')
        self.assertEqual(frontlights.context_action(99000, 1000), 'handoff')
        self.assertEqual(frontlights.context_action(140000, 10000), 'stop')
        self.assertEqual(frontlights.context_action(None, 1000), 'stop')
        with self.assertRaises(ValueError):
            frontlights.context_action(-1, 100)

    def test_snapshot_drift_includes_acceptance_body_and_status(self):
        a = {'number': 1, 'title': 'Save', 'body': 'Accept A', 'state': 'OPEN'}
        self.assertEqual(frontlights.drift(a, dict(a)), [])
        self.assertEqual(frontlights.drift(a, dict(a, body='Accept B')), ['body'])

    def test_checkpoint_binds_git_head_and_dirty_content(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            def git(*args):
                return subprocess.run(['git', '-C', d, *args], check=True,
                                      capture_output=True, text=True)
            git('init', '-b', 'codex/fixture')
            git('config', 'user.email', 'fixture@example.invalid')
            git('config', 'user.name', 'Fixture')
            (root / 'app.txt').write_text('one')
            git('add', '.')
            git('commit', '-m', 'fixture')
            a = frontlights.git_evidence(root)
            (root / 'app.txt').write_text('two')
            b = frontlights.git_evidence(root)
            self.assertEqual(a['head'], b['head'])
            self.assertNotEqual(a['diff_sha256'], b['diff_sha256'])
            (root / 'new.txt').write_text('untracked')
            c = frontlights.git_evidence(root)
            self.assertNotEqual(b['files_sha256'], c['files_sha256'])

    def test_cli_invalid_json_exits_nonzero_without_traceback(self):
        result = subprocess.run([sys.executable, str(Path(frontlights.__file__)),
                                 'schedule', '--plan', 'missing.json', '--limit', '2'],
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('Traceback', result.stderr)


class IntegrationReadTests(unittest.TestCase):
    def test_malformed_github_payload_is_unavailable(self):
        with tempfile.TemporaryDirectory() as root, patch.object(frontlights, 'run', return_value=b'{"message":"error"}'):
            result = frontlights.inspect({'repository': 'example/pilot'}, root)
        self.assertEqual(result['sources']['github']['status'], 'unavailable')

    def test_github_pagination_filters_prs_roads_unconfigured(self):
        raw = json.dumps([[{'number': 1}], [{'number': 2, 'pull_request': {}}],
                          [{'number': 3}]]).encode()
        with tempfile.TemporaryDirectory() as root, patch.object(frontlights, 'run', return_value=raw) as run:
            result = frontlights.inspect({'repository': 'example/pilot'}, root)
        self.assertEqual([i['number'] for i in result['sources']['github']['issues']], [1, 3])
        self.assertIn('--paginate', run.call_args.args[0])
        self.assertEqual(result['sources']['roads']['status'], 'unconfigured')

    def test_unavailable_integrations_do_not_invent_data_or_echo_tokens(self):
        with tempfile.TemporaryDirectory() as root, patch.object(frontlights, 'run', side_effect=ValueError('private-secret')):
            result = frontlights.inspect({'repository': 'example/pilot', 'roads': {
                'observations_url': 'http://unsafe.example', 'token_env': 'MISSING_TOKEN'}}, root)
        self.assertEqual(result['sources']['github']['status'], 'unavailable')
        self.assertEqual(result['sources']['roads']['status'], 'unavailable')
        self.assertNotIn('private-secret', json.dumps(result))


def board(**overrides):
    project = {'owner': 'example-org', 'number': 3, 'assignee': '@me', 'issue_type': 'Task',
               'fields': {'Status': 'Open', 'Area': None}}
    project.update(overrides)
    return project


class ProjectBoardTests(unittest.TestCase):
    """Issues belong on the configured board; a board that is set must never be skipped."""

    def test_inspect_reports_board_unconfigured_when_absent_or_null(self):
        for config in ({'repository': 'example/pilot'}, {'repository': 'example/pilot', 'project': None}):
            with self.subTest(config=config), tempfile.TemporaryDirectory() as root, \
                    patch.object(frontlights, 'run', return_value=b'[]'):
                result = frontlights.inspect(config, root)
            self.assertEqual(result['sources']['project']['status'], 'unconfigured')

    def test_inspect_reads_the_configured_board(self):
        def fake(argv, cwd=None):
            if argv[:3] == ['gh', 'project', 'view']:
                return json.dumps({'title': 'Board', 'url': 'https://github.com/orgs/example-org/projects/3'}).encode()
            return b'[]'
        with tempfile.TemporaryDirectory() as root, patch.object(frontlights, 'run', side_effect=fake) as run:
            result = frontlights.inspect({'repository': 'example/pilot', 'project': board()}, root)
        project = result['sources']['project']
        self.assertEqual(project['status'], 'available')
        self.assertEqual((project['owner'], project['number']), ('example-org', 3))
        self.assertEqual(project['defaults'], {'Status': 'Open'})
        self.assertEqual(project['choose_per_issue'], ['Area'])
        self.assertIn(['gh', 'project', 'view', '3', '--owner', 'example-org', '--format', 'json'],
                      [c.args[0] for c in run.call_args_list])

    def test_inspect_reports_an_unreachable_board_without_inventing_it(self):
        def fake(argv, cwd=None):
            if argv[:3] == ['gh', 'project', 'view']:
                raise ValueError('missing project scope private-token')
            return b'[]'
        with tempfile.TemporaryDirectory() as root, patch.object(frontlights, 'run', side_effect=fake):
            result = frontlights.inspect({'repository': 'example/pilot', 'project': board()}, root)
        self.assertEqual(result['sources']['project']['status'], 'unavailable')
        self.assertNotIn('private-token', json.dumps(result))

    def test_inspect_rejects_a_malformed_board(self):
        for bad in (board(owner='bad owner'), board(number=0), board(number=True), board(number='3'),
                    board(fields={'Status': 7}), board(fields=[]), board(assignee=''), 'example-org/3'):
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as root, \
                    patch.object(frontlights, 'run', return_value=b'[]'), self.assertRaisesRegex(ValueError, 'project'):
                frontlights.inspect({'repository': 'example/pilot', 'project': bad}, root)

    def test_a_local_project_cannot_have_a_board(self):
        with tempfile.TemporaryDirectory() as root, self.assertRaisesRegex(ValueError, 'project'):
            frontlights.inspect({'repository': None, 'project': board()}, root)

    def test_plan_must_choose_every_open_board_field_per_issue(self):
        p = plan()
        with self.assertRaisesRegex(ValueError, 'Area'):
            frontlights.validate_plan(p, board())
        for i in p['issues']:
            i['project_fields'] = {'Area': 'Backend'}
        checked = frontlights.validate_plan(p, board())
        self.assertEqual(checked['issues'][0]['project_fields'], {'Area': 'Backend'})

    def test_plan_issue_type_comes_from_issue_or_board_default(self):
        p = plan()
        for i in p['issues']:
            i['project_fields'] = {'Area': 'Backend'}
        frontlights.validate_plan(p, board())
        with self.assertRaisesRegex(ValueError, 'issue_type'):
            frontlights.validate_plan(p, board(issue_type=None))
        for i in p['issues']:
            i['issue_type'] = 'Bug'
        frontlights.validate_plan(p, board(issue_type=None))

    def test_plan_rejects_malformed_or_unknown_board_fields(self):
        for bad in ({'Area': ''}, {'Area': 3}, {'Area': 'Backend', 'Typo': 'x'}, ['Area']):
            p = plan()
            for i in p['issues']:
                i['project_fields'] = bad
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                frontlights.validate_plan(p, board())

    def test_plan_without_board_ignores_nothing_and_still_validates(self):
        frontlights.validate_plan(plan())

    def test_cli_validate_plan_applies_the_board_from_config(self):
        with tempfile.TemporaryDirectory() as d:
            plan_path, config_path = Path(d) / 'plan.json', Path(d) / 'config.json'
            plan_path.write_text(json.dumps(plan()), encoding='utf-8')
            config_path.write_text(json.dumps({'repository': 'example/pilot', 'project': board()}), encoding='utf-8')
            result = subprocess.run([sys.executable, str(Path(frontlights.__file__)), 'validate-plan',
                                     '--plan', str(plan_path), '--config', str(config_path)],
                                    capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Area', result.stdout + result.stderr)
        self.assertNotIn('Traceback', result.stderr)


class LocalModeTests(unittest.TestCase):
    """A project without a remote is a supported state, not a missing prerequisite."""

    def test_inspect_without_repository_reports_github_unconfigured(self):
        with tempfile.TemporaryDirectory() as root, patch.object(frontlights, 'run') as run:
            result = frontlights.inspect({'repository': None, 'roads': None}, root)
        run.assert_not_called()
        self.assertIsNone(result['repository'])
        self.assertEqual(result['sources']['github']['status'], 'unconfigured')
        self.assertEqual(result['sources']['roads']['status'], 'unconfigured')

    def test_inspect_still_rejects_a_malformed_or_missing_repository(self):
        for config in ({'repository': 'not a repository'}, {'roads': None}, {'repsitory': None}):
            with self.subTest(config=config), tempfile.TemporaryDirectory() as root, \
                    self.assertRaisesRegex(ValueError, 'repository'):
                frontlights.inspect(config, root)

    def test_resume_rejects_a_source_change_alone(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            handoff = root / 'handoff.md'
            handoff.write_text('Next.')
            local = {'number': 1, 'body': 'Accept A', 'source': 'local'}
            evidence = {'head': 'a', 'branch': 'claude/i', 'diff_sha256': 'a', 'files_sha256': 'b', 'files': {}}
            with patch.object(frontlights, 'git_evidence', return_value=evidence):
                saved = frontlights.checkpoint(root, local, handoff, 'Next')
                with self.assertRaisesRegex(ValueError, 'different issue'):
                    frontlights.resume(root, saved, dict(local, source=None), handoff)

    def test_local_plan_validates_without_urls(self):
        p = plan()
        p['repository'] = None
        for i in p['issues']:
            i['url'] = None
        frontlights.validate_plan(p)
        p['issues'][0]['url'] = 'https://github.com/example/pilot/issues/1'
        with self.assertRaisesRegex(ValueError, 'local plan'):
            frontlights.validate_plan(p)

    def test_local_issue_snapshot_checkpoints_and_resumes(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            handoff = root / 'handoff.md'
            handoff.write_text('Next: run focused tests.')
            local = {'number': 1, 'title': 'Save', 'body': 'Accept A', 'state': 'open', 'source': 'local'}
            evidence = {'head': 'a' * 40, 'branch': 'claude/issue-1',
                        'diff_sha256': 'a', 'files_sha256': 'b', 'files': {}}
            with patch.object(frontlights, 'git_evidence', return_value=evidence):
                saved = frontlights.checkpoint(root, local, handoff, 'Run focused tests')
                self.assertEqual(frontlights.resume(root, saved, local, handoff)['state'], 'unchanged')
                with self.assertRaises(ValueError):
                    frontlights.checkpoint(root, dict(local, source='guess'), handoff, 'Run focused tests')
                remote = dict(local, source=None, html_url='https://github.com/example/pilot/issues/1')
                with self.assertRaisesRegex(ValueError, 'different issue'):
                    frontlights.resume(root, saved, remote, handoff)


class LocalAuthorizationTests(unittest.TestCase):
    def test_local_charter_needs_an_explicit_local_operation(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, 'src').mkdir()
            c = dict(charter(root), repository=None)
            op = dict(repository=None, issue=1, kind='edit', branch='claude/issue-1',
                      worktree=root, path=str(Path(root) / 'src' / 'a.py'))
            frontlights.authorize(c, op)
            missing = {k: v for k, v in op.items() if k != 'repository'}
            for bad in (missing, dict(op, repository='example/pilot')):
                with self.subTest(op=bad), self.assertRaisesRegex(ValueError, 'repository'):
                    frontlights.authorize(c, bad)


class BranchPrefixTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'src').mkdir()

    def operation(self, branch):
        return dict(repository='example/pilot', issue=1, kind='edit', branch=branch,
                    worktree=str(self.root), path=str(self.root / 'src' / 'app.py'))

    def charter_for(self, branch, **extra):
        c = charter(self.root)
        c['worktrees'][0]['branch'] = branch
        c.update(extra)
        return c

    def test_default_prefix_is_claude(self):
        frontlights.authorize(self.charter_for('claude/issue-1'), self.operation('claude/issue-1'))
        with self.assertRaisesRegex(ValueError, 'branch'):
            frontlights.authorize(self.charter_for('codex/issue-1'), self.operation('codex/issue-1'))

    def test_prefix_cannot_open_a_protected_namespace(self):
        for protected, prefix, branch in ((['release/*'], 'release/', 'release/1.0'),
                                          (['Release'], 'release/', 'release/1.0'),
                                          (['MAIN'], 'main/', 'main/x')):
            c = self.charter_for(branch, branch_prefix=prefix, protected_branches=protected)
            with self.subTest(prefix=prefix), self.assertRaisesRegex(ValueError, 'branch_prefix'):
                frontlights.authorize(c, self.operation(branch))

    def test_mid_pattern_protection_closes_the_prefix_and_bad_entries_fail_cleanly(self):
        for pattern in ('release/*/hotfix', '*/hotfix', 'rel*/hotfix'):
            c = self.charter_for('release/a/hotfix/x', branch_prefix='release/',
                                 protected_branches=[pattern])
            with self.subTest(pattern=pattern), \
                    self.assertRaisesRegex(ValueError, 'protected or unapproved branch'):
                frontlights.authorize(c, self.operation('release/a/hotfix/x'))
        allowed = self.charter_for('release/1.0', branch_prefix='release/',
                                   protected_branches=['release/*/hotfix'])
        frontlights.authorize(allowed, self.operation('release/1.0'))
        for entries in ([None], [5], 'main'):
            with self.subTest(entries=entries), self.assertRaisesRegex(ValueError, 'protected_branches'):
                frontlights.authorize(self.charter_for('claude/issue-1', protected_branches=entries),
                                   self.operation('claude/issue-1'))

    def test_protected_branch_inside_prefix_is_denied_case_insensitively(self):
        c = self.charter_for('claude/x', protected_branches=['Claude/X'])
        with self.assertRaisesRegex(ValueError, 'protected or unapproved branch'):
            frontlights.authorize(c, self.operation('claude/x'))

    def test_one_protected_branch_does_not_block_its_siblings(self):
        c = self.charter_for('claude/issue-2', protected_branches=['claude/issue-1'])
        frontlights.authorize(c, self.operation('claude/issue-2'))
        for branch in ('claude/issue-1', 'claude/issue-1/sub'):
            trailing = self.charter_for(branch, protected_branches=['claude/issue-1/'])
            with self.subTest(branch=branch), \
                    self.assertRaisesRegex(ValueError, 'protected or unapproved branch'):
                frontlights.authorize(trailing, self.operation(branch))
        below = self.charter_for('claude/issue-1/sub', protected_branches=['claude/issue-1'])
        with self.assertRaisesRegex(ValueError, 'protected or unapproved branch'):
            frontlights.authorize(below, self.operation('claude/issue-1/sub'))

    def test_null_prefix_and_missing_charter_repository_are_denied(self):
        with self.assertRaisesRegex(ValueError, 'branch_prefix'):
            frontlights.authorize(self.charter_for('claude/issue-1', branch_prefix=None),
                               self.operation('claude/issue-1'))
        c = self.charter_for('claude/issue-1')
        del c['repository']
        with self.assertRaisesRegex(ValueError, 'repository'):
            frontlights.authorize(c, self.operation('claude/issue-1'))

    def test_charter_prefix_is_honoured_and_validated(self):
        c = self.charter_for('codex/issue-1', branch_prefix='codex/')
        frontlights.authorize(c, self.operation('codex/issue-1'))
        for prefix in ('', 'codex', '/', 'main/', 5):
            with self.subTest(prefix=prefix), self.assertRaisesRegex(ValueError, 'branch_prefix'):
                frontlights.authorize(self.charter_for('codex/issue-1', branch_prefix=prefix),
                                   self.operation('codex/issue-1'))


class VerificationDiscoveryTests(unittest.TestCase):
    def inspect(self, files):
        with tempfile.TemporaryDirectory() as d:
            for name, text in files.items():
                path = Path(d, name)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding='utf-8')
            return frontlights.inspect({'repository': None}, d)

    def test_python_unittest_and_pytest_candidates(self):
        self.assertEqual(self.inspect({'tests/test_a.py': ''})['verification_candidates'],
                         [['python', '-m', 'unittest', 'discover', '-s', 'tests', '-v']])
        self.assertEqual(self.inspect({'pyproject.toml': '[tool.pytest.ini_options]\n',
                                       'tests/test_a.py': ''})['verification_candidates'],
                         [['python', '-m', 'pytest']])
        self.assertEqual(self.inspect({'pytest.ini': '[pytest]\n'})['verification_candidates'],
                         [['python', '-m', 'pytest']])

    def test_npm_scripts_kept_and_nothing_invented(self):
        result = self.inspect({'package.json': '{"scripts": {"test": "vitest"}}'})
        self.assertEqual(result['verification_commands'], {'test': 'vitest'})
        self.assertEqual(result['verification_candidates'], [])
        self.assertEqual(self.inspect({})['verification_candidates'], [])


class PathRiskTests(unittest.TestCase):
    def test_long_or_virtualized_windows_root_is_flagged(self):
        short = frontlights.path_risk('C:/wt/app', 'C:/wt/app', 'win32')
        self.assertEqual(short['status'], 'ok')
        deep = 'C:/Users/u/AppData/Roaming/' + 'x' * 170
        self.assertEqual(frontlights.path_risk(deep, deep, 'win32')['status'], 'long_path')
        virtual = frontlights.path_risk('C:/Users/u/AppData/Roaming/App/s',
                                     'C:/Users/u/AppData/Local/Packages/App_x/LocalCache/Roaming/App/s', 'win32')
        self.assertEqual(virtual['status'], 'virtualized')
        self.assertIn('worktree', virtual['advice'])

    def test_other_platforms_are_not_flagged(self):
        deep = '/home/u/' + 'x' * 300
        self.assertEqual(frontlights.path_risk(deep, deep, 'linux')['status'], 'ok')

    def test_inspect_reports_path_risk(self):
        with tempfile.TemporaryDirectory() as root:
            self.assertIn(frontlights.inspect({'repository': None}, root)['path_risk']['status'],
                          {'ok', 'long_path', 'virtualized'})


QH_ENGLISH = ('    Current AC Power Setting Index: 0x00000000\n'
              '    Current DC Power Setting Index: 0x0000001e\n')
QH_LOCALIZED = ('    \u00cdndice de Configura\u00e7\u00f5es de Correntes Alternadas Atuais: 0x00000000\n'
                '    \u00cdndice de Configura\u00e7\u00f5es de Correntes Cont\ufffdnuas Atuais: 0x00000000\n')
Q_WITHOUT_VALUE = ('GUID do Esquema de Energia: 381b4222 (Equilibrado)\n'
                   '  Subgrupo: SUB_VIDEO\n')


class MonitoringTests(unittest.TestCase):
    def test_power_value_parses_english_and_localized_output(self):
        self.assertEqual(frontlights.power_value(QH_ENGLISH), {'ac': 0, 'dc': 30})
        # The console codepage mangles the accented word; the ASCII stem must still match.
        self.assertEqual(frontlights.power_value(QH_LOCALIZED), {'ac': 0, 'dc': 0})

    def test_absent_power_index_is_unknown_not_zero(self):
        for text in (Q_WITHOUT_VALUE, '', None, 'garbage without any index'):
            with self.subTest(text=text):
                self.assertEqual(frontlights.power_value(text), {'ac': None, 'dc': None})

    def test_host_candidates_match_tokens_not_image_name(self):
        rows = [{'ProcessId': 1, 'CommandLine': 'claude remote-control --name Vision'},
                {'ProcessId': 2, 'CommandLine': 'claude remote-control --help'},
                {'ProcessId': 3, 'CommandLine': 'node C:\\x\\cli.js remote-control --name A'},
                {'ProcessId': 4, 'CommandLine': 'code C:\\src\\remote-control\\readme.md'},
                {'ProcessId': 5, 'CommandLine': 'claude remote-control --version'},
                {'ProcessId': 6, 'CommandLine': None},
                {'ProcessId': 9, 'CommandLine': 'claude rc'},
                {'ProcessId': 10, 'CommandLine': 'node C:\\x\\claude\\cli.js rc --name B'},
                {'ProcessId': 11, 'CommandLine': 'claude rc --help'},
                {'ProcessId': 12, 'CommandLine': 'notepad rc'}]
        self.assertEqual([c['pid'] for c in frontlights.host_candidates(rows)], [1, 3, 9, 10])

    def test_since_marks_hosts_older_than_the_session(self):
        rows = [{'ProcessId': 7, 'CommandLine': 'claude remote-control', 'CreationDate': '2026-09-25T08:00:00'},
                {'ProcessId': 8, 'CommandLine': 'claude remote-control', 'CreationDate': '2026-09-25T12:00:00'}]
        found = frontlights.host_candidates(rows, since='2026-09-25T10:00:00')
        self.assertEqual([(c['pid'], c['predates_session']) for c in found], [(7, True), (8, False)])

    def test_since_compares_powershell_dates_as_times(self):
        rows = [{'ProcessId': 9, 'CommandLine': 'claude remote-control',
                 'CreationDate': '/Date(1790360617247)/'}]  # 2026-09-25T18:23:37Z
        self.assertTrue(frontlights.host_candidates(rows, since='2026-09-25T19:00:00Z')[0]['predates_session'])
        self.assertFalse(frontlights.host_candidates(rows, since='2026-09-25T18:00:00Z')[0]['predates_session'])

    def test_known_host_needs_the_same_process_started_before_confirmation(self):
        record = {'mode': 'phone', 'host_name': 'pc', 'host_process_id': 42,
                  'observed_phone_confirmation': {'at': '2026-09-25T18:25:43Z'}}
        before = {'pid': 42, 'created': '/Date(1790360617247)/'}
        known = frontlights.known_host(record, [before])
        self.assertEqual((known['pid'], known['host_name'], known['confirmed_at']),
                         (42, 'pc', '2026-09-25T18:25:43Z'))
        restarted = {'pid': 42, 'created': '2026-09-26T09:00:00Z'}
        for rec, candidates in ((record, [restarted]), (record, [{'pid': 43, 'created': before['created']}]),
                                (record, [{'pid': 42, 'created': None}]), (record, []),
                                ({**record, 'mode': 'local'}, [before]),
                                ({**record, 'observed_phone_confirmation': {}}, [before]), (None, [before])):
            with self.subTest(record=rec, candidates=candidates):
                self.assertIsNone(frontlights.known_host(rec, candidates))

    def test_monitoring_record_is_found_from_a_linked_worktree(self):
        with tempfile.TemporaryDirectory() as main, tempfile.TemporaryDirectory() as linked:
            Path(main, '.frontlights').mkdir()
            Path(main, '.frontlights', 'monitoring.json').write_text('{"mode": "phone"}', encoding='utf-8')
            common = (str(Path(main, '.git')) + '\n').encode()
            with patch.object(frontlights, 'run', return_value=common):
                self.assertEqual(frontlights.monitoring_record(linked), {'mode': 'phone'})
            with patch.object(frontlights, 'run', side_effect=ValueError('not a repository')):
                self.assertIsNone(frontlights.monitoring_record(linked))

    def test_monitoring_reports_a_known_host_without_claiming_a_phone(self):
        listing = json.dumps([{'ProcessId': 42, 'CommandLine': 'claude remote-control --name pc',
                               'CreationDate': '/Date(1790360617247)/'}]).encode()
        record = {'mode': 'phone', 'host_name': 'pc', 'host_process_id': 42,
                  'observed_phone_confirmation': {'at': '2026-09-25T18:25:43Z'}}

        def fake(argv, cwd=None):
            if argv[0] == 'powershell':
                return listing
            raise ValueError('not needed')
        with patch.object(frontlights.sys, 'platform', 'win32'), \
             patch.object(frontlights, 'run', side_effect=fake), \
             patch.object(frontlights, 'monitoring_record', return_value=record):
            result = frontlights.monitoring('.')
        self.assertEqual(result['host']['known']['pid'], 42)
        self.assertEqual(result['host']['conclusion'], 'confirmed host still running')
        self.assertIsNone(result['phone_connected'])

    def test_monitoring_never_claims_a_phone_and_degrades_per_source(self):
        with patch.object(frontlights.sys, 'platform', 'win32'), \
             patch.object(frontlights, 'run', side_effect=ValueError('powercfg exploded')):
            result = frontlights.monitoring('.')
        self.assertIsNone(result['phone_connected'])
        self.assertEqual(result['authority'], 'user confirmation in this session')
        for name in ('lock_display_timeout', 'lid_close_action'):
            self.assertEqual(result['power'][name]['status'], 'unavailable')
        # Absence must not be concluded when the listing itself failed.
        self.assertEqual(result['host']['status'], 'unavailable')
        self.assertNotIn('powercfg exploded', json.dumps(result))

    def test_monitoring_is_unsupported_off_windows_without_raising(self):
        with patch.object(frontlights.sys, 'platform', 'linux'):
            result = frontlights.monitoring('.')
        self.assertEqual(result['power']['status'], 'unsupported')
        self.assertEqual(result['host']['status'], 'unsupported')
        self.assertIsNone(result['phone_connected'])

    def test_phone_mode_requires_a_real_confirmation(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, 'src').mkdir()
            operation = dict(repository='example/pilot', issue=1, kind='edit',
                             branch='claude/issue-1', worktree=root,
                             path=str(Path(root) / 'src' / 'a.py'))
            good = charter(root)
            good['monitoring'] = {'mode': 'phone', 'confirmed_by': 'human', 'phone_connected': True}
            self.assertEqual(frontlights.authorize(good, operation)['scope_check'], 'pass')
            for value in ({}, {'phone_connected': False}, {'phone_connected': 'true'},
                          {'phone_connected': 1}):
                with self.subTest(value=value):
                    bad = charter(root)
                    bad['monitoring'] = {'mode': 'phone', 'confirmed_by': 'human', **value}
                    with self.assertRaisesRegex(ValueError, 'physical phone not confirmed'):
                        frontlights.authorize(bad, operation)


class UpdateCheckTests(unittest.TestCase):
    """Warn about a newer published version without relying on the stale local clone."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.plugin_root = Path(self.tmp.name, 'plugin')
        self.config_dir = Path(self.tmp.name, 'config')
        Path(self.plugin_root, '.claude-plugin').mkdir(parents=True)
        Path(self.config_dir, 'plugins').mkdir(parents=True)
        self.write(self.plugin_root / '.claude-plugin' / 'plugin.json', {'name': 'pilot', 'version': '0.7.2'})
        self.write(self.plugin_root / '.claude-plugin' / 'marketplace.json', {'name': 'market', 'plugins': []})
        self.known({'source': 'github', 'repo': 'OWNER/REPOSITORY'})

    def write(self, path, data):
        Path(path).write_text(json.dumps(data), encoding='utf-8')

    def known(self, source):
        self.write(self.config_dir / 'plugins' / 'known_marketplaces.json', {'market': {'source': source}})

    def check(self, published=None, error=None):
        body = json.dumps({'name': 'market', 'plugins': [{'name': 'pilot', 'version': published}]})
        fake = patch.object(frontlights, 'fetch_text', side_effect=error, return_value=body)
        with fake as fetch:
            result = frontlights.update_check(self.plugin_root, self.config_dir)
        return result, fetch

    def test_newer_published_version_lists_both_commands(self):
        result, fetch = self.check('0.10.0')
        self.assertEqual(result['status'], 'update_available')
        self.assertEqual(result['commands'], ['claude plugin marketplace update market',
                                              'claude plugin update pilot@market'])
        self.assertEqual(fetch.call_args[0][0], 'https://api.github.com/repos/OWNER/REPOSITORY'
                                                '/contents/.claude-plugin/marketplace.json')

    def test_same_or_older_published_version_is_current(self):
        for published in ('0.7.2', '0.7.1'):
            with self.subTest(published=published):
                result, _ = self.check(published)
                self.assertEqual(result['status'], 'current')
                self.assertNotIn('commands', result)

    def test_git_url_source_resolves_to_the_same_repository(self):
        self.known({'source': 'git', 'url': 'https://github.com/OWNER/REPOSITORY.git'})
        result, fetch = self.check('0.8.0')
        self.assertEqual(result['status'], 'update_available')
        self.assertIn('/repos/OWNER/REPOSITORY/', fetch.call_args[0][0])

    def test_network_failure_or_unknown_source_is_unavailable_not_current(self):
        result, _ = self.check(error=OSError('offline'))
        self.assertEqual(result['status'], 'unavailable')
        self.assertNotIn('offline', json.dumps(result))
        self.known({'source': 'directory', 'path': 'somewhere'})
        result, fetch = self.check('9.9.9')
        self.assertEqual(result['status'], 'unavailable')
        fetch.assert_not_called()

    def test_non_numeric_version_is_never_compared(self):
        result, _ = self.check('latest')
        self.assertEqual(result['status'], 'unavailable')


if __name__ == '__main__':
    unittest.main()
