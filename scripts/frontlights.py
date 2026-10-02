"""Frontlights pilot: deterministic planning/evidence helpers, never a shell agent.

Only inspect performs network reads. No command here publishes issues, executes
tests, grants Claude permissions, or changes a product repository.
"""
import argparse
import datetime as dt
import fnmatch
import hashlib
import itertools
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import urllib.request
import urllib.parse


def require(condition, message):
    if not condition:
        raise ValueError(message)


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def strings(value):
    return isinstance(value, list) and bool(value) and all(nonempty(x) for x in value)


def number(value):
    return type(value) is int and value > 0


def repository(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', value)


def relative(value):
    require(nonempty(value), 'ownership must name a path')
    value = value.replace('\\', '/')
    require(not PurePosixPath(value).is_absolute() and ':' not in value and
            all(p not in ('..', '.', '') for p in value.split('/')),
            'ownership must be a normalized relative path')
    require(value == '*' or not any(c in value for c in '*?['),
            'use concrete ownership paths or * for unknown ownership')
    return value.casefold().rstrip('/')


def overlap(a, b):
    a, b = relative(a), relative(b)
    return a == '*' or b == '*' or a == b or a.startswith(b + '/') or b.startswith(a + '/')


def conflicts(a, b):
    return any(overlap(x, y) for x in a['ownership'] for y in b['ownership'])


def repository_or_local(value):
    """None means a local project without a remote, which is a supported state."""
    return value is None or repository(value)


def board(project, repo=...):
    """The issue board every created issue must join, or None when none is configured.

    `fields` maps a board field to its default value; None means the value is chosen per
    issue in the approved plan. The board is data for gh, never a place to embed credentials.
    """
    if project is None:
        return None
    require(isinstance(project, dict), 'project must be an object or null')
    require(repo is not None, 'project board requires a GitHub repository; a local project has none')
    owner, num = project.get('owner'), project.get('number')
    require(isinstance(owner, str) and re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})', owner),
            'project.owner must be a GitHub user or organization login')
    require(number(num), 'project.number must be a positive integer')
    fields = project.get('fields', {})
    require(isinstance(fields, dict) and all(nonempty(k) and (v is None or nonempty(v))
            for k, v in fields.items()), 'project.fields must map field names to a value or null')
    for key in ('assignee', 'issue_type'):
        require(project.get(key) is None or nonempty(project.get(key)),
                f'project.{key} must be a non-empty string or null')
    return {'owner': owner, 'number': num, 'fields': dict(fields),
            'assignee': project.get('assignee'), 'issue_type': project.get('issue_type')}


def board_fields(project, issue):
    """Board values for one issue: defaults plus the per-issue choices, with nothing left open.

    A sub-issue (`parent` set) joins the board only through its parent: it takes no values.
    """
    if issue.get('parent') is not None:
        require('project_fields' not in issue,
                f'{issue["id"]}: sub-issue não leva project_fields; entra no quadro só pelo pai')
        return {}
    chosen = issue.get('project_fields', {})
    require(isinstance(chosen, dict) and all(nonempty(k) and nonempty(v) for k, v in chosen.items()),
            f'{issue["id"]}: project_fields must map field names to non-empty values')
    unknown = sorted(set(chosen) - set(project['fields']))
    require(not unknown, f'{issue["id"]}: project_fields not on the board: {", ".join(unknown)}')
    values = {k: v for k, v in project['fields'].items() if v is not None}
    values.update(chosen)
    missing = sorted(k for k in project['fields'] if k not in values)
    require(not missing, f'{issue["id"]}: choose board field(s) {", ".join(missing)} in the plan')
    issue_type = issue.get('issue_type') or project['issue_type']
    require(nonempty(issue_type), f'{issue["id"]}: issue_type required by the board (plan or project default)')
    return values


def validate_plan(plan, project=None):
    require(isinstance(plan, dict) and 'repository' in plan and repository_or_local(plan['repository']),
            'repository must be owner/name, or null for a local project')
    project = board(project, plan['repository'])
    issues = plan.get('issues')
    require(isinstance(issues, list) and 0 < len(issues) <= 24,
            'plan must contain 1..24 session-sized issues; split larger batches')
    ids = set()
    for issue in issues:
        require(isinstance(issue, dict) and number(issue.get('id')), 'positive integer issue id required')
        require(issue['id'] not in ids, 'duplicate issue id')
        ids.add(issue['id'])
        for field in ('title', 'outcome', 'vertical_check'):
            require(nonempty(issue.get(field)), f'{issue["id"]}: missing {field}')
        for field in ('acceptance', 'tests', 'ownership', 'non_goals', 'risks'):
            require(strings(issue.get(field)), f'{issue["id"]}: missing {field}')
        for path in issue['ownership']:
            relative(path)
        require(issue.get('session_sized') is True, 'oversized issue must be decomposed')
        deps = issue.get('dependencies')
        require(isinstance(deps, list) and all(number(x) for x in deps) and len(deps) == len(set(deps)),
                'dependencies must be unique integer ids')
        require(issue.get('status') in {'ready', 'running', 'blocked', 'verified', 'proposed'}, 'invalid status')
        if issue.get('parent') is not None:
            require(number(issue['parent']),
                    f'{issue["id"]}: parent precisa ser o id (inteiro positivo) de uma issue deste plano')
            require(issue['parent'] != issue['id'], f'{issue["id"]}: parent não pode ser a própria issue')
        if project is not None:
            board_fields(project, issue)
        if issue.get('url'):
            require(plan['repository'] is not None, 'a local plan cannot reference GitHub issue URLs')
            require(issue['url'] == f'https://github.com/{plan["repository"]}/issues/{issue["id"]}',
                    'issue URL does not match repository/id')
    graph = {i['id']: i['dependencies'] for i in issues}
    visited, visiting = set(), set()
    def visit(node):
        require(node in graph, f'missing dependency {node}')
        require(node not in visiting, 'circular dependencies')
        if node in visited:
            return
        visiting.add(node)
        for dep in graph[node]:
            visit(dep)
        visiting.remove(node)
        visited.add(node)
    for node in graph:
        visit(node)
    sub_issues(issues)
    return plan


SUB_ISSUES_MAX = 100   # GitHub: sub-issues per parent
SUB_ISSUES_DEPTH = 8   # GitHub: levels of nesting below a top-level issue


def sub_issues(issues):
    """Parent links must stay inside the plan and within GitHub's sub-issue limits.

    `parent` names another issue of this same plan, so the check needs no network.
    Only the plan's children are counted; the children a parent already has on
    GitHub are checked before publishing (`subIssuesSummary.total`).
    """
    by_id = {i['id']: i for i in issues}
    children = {}
    for i in issues:
        parent = i.get('parent')
        if parent is None:
            continue
        require(parent in by_id, f'{i["id"]}: parent {parent} não está no plano; inclua a issue de origem no plano')
        children[parent] = children.get(parent, 0) + 1
    for parent, count in children.items():
        require(count <= SUB_ISSUES_MAX,
                f'{parent}: {count} filhas passam do limite de {SUB_ISSUES_MAX} sub-issues por pai do GitHub')
    for i in issues:
        seen, node, depth = {i['id']}, i, 0
        while node.get('parent') is not None:
            require(node['parent'] not in seen, f'{i["id"]}: ciclo em parent')
            seen.add(node['parent'])
            node, depth = by_id[node['parent']], depth + 1
        require(depth <= SUB_ISSUES_DEPTH,
                f'{i["id"]}: {depth} níveis de sub-issue passam do limite de {SUB_ISSUES_DEPTH} do GitHub')


def schedule(plan, limit):
    """Maximum-cardinality non-conflicting ready wave, deterministic tie-breaking."""
    validate_plan(plan)
    require(number(limit) and limit <= 24, 'concurrency must be 1..24')
    issues = plan['issues']
    active = [i for i in issues if i['status'] == 'running']
    require(len(active) <= limit, 'running issues exceed authorization')
    require(not any(conflicts(a, b) for a, b in itertools.combinations(active, 2)),
            'running issues have overlapping ownership')
    completed = {i['id'] for i in issues if i['status'] == 'verified'}
    require(all(set(i['dependencies']) <= completed for i in active),
            'running issue has an unverified dependency')
    ready = [i for i in issues if i['status'] == 'ready' and
             set(i['dependencies']) <= completed and not any(conflicts(i, a) for a in active)]
    # Bounded at 24 issues. Search larger subsets first so a broad first issue
    # cannot serialize independent narrow slices. No process launch is implied.
    for size in range(min(limit - len(active), len(ready)), 0, -1):
        for group in itertools.combinations(ready, size):
            if not any(conflicts(a, b) for a, b in itertools.combinations(group, 2)):
                return [i['id'] for i in group]
    return []


def context_action(used, reserve):
    if used is None:
        return 'stop'
    require(type(used) is int and used >= 0 and number(reserve), 'invalid context accounting')
    if used + reserve >= 150000:
        return 'stop'
    if used + reserve >= 100000:
        return 'handoff'
    return 'continue'


def authorize(charter, operation):
    """Check an intended structured operation. This does NOT grant permissions."""
    require(isinstance(charter, dict) and isinstance(operation, dict), 'objects required')
    require('repository' in charter and repository_or_local(charter['repository']), 'invalid charter repository')
    for field in ('approved_by', 'approval_reference'):
        require(nonempty(charter.get(field)), f'missing human {field}')
    expiry = dt.datetime.fromisoformat(charter.get('expires_at', ''))
    require(expiry.tzinfo is not None and expiry > dt.datetime.now(dt.timezone.utc), 'authorization expired')
    require(number(charter.get('concurrency')), 'invalid concurrency')
    require(strings(charter.get('stop_conditions')), 'stop conditions required')
    monitor = charter.get('monitoring', {})
    require(monitor.get('mode') in {'phone', 'local', 'alternative'} and
            nonempty(monitor.get('confirmed_by')), 'monitoring arrangement unconfirmed')
    if monitor['mode'] == 'alternative':
        require(nonempty(monitor.get('details')), 'alternative monitoring needs details')
    if monitor['mode'] == 'phone':
        require(monitor.get('phone_connected') is True, 'physical phone not confirmed')
    require('repository' in operation and operation['repository'] == charter['repository'],
            'unapproved repository')
    require(number(operation.get('issue')) and operation['issue'] in charter.get('issue_ids', []),
            'unapproved issue')
    kind = operation.get('kind')
    require(kind in {'edit', 'test', 'checkpoint', 'draft_pr', 'issue_update'} and
            kind in charter.get('operations', []), 'operation outside bounded authorization')
    if kind in {'draft_pr', 'issue_update'}:
        raise ValueError('external write requires native permission; this pilot cannot securely scope generic tools')
    worktree = Path(operation.get('worktree', '')).resolve(strict=True)
    matches = [w for w in charter.get('worktrees', []) if w.get('issue') == operation['issue'] and
               Path(w['path']).resolve(strict=True) == worktree]
    require(len(matches) == 1, 'unapproved or ambiguous worktree')
    selected = matches[0]
    branch = operation.get('branch')
    extra = charter.get('protected_branches', [])
    require(isinstance(extra, list) and all(nonempty(p) and p.strip('/') for p in extra),
            'invalid protected_branches')
    # A trailing slash is malformed for Git; normalize it rather than let it match nothing.
    protected = {p.casefold().rstrip('/') for p in {'main', 'master'} | set(extra)}
    # The prefix may not sit inside a protected name or trailing-* namespace.
    namespaces = {p.rstrip('*').rstrip('/') for p in protected} - {''}
    prefix = charter.get('branch_prefix', 'claude/')
    stem = prefix.rstrip('/').casefold() if isinstance(prefix, str) else ''
    require(nonempty(prefix) and prefix.endswith('/') and nonempty(stem) and
            not any(stem == n or stem.startswith(n + '/') for n in namespaces), 'invalid branch_prefix')
    # Each protected entry p also protects everything below it (p/*), so
    # release/*/hotfix, */hotfix and claude/issue-1 cover their descendants
    # without blocking sibling branches.
    folded = branch.casefold() if nonempty(branch) else ''
    require(nonempty(branch) and branch == selected.get('branch') and branch.startswith(prefix) and
            not any(fnmatch.fnmatchcase(folded, p) or fnmatch.fnmatchcase(folded, p + '/*')
                    for p in protected), 'protected or unapproved branch')
    if kind == 'test':
        argv = operation.get('argv')
        require(strings(argv) and argv in charter.get('verification_commands', []), 'unapproved verification argv')
    else:
        require(nonempty(operation.get('path')), 'target path required')
        target = Path(operation['path'])
        if not target.is_absolute():
            target = worktree / target
        target = target.resolve()
        require(target.is_relative_to(worktree), 'path escapes worktree')
        rel = target.relative_to(worktree).as_posix()
        parts = {p.casefold() for p in target.relative_to(worktree).parts}
        forbidden = {'.git', '.claude', '.codex', '.agents', '.frontlights', '.workflows', 'claude.md', 'agents.md'}
        require(not (parts & forbidden), 'policy and control files require human review')
        require(any(relative(p) != '*' and
                    (relative(rel) == relative(p) or relative(rel).startswith(relative(p) + '/'))
                    for p in selected.get('ownership', [])), 'path outside owned scope')
    return {'scope_check': 'pass', 'permission_granted': False,
            'note': 'Verify actual Git branch/remotes and native permissions before acting.'}


def run(argv, cwd=None):
    result = subprocess.run(argv, cwd=cwd, capture_output=True, timeout=45)
    require(result.returncode == 0, f'{argv[0]} read failed (exit {result.returncode}); inspect credentials/configuration locally')
    return result.stdout


def git_evidence(root):
    root = Path(root).resolve(strict=True)
    def git(*args):
        return run(['git', '-C', str(root), *args])
    head = git('rev-parse', 'HEAD').decode().strip()
    branch = git('branch', '--show-current').decode().strip()
    diff = git('diff', '--binary', '--no-ext-diff', '--no-textconv', 'HEAD', '--', '.', ':!.frontlights')
    names = git('ls-files', '-z', '--cached', '--others', '--exclude-standard').split(b'\0')
    files = {}
    for raw in sorted(set(names)):
        if not raw:
            continue
        name = os.fsdecode(raw)
        if name.replace('\\', '/').startswith('.frontlights/'):
            continue
        path = root / name
        require(path.resolve().is_relative_to(root), 'tracked/untracked path escapes repository')
        if path.is_symlink():
            files[name] = hashlib.sha256(os.readlink(path).encode()).hexdigest()
        elif path.is_file():
            files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            files[name] = 'missing-or-submodule'
    return {'head': head, 'branch': branch,
            'diff_sha256': hashlib.sha256(diff).hexdigest(),
            'files_sha256': hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest(),
            'files': files}


def drift(snapshot, current):
    return [key for key in ('number', 'title', 'body', 'state', 'labels', 'assignees', 'updated_at')
            if snapshot.get(key) != current.get(key)]


def checkpoint(root, issue, handoff, next_step):
    root, handoff = Path(root).resolve(strict=True), Path(handoff).resolve()
    require(handoff.is_file(), 'durable handoff file required')
    require(number(issue.get('number')) and nonempty(issue.get('body')) and
            (nonempty(issue.get('html_url')) or issue.get('source') == 'local'),
            'current canonical GitHub issue snapshot, or a local issue with source "local", required')
    require(nonempty(next_step), 'next concrete step required')
    return {'version': 1, 'created_at': dt.datetime.now(dt.timezone.utc).isoformat(),
            'root': str(root), 'issue': issue, 'git': git_evidence(root),
            'handoff_sha256': hashlib.sha256(handoff.read_bytes()).hexdigest(),
            'next_step': next_step}


def resume(root, saved, current_issue, handoff):
    require(saved.get('version') == 1, 'unsupported checkpoint version')
    require(Path(root).resolve(strict=True) == Path(saved['root']).resolve(strict=True),
            'checkpoint belongs to a different worktree')
    require(saved['issue'].get('number') == current_issue.get('number') and
            saved['issue'].get('html_url') == current_issue.get('html_url') and
            saved['issue'].get('source') == current_issue.get('source'),
            'checkpoint belongs to a different issue/repository')
    live = git_evidence(root)
    changed = drift(saved['issue'], current_issue)
    git_changed = [k for k in ('head', 'branch', 'diff_sha256', 'files_sha256') if saved['git'].get(k) != live.get(k)]
    handoff_changed = hashlib.sha256(Path(handoff).read_bytes()).hexdigest() != saved['handoff_sha256']
    return {'state': 'reconcile' if changed or git_changed or handoff_changed else 'unchanged',
            'issue_drift': changed, 'git_drift': git_changed, 'handoff_changed': handoff_changed,
            'next_step': saved['next_step'], 'required': 'Read current GitHub issue, handoff and diff before continuing.'}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('RoadS redirect refused; configure the final approved endpoint')


LONG_PATH_LIMIT = 160
SHORT_BASE_ADVICE = ('Git worktrees under this root may exceed the Windows path limit; propose a short '
                     'worktree base such as C:/wt/<project> and ask before using it.')


def path_risk(given, resolved, platform=None):
    """Flag roots where `git worktree add` can fail on Windows ('$GIT_DIR' too big).

    Packaged desktop apps may redirect AppData into a much longer
    `Packages/<app>/LocalCache` path that Git sees but the user never typed.
    """
    platform = platform or sys.platform
    given_n, resolved_n = (str(p).replace('\\', '/').rstrip('/') for p in (given, resolved))
    result = {'status': 'ok', 'length': max(len(given_n), len(resolved_n))}
    if not platform.startswith('win'):
        return result
    lowered = resolved_n.casefold()
    if given_n.casefold() != lowered and '/packages/' in lowered and '/localcache/' in lowered:
        result.update(status='virtualized', resolved=resolved_n, advice=SHORT_BASE_ADVICE)
    elif result['length'] > LONG_PATH_LIMIT:
        result.update(status='long_path', advice=SHORT_BASE_ADVICE)
    return result


def verification_candidates(root):
    """Candidate argv from repository files. Candidates, never approved commands."""
    root = Path(root)
    pyproject = root / 'pyproject.toml'
    if (root / 'pytest.ini').is_file() or (
            pyproject.is_file() and '[tool.pytest' in pyproject.read_text(encoding='utf-8', errors='replace')):
        return [['python', '-m', 'pytest']]
    if (root / 'tests').is_dir() and any((root / 'tests').glob('test*.py')):
        return [['python', '-m', 'unittest', 'discover', '-s', 'tests', '-v']]
    return []


def inspect(config, root):
    # A missing key is a typo, not a local project: only an explicit null disables GitHub.
    require('repository' in config and repository_or_local(config['repository']),
            'repository must be owner/name, or null for a local project')
    repo = config['repository']
    result = {'repository': repo, 'fetched_at': dt.datetime.now(dt.timezone.utc).isoformat(),
              'sources': {}, 'verification_commands': {},
              'verification_candidates': verification_candidates(root),
              'path_risk': path_risk(Path(root).absolute(), os.path.realpath(root))}
    if repo is None:
        result['sources']['github'] = {'status': 'unconfigured'}
    else:
        try:
            raw = run(['gh', 'api', '--paginate', '--slurp', f'repos/{repo}/issues?state=all&per_page=100'])
            pages = json.loads(raw)
            require(isinstance(pages, list) and all(isinstance(page, list) and
                    all(isinstance(i, dict) and number(i.get('number')) for i in page)
                    for page in pages), 'malformed GitHub paginated response')
            result['sources']['github'] = {'status': 'available', 'source': f'https://github.com/{repo}/issues',
                                           'issues': [i for page in pages for i in page if 'pull_request' not in i]}
        except (ValueError, OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
            result['sources']['github'] = {'status': 'unavailable', 'reason': 'GitHub read failed; check gh authentication and repository access'}
    project = board(config.get('project'), repo)
    if project is None:
        result['sources']['project'] = {'status': 'unconfigured'}
    else:
        summary = {'owner': project['owner'], 'number': project['number'],
                   'defaults': {k: v for k, v in project['fields'].items() if v is not None},
                   'choose_per_issue': sorted(k for k, v in project['fields'].items() if v is None),
                   'assignee': project['assignee'], 'issue_type': project['issue_type']}
        try:
            view = json.loads(run(['gh', 'project', 'view', str(project['number']),
                                   '--owner', project['owner'], '--format', 'json']))
            require(isinstance(view, dict) and nonempty(view.get('title')), 'malformed project response')
            result['sources']['project'] = {'status': 'available', 'title': view['title'],
                                            'url': view.get('url'), **summary}
        except (ValueError, OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
            result['sources']['project'] = {'status': 'unavailable', **summary,
                                            'reason': 'project board read failed; check gh "project" scope and board access'}
    roads = config.get('roads')
    if not roads:
        result['sources']['roads'] = {'status': 'unconfigured'}
    else:
        try:
            url = roads['observations_url']
            parsed = urllib.parse.urlsplit(url)
            require(parsed.scheme == 'https' and parsed.hostname and not parsed.username and
                    not parsed.password and not parsed.query and not parsed.fragment,
                    'RoadS requires HTTPS endpoint without credentials/query/fragment')
            token = os.environ.get(roads.get('token_env', ''))
            require(token, 'RoadS token unavailable')
            request = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}', 'Accept': 'application/json'})
            with urllib.request.build_opener(NoRedirect).open(request, timeout=20) as response:
                raw = response.read(2_000_001)
            require(len(raw) <= 2_000_000, 'RoadS payload exceeds pilot limit')
            result['sources']['roads'] = {'status': 'available', 'source': url, 'data': json.loads(raw)}
        except (ValueError, OSError, KeyError):
            result['sources']['roads'] = {'status': 'unavailable', 'reason': 'RoadS read failed; verify endpoint contract and token locally'}
    package = Path(root) / 'package.json'
    if package.exists():
        result['verification_commands'] = json.loads(package.read_text(encoding='utf-8')).get('scripts', {})
    return result


POWER_SETTINGS = {
    'lock_display_timeout': ('7516b95f-f776-4464-8c53-06167f40cc99',
                             '8EC4B3A5-6868-48c2-BE75-4F3044BE88A7'),
    'lid_close_action': ('4f971e89-eebd-4455-a8de-9e59040e7347',
                         '5ca83367-6e45-459f-a27b-476b1d01c936'),
}
HELP_TOKENS = {'--help', '-h', '-?', '/?', '--version', 'help', 'version'}


def power_value(text):
    """Parse one powercfg /qh block. An absent index stays None, never 0.

    powercfg /q omits these settings entirely because they are hidden, so a
    missing index must read as unknown; treating it as 0 would silently claim
    the safe value on a machine that was never configured.
    """
    found = {'ac': None, 'dc': None}
    for line in (text or '').splitlines():
        match = re.search(r'\b(AC|DC)\b[^:]*:\s*(0x[0-9a-fA-F]+|\d+)\s*$', line, re.IGNORECASE)
        if match:
            key = match.group(1).lower()
        else:
            # Localized powercfg translates the words but keeps the index. Match
            # ASCII-only stems: the console codepage mangles accented characters
            # into replacement chars, which no \w class would match.
            match = re.search(r'(Altern|Cont)[^:]*:\s*(0x[0-9a-fA-F]+|\d+)\s*$', line)
            if not match:
                continue
            key = 'ac' if match.group(1) == 'Altern' else 'dc'
        raw = match.group(2)
        if found[key] is None:
            found[key] = int(raw, 16) if raw.lower().startswith('0x') else int(raw)
    return found


def host_candidates(rows, since=None):
    """Select processes that look like a Remote Control host.

    Matches the tokenized command line, not the image name: on Windows the CLI
    frequently runs under node.exe, so an image-name filter both misses real
    hosts and matches a bare `remote-control --help`.
    """
    selected = []
    for row in rows or []:
        command = row.get('CommandLine') or row.get('command') or ''
        tokens = [token.strip('"\'') for token in command.split()]
        # `claude rc` is the short alias of `claude remote-control`.
        if 'remote-control' not in tokens and not (
                'rc' in tokens and any('claude' in token.lower() for token in tokens)):
            continue
        if any(token.lower() in HELP_TOKENS for token in tokens):
            continue
        created = row.get('CreationDate') or row.get('created')
        started, start = moment(created), moment(since)
        selected.append({'pid': row.get('ProcessId') or row.get('pid'),
                         'created': created,
                         'predates_session': bool(started and start and started < start)})
    return selected


def moment(value):
    """Parse ISO 8601 or the `/Date(ms)/` that PowerShell 5.1 emits; None when unknown."""
    if not isinstance(value, str) or not value:
        return None
    match = re.fullmatch(r'/Date\((-?\d+)\)/', value)
    try:
        if match:
            return dt.datetime.fromtimestamp(int(match.group(1)) / 1000, dt.timezone.utc)
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (ValueError, OverflowError, OSError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def monitoring_record(root):
    """Read `.frontlights/monitoring.json` here or, from a linked worktree, in the main one."""
    places = [Path(root)]
    try:
        common = run(['git', 'rev-parse', '--path-format=absolute', '--git-common-dir'], cwd=root)
        places.append(Path(common.decode('utf-8', 'replace').strip()).parent)
    except (ValueError, OSError, subprocess.TimeoutExpired):
        pass
    for place in places:
        try:
            record = load(place / '.frontlights' / 'monitoring.json')
        except (OSError, ValueError):
            continue
        if isinstance(record, dict):
            return record
    return None


def known_host(record, candidates):
    """Match a running host to the one the user confirmed on the phone.

    Same process id, and a process started no later than the confirmation: a
    restarted host, or a reused id, gets a new start time and falls back to asking.
    """
    if not isinstance(record, dict) or record.get('mode') != 'phone':
        return None
    confirmed = moment((record.get('observed_phone_confirmation') or {}).get('at'))
    if not confirmed:
        return None
    for candidate in candidates:
        started = moment(candidate.get('created'))
        if candidate.get('pid') == record.get('host_process_id') and started and started <= confirmed:
            return {'pid': candidate['pid'], 'host_name': record.get('host_name'),
                    'confirmed_at': record['observed_phone_confirmation']['at'],
                    'source': '.frontlights/monitoring.json'}
    return None


def monitoring(root, since=None):
    """Read-only monitoring preflight. Proves absence, never a connected phone."""
    result = {'checked_at': dt.datetime.now(dt.timezone.utc).isoformat(),
              'root': str(root), 'phone_connected': None,
              'authority': 'user confirmation in this session',
              'requirement': 'terminal window stays open; lid open until the closed-lid test passes',
              'power': {}, 'host': {}}
    if not sys.platform.startswith('win'):
        unsupported = {'status': 'unsupported', 'platform': sys.platform}
        result['power'], result['host'] = dict(unsupported), dict(unsupported)
        return result
    for name, (subgroup, setting) in POWER_SETTINGS.items():
        argv = ['powercfg', '/qh', 'SCHEME_CURRENT', subgroup, setting]
        try:
            values = power_value(run(argv).decode('utf-8', 'replace'))
            require(values['ac'] is not None or values['dc'] is not None, 'no power index reported')
            result['power'][name] = {'status': 'available', 'source': ' '.join(argv), **values}
        except (ValueError, OSError, subprocess.TimeoutExpired):
            result['power'][name] = {'status': 'unavailable', 'source': ' '.join(argv),
                                     'reason': 'powercfg read failed; run the command locally'}
    argv = ['powershell', '-NoProfile', '-NonInteractive', '-Command',
            'Get-CimInstance Win32_Process | Select-Object ProcessId,CommandLine,CreationDate '
            '| ConvertTo-Json -Compress']
    try:
        rows = json.loads(run(argv).decode('utf-8', 'replace') or 'null')
        require(isinstance(rows, (list, dict)), 'malformed process listing')
        found = host_candidates(rows if isinstance(rows, list) else [rows], since)
        known = known_host(monitoring_record(root), found)
        result['host'] = {'status': 'available', 'candidates': found, 'known': known,
                          'conclusion': 'no persistent host' if not found
                                        else 'confirmed host still running' if known
                                        else 'a process, not a connected phone'}
    except (ValueError, OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        result['host'] = {'status': 'unavailable',
                          'reason': 'process listing failed; absence cannot be concluded'}
    return result


PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def version_key(value):
    """`1.2.3` as a comparable tuple; None for anything else, never a guess."""
    if not isinstance(value, str) or not re.fullmatch(r'\d+(\.\d+)*', value.strip()):
        return None
    return tuple(int(part) for part in value.strip().split('.'))


def marketplace_repo(source):
    """owner/name of a GitHub-hosted marketplace source, or None."""
    if not isinstance(source, dict):
        return None
    if source.get('source') == 'github' and repository(source.get('repo')):
        return source['repo']
    match = re.fullmatch(r'(?:https://|git@)github\.com[/:]([^/\s]+/[^/\s]+?)(?:\.git)?/?',
                         str(source.get('url') or ''))
    return match.group(1) if source.get('source') == 'git' and match else None


def fetch_text(url):
    request = urllib.request.Request(url, headers={'Accept': 'application/vnd.github.raw+json',
                                                   'User-Agent': 'frontlights-update-check'})
    with urllib.request.urlopen(request, timeout=10) as response:
        raw = response.read(1_000_001)
    require(len(raw) <= 1_000_000, 'published manifest exceeds limit')
    return raw.decode('utf-8-sig')


def update_check(plugin_root=PLUGIN_ROOT, config_dir=None):
    """Compare the running plugin's version with the one published on GitHub.

    The desktop Update button compares against the local marketplace clone,
    which a third-party marketplace refreshes only on demand, so the published
    manifest is read from the source itself. Read only; it never updates.
    """
    plugin = load(Path(plugin_root) / '.claude-plugin' / 'plugin.json')
    market_name = load(Path(plugin_root) / '.claude-plugin' / 'marketplace.json')['name']
    result = {'checked_at': dt.datetime.now(dt.timezone.utc).isoformat(),
              'plugin': plugin['name'], 'marketplace': market_name,
              'installed': plugin.get('version'), 'published': None}
    config_dir = Path(config_dir or os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude')
    try:
        known = load(config_dir / 'plugins' / 'known_marketplaces.json')
        repo = marketplace_repo((known.get(market_name) or {}).get('source'))
        require(repo, 'marketplace is not hosted on GitHub')
        # The contents API, not raw.githubusercontent.com: raw caches for minutes
        # and would hide a version pushed just before the session.
        url = f'https://api.github.com/repos/{repo}/contents/.claude-plugin/marketplace.json'
        result['source'] = url
        entry = next(p for p in json.loads(fetch_text(url))['plugins'] if p.get('name') == plugin['name'])
        result['published'] = entry.get('version')
    except (ValueError, OSError, KeyError, TypeError, StopIteration, AttributeError):
        result['status'] = 'unavailable'
        result['reason'] = 'published version could not be read; check the network and the marketplace source'
        return result
    installed, published = version_key(result['installed']), version_key(result['published'])
    if installed is None or published is None:
        result['status'] = 'unavailable'
        result['reason'] = 'version is not numeric'
    elif published > installed:
        result['status'] = 'update_available'
        result['commands'] = [f'claude plugin marketplace update {market_name}',
                              f'claude plugin update {plugin["name"]}@{market_name}']
    else:
        result['status'] = 'current'
    return result


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('validate-plan', 'schedule'):
        p = sub.add_parser(name)
        p.add_argument('--plan', required=True)
        if name == 'schedule':
            p.add_argument('--limit', type=int, required=True)
        else:
            p.add_argument('--config', help='.frontlights/config.json; enforces its project board fields')
    p = sub.add_parser('authorize')
    p.add_argument('--charter', required=True)
    p.add_argument('--operation', required=True)
    p = sub.add_parser('context')
    p.add_argument('--used', type=int)
    p.add_argument('--reserve', type=int, required=True)
    p = sub.add_parser('inspect')
    p.add_argument('--config', required=True)
    p.add_argument('--root', default='.')
    p = sub.add_parser('evidence')
    p.add_argument('--root', default='.')
    p = sub.add_parser('monitoring')
    p.add_argument('--root', default='.')
    p.add_argument('--since', help='ISO 8601 session start; marks older hosts')
    sub.add_parser('update-check')
    for name in ('checkpoint', 'resume'):
        p = sub.add_parser(name)
        p.add_argument('--root', required=True)
        p.add_argument('--issue', required=True, help='Current gh api issue JSON')
        p.add_argument('--handoff', required=True)
        if name == 'checkpoint':
            p.add_argument('--next-step', required=True)
        else:
            p.add_argument('--checkpoint', required=True)
    p = sub.add_parser('drift')
    p.add_argument('--snapshot', required=True)
    p.add_argument('--current', required=True)
    args = parser.parse_args()
    try:
        if args.command == 'validate-plan':
            project = load(args.config).get('project') if args.config else None
            validate_plan(load(args.plan), project)
            result = {'valid': True, 'semantic_review_required': True}
        elif args.command == 'schedule':
            result = {'start': schedule(load(args.plan), args.limit)}
        elif args.command == 'authorize':
            result = authorize(load(args.charter), load(args.operation))
        elif args.command == 'context':
            result = {'action': context_action(args.used, args.reserve)}
        elif args.command == 'inspect':
            result = inspect(load(args.config), args.root)
        elif args.command == 'evidence':
            result = git_evidence(args.root)
        elif args.command == 'monitoring':
            result = monitoring(args.root, args.since)
        elif args.command == 'update-check':
            result = update_check()
        elif args.command == 'checkpoint':
            result = checkpoint(args.root, load(args.issue), args.handoff, args.next_step)
        elif args.command == 'resume':
            result = resume(args.root, load(args.checkpoint), load(args.issue), args.handoff)
        else:
            result = {'discrepancies': drift(load(args.snapshot), load(args.current))}
        print(json.dumps(result, indent=2, ensure_ascii=True))
        return 0
    except (ValueError, OSError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({'error': str(exc)}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
