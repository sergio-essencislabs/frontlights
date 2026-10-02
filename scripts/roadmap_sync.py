#!/usr/bin/env python3
"""Roadmap sync between RoadS and the project's ROADMAP and SPRINT files.

Deterministic helper behind the roadmap-sync route of /frontlights. It owns everything that must
not depend on a model's judgement: the configuration, the credential, the first-use approval, the
HTTP calls, the sprint week, path safety, idempotency markers, backups and the acknowledgement.
The prose that goes into the files is written by the session into staged copies; this helper only
checks and moves those copies into place.

Operations (each prints one JSON object on stdout):
  status          configuration, credential presence, approval, scrumRoot, current week paths and
                  the marker nonce's age. No network, never fails for a missing configuration.
  approve         record the user's approval of the (endpoint URL, secretEnvVar) pair. Run only
                  after the user said yes in the conversation.
  fetch           POST <endpoint>/sync-board (its failure is reported, never fatal), GET
                  <endpoint>/roadmap-state (required: no fallback) and GET <endpoint>/pending-changes,
                  then write the plan and a staged copy of the roadmap and of each sprint file with a
                  pending change (a sprint file that does not exist yet is staged empty).
  apply           move the staged copies into place, re-read, verify every marker, then ack
                  (unless --no-ack).
  ack             verify every marker in the targets, then POST <endpoint>/ack with the plan's asOf.
  rotate-markers  mint a fresh marker nonce and rewrite every marker in the roadmap and the current
                  week's sprint file. No network.

Exit codes: 0 done; 1 nothing was acknowledged (apply validates every target before it writes any,
so a refusal from validation wrote nothing; a missing marker or an unconfirmed decline is raised
after writing and says so, with `written` and `backups`); 2 the files are written and verified but
the acknowledgement did not happen (`retryable` says whether running ack again can fix it).

Markers are `<!-- roads:<id> <tag> -->` (or with a trailing ` declined`). The tag is a truncated
HMAC-SHA256 over the id keyed by a 128-bit nonce kept in `.frontlights/roadmap-sync/marker.json`.
The nonce never leaves the machine and never appears in the files or in the printed plan, so the
roadmap service cannot spell a valid marker whatever text it returns.

The credential is read from the environment only (on Windows also the value saved with `setx`),
sent only in the Authorization header to the approved endpoint, never written, and redacted from
every string this helper prints.
"""

import argparse
import datetime as dt
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

EFFORTS = ('Very High', 'High', 'Medium', 'Low')
ACTIONS = ('add', 'modify', 'remove', 'move_lane')
MAX_BODY_BYTES = 5 * 1024 * 1024
BACKUP_GENERATIONS = 5
SHRINK_RATIO = 0.9
SECRET_ENV_PATTERN = r'^FRONTLIGHTS_[A-Z0-9_]+$'
ID_PATTERN = r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$'
FIELD_LIMITS = {'action': 100, 'title': 400, 'description': 4000, 'produto': 200, 'prioridade': 100,
                'effort': 100, 'lane': 200, 'laneId': 200, 'githubIssueUrl': 500, 'payload': 2000,
                'timezone': 100, 'reason': 100, 'message': 1000, 'error': 1000}
STATE_SCHEMA_VERSION = 1
ITEM_STATUSES = ('open', 'development', 'blocker', 'done', 'none')
SNAPSHOT_MAX_AGE = dt.timedelta(hours=24)
TEMPLATE_LOOKBACK_WEEKS = 26
REPARSE_POINT = 0x400
OFFLINE_MASK = 0x1000 | 0x40000 | 0x400000  # OFFLINE | RECALL_ON_OPEN | RECALL_ON_DATA_ACCESS
MANIFEST = '.staging-origin.json'

_SECRETS = []


class Refusal(Exception):
    """A deliberate refusal: the message is safe to show the user."""


def require(condition, message):
    if not condition:
        raise Refusal(message)


def now_iso():
    return dt.datetime.now().astimezone().isoformat()


def protect(text):
    for secret in _SECRETS:
        if secret:
            text = text.replace(secret, '[redacted]')
    return text


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write_json_atomic(value, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f'.{path.name}.{secrets.token_hex(8)}.tmp'
    try:
        temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding='utf-8')
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def sha256(path):
    path = Path(path)
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------- configuration and context

def main_worktree(root):
    try:
        common = subprocess.run(['git', 'rev-parse', '--path-format=absolute', '--git-common-dir'],
                                cwd=root, capture_output=True, text=True, timeout=20)
        if common.returncode == 0 and common.stdout.strip():
            return Path(common.stdout.strip()).parent
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def config_path(root):
    """`.frontlights/config.json` here or, from a linked worktree, in the main one."""
    for place in (Path(root), main_worktree(root)):
        if place and (place / '.frontlights' / 'config.json').is_file():
            return (place / '.frontlights' / 'config.json').resolve()
    return None


def canonical_endpoint(url):
    parts = urllib.parse.urlsplit(url)
    return f'{parts.scheme.lower()}://{parts.netloc.lower()}{parts.path.rstrip("/")}'


def validate_endpoint(url):
    require(isinstance(url, str) and url, 'roadmapSync.endpoint is required')
    parts = urllib.parse.urlsplit(url)
    local = parts.hostname in ('localhost', '127.0.0.1', '::1')
    require(parts.scheme == 'https' or (parts.scheme == 'http' and local),
            'roadmapSync.endpoint must use https (http only for localhost)')
    require(parts.hostname and not parts.username and not parts.password and not parts.query and not parts.fragment,
            'roadmapSync.endpoint must be a plain URL without credentials, query or fragment')
    return url


def validate_issue_targets(targets):
    require(isinstance(targets, dict), 'roadmapSync.issueTargets must map each produto to its repository')
    for produto, target in targets.items():
        require(isinstance(target, dict) and isinstance(target.get('repository'), str)
                and re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', target['repository']),
                f'roadmapSync.issueTargets[{produto!r}].repository must be owner/name')
        project = target.get('project')
        require(project is None or (isinstance(project, dict) and isinstance(project.get('owner'), str)
                                    and isinstance(project.get('number'), int) and project['number'] > 0),
                f'roadmapSync.issueTargets[{produto!r}].project must be null or {{owner, number}}')
    return targets


def expand_root(value):
    require(isinstance(value, str) and value.strip(), 'roadmapSync.scrumRoot is required')
    return os.path.expanduser(os.path.expandvars(value.strip()))


class Context:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.config_path = config_path(root)
        require(self.config_path, 'No .frontlights/config.json in this project; create it first (see examples/config.json).')
        config = read_json(self.config_path)
        sync = config.get('roadmapSync')
        require(isinstance(sync, dict) and sync.get('enabled') is True,
                'This project does not enable roadmap sync: add a roadmapSync block with "enabled": true to .frontlights/config.json.')
        self.sync = sync
        self.endpoint = validate_endpoint(sync.get('endpoint'))
        self.secret_env = sync.get('secretEnvVar')
        require(isinstance(self.secret_env, str) and re.fullmatch(SECRET_ENV_PATTERN, self.secret_env),
                'roadmapSync.secretEnvVar must match ^FRONTLIGHTS_[A-Z0-9_]+$')
        self.scrum_root = expand_root(sync.get('scrumRoot'))
        for key in ('roadmapFile', 'weekFolderPattern', 'sprintFilePattern'):
            require(isinstance(sync.get(key), str) and sync[key].strip(), f'roadmapSync.{key} is required')
        self.max_sprint_items = sync.get('maxSprintItems', 4)
        require(isinstance(self.max_sprint_items, int) and self.max_sprint_items > 0,
                'roadmapSync.maxSprintItems must be a positive integer')
        self.issue_targets = validate_issue_targets(sync.get('issueTargets') or {})
        self.state_dir = self.config_path.parent / 'roadmap-sync'
        self.plan_path = self.state_dir / 'plan.json'
        self.staging_dir = self.state_dir / 'staging'
        self.approval_path = self.state_dir / 'approval.json'
        self.marker_path = self.state_dir / 'marker.json'
        self.state_path = self.state_dir / 'state.json'
        self.name = str(config.get('repository') or self.config_path.parent.parent.name)


# ---------------------------------------------------------------- credential and approval

def windows_user_env(name):
    if not sys.platform.startswith('win'):
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment') as key:
            value, _ = winreg.QueryValueEx(key, name)
            return value if isinstance(value, str) else None
    except OSError:
        return None


def secret_present(name):
    return bool(os.environ.get(name) or windows_user_env(name))


def read_secret(name):
    """Process environment first, then the Windows user scope, which `setx` writes and which a
    session started before the variable existed cannot see in its own environment."""
    value = os.environ.get(name) or windows_user_env(name)
    require(value, f'Environment variable {name} is not set. Set it once in your own terminal with: '
                   f'setx {name} "<value issued by RoadS>" -- then restart Claude. It is read from the '
                   'environment only and never printed or stored.')
    _SECRETS.append(value)
    return value


def approval_pair(ctx):
    return {'endpoint': canonical_endpoint(ctx.endpoint), 'secretEnvVar': ctx.secret_env}


def approval_state(ctx):
    pair = approval_pair(ctx)
    try:
        stored = read_json(ctx.approval_path)
    except (OSError, ValueError):
        return 'unapproved', pair, None
    if stored.get('endpoint') == pair['endpoint'] and stored.get('secretEnvVar') == pair['secretEnvVar']:
        return 'approved', pair, None
    return 'changed', pair, {'endpoint': stored.get('endpoint'), 'secretEnvVar': stored.get('secretEnvVar')}


def assert_approved(ctx):
    state, pair, _ = approval_state(ctx)
    why = ('the endpoint URL or the secret variable changed since the last approval' if state == 'changed'
           else 'this pair has never been approved')
    require(state == 'approved', f'Roadmap sync is not approved to send {pair["secretEnvVar"]} to {pair["endpoint"]} '
                                 f'({why}). Ask the user; only after an explicit yes run the approve operation.')


# ---------------------------------------------------------------- week and paths

def sprint_week(today):
    """The Monday-Friday week containing today; a weekend belongs to the week just ended."""
    start = today - dt.timedelta(days=today.weekday())
    return start, start + dt.timedelta(days=4)


def expand_pattern(pattern, start, end):
    count = [0]

    def dd_mm(_match):
        count[0] += 1
        return (start if count[0] == 1 else end).strftime('%d_%m')
    text = re.sub(r'\{dd_MM\}', dd_mm, pattern)
    return text.replace('{yyyy}', start.strftime('%Y')).replace('{MM}', start.strftime('%m')).replace('{dd}', start.strftime('%d'))


def sprint_relative(ctx, start, end):
    folder = expand_pattern(ctx.sync['weekFolderPattern'], start, end)
    leaf = expand_pattern(ctx.sync['sprintFilePattern'], start, end)
    return folder.rstrip('/\\') + '/' + leaf


def targets(ctx, today):
    start, end = sprint_week(today)
    return {'week': (start, end), 'roadmap': expand_pattern(ctx.sync['roadmapFile'], start, end),
            'sprint': sprint_relative(ctx, start, end)}


def sprint_template(ctx, start, end):
    """The most recent existing sprint file before this sprint, walking back one week at a time
    with the same length; a new sprint file follows its headings and sections."""
    for weeks in range(1, TEMPLATE_LOOKBACK_WEEKS + 1):
        earlier = start - dt.timedelta(weeks=weeks)
        try:
            path = safe_target(ctx.scrum_root, sprint_relative(ctx, earlier, earlier + (end - start)))
        except Refusal:
            continue
        if os.path.isfile(path):
            return path
    return None


def reparse_tag(path):
    """0 for an ordinary entry, the reparse tag otherwise; -1 when unreadable."""
    try:
        info = os.lstat(path)
    except OSError:
        return -1
    attributes = getattr(info, 'st_file_attributes', 0)
    if attributes & REPARSE_POINT:
        return getattr(info, 'st_reparse_tag', -1)
    return -1 if os.path.islink(path) else 0


def cloud_tag(tag):
    # OneDrive Files On Demand and other sync providers use IO_REPARSE_TAG_CLOUD_*: storage, not
    # redirection, so it is allowed. Every other reparse point is refused.
    return tag > 0 and (tag & 0xFFFF0FFF) == 0x9000001A


def fully_qualified(path):
    if sys.platform.startswith('win'):
        return bool(re.match(r'^(?:[A-Za-z]:[\\/]|[\\/][\\/][^\\/]+[\\/])', path))
    return path.startswith('/')


def safe_target(scrum_root, relative):
    """Resolve a pattern-derived path under scrumRoot and refuse anything that could land a write
    elsewhere: containment after normalisation, Windows' silent trimming of trailing dots and
    spaces, and any junction, symbolic link or non-cloud reparse point between scrumRoot and the
    target. An online-only cloud placeholder is refused: reading it would trigger a download."""
    require(fully_qualified(scrum_root), f'Refusing scrumRoot {scrum_root}: it must be a fully qualified path')
    root = os.path.abspath(scrum_root).rstrip('\\/')
    require(os.path.splitdrive(root)[1].strip('\\/'),
            f'Refusing scrumRoot {scrum_root}: it must name a folder, not the root of a drive or share')
    require(os.path.isdir(root), f'scrumRoot does not exist: {root}')
    for segment in re.split(r'[\\/]', relative):
        require(not re.fullmatch(r'[. ]*', segment) and not re.search(r'[. ]$', segment) and ':' not in segment,
                f"Refusing target path: segment {segment!r} is empty, relative, or ends in a dot or space")
    require(not os.path.isabs(relative), 'Refusing target path: it must be relative to scrumRoot')
    full = os.path.abspath(os.path.join(root, relative))
    require(os.path.normcase(full).startswith(os.path.normcase(root + os.sep)), f'Refusing target path outside scrumRoot: {full}')
    cursor = full
    while True:
        if os.path.lexists(cursor):
            tag = reparse_tag(cursor)
            require(tag == 0 or cloud_tag(tag), f'Refusing target path: {cursor} is a junction, symbolic link or other reparse point')
        if len(cursor) <= len(root):
            break
        parent = os.path.dirname(cursor)
        if parent == cursor:
            break
        cursor = parent
    if os.path.isfile(full):
        attributes = getattr(os.stat(full), 'st_file_attributes', 0)
        require(not attributes & OFFLINE_MASK, f"{full} is an online-only cloud placeholder not available on this device. "
                                               "Open it once or mark it 'Always keep on this device', then sync again.")
    else:
        require(not os.path.exists(full), f'Refusing target path: {full} is a directory')
    return full


def relative_to_root(ctx, path):
    root = os.path.abspath(ctx.scrum_root).rstrip('\\/') + os.sep
    full = os.path.abspath(path)
    require(os.path.normcase(full).startswith(os.path.normcase(root)), "The stored plan points outside scrumRoot; run fetch again.")
    return full[len(root):]


# ---------------------------------------------------------------- markers

def new_nonce():
    return secrets.token_hex(16)


def marker_record(ctx):
    try:
        stored = read_json(ctx.marker_path)
    except (OSError, ValueError):
        return None
    nonce = stored.get('nonce')
    if not isinstance(nonce, str) or not re.fullmatch(r'[0-9a-f]{32}', nonce):
        return None
    return stored


def write_marker_record(ctx, nonce, rotated=False):
    record = {'schemaVersion': 1, 'nonce': nonce, 'createdAt': now_iso()}
    if rotated:
        record['rotatedAt'] = record['createdAt']
    write_json_atomic(record, ctx.marker_path)


def marker_nonce(ctx):
    """The stored nonce, minting one when there is none; the flag says whether it was minted."""
    record = marker_record(ctx)
    if record:
        return record['nonce'], False
    nonce = new_nonce()
    write_marker_record(ctx, nonce)
    return nonce, True


def marker_tag(change_id, nonce, purpose='marker'):
    require(isinstance(nonce, str) and re.fullmatch(r'[0-9a-f]{32}', nonce), 'No usable marker nonce; run fetch again.')
    digest = hmac.new(bytes.fromhex(nonce), f'{purpose}:{change_id}'.encode('utf-8'), hashlib.sha256).digest()
    return digest[:8].hex()


def nonce_id(nonce):
    return marker_tag('nonce', nonce, 'nonce-id')


def marker_text(change_id, nonce, declined=False):
    suffix = ' declined' if declined else ''
    return f'<!-- roads:{change_id} {marker_tag(change_id, nonce)}{suffix} -->'


def marker_pattern(change_id, nonce):
    return r'<!--\s*roads:' + re.escape(change_id) + r'\s+' + marker_tag(change_id, nonce) + r'(?:\s+(declined))?\s*-->'


def marker_inventory(text, nonce):
    """Every marker this nonce vouches for, id -> 'applied' | 'declined'."""
    found = {}
    for match in re.finditer(r'<!--\s*roads:([A-Za-z0-9][A-Za-z0-9_.-]{0,127})\s+([0-9a-f]{16})(?:\s+(declined))?\s*-->', text or ''):
        change_id = match.group(1)
        if match.group(2) == marker_tag(change_id, nonce):
            found.setdefault(change_id, 'declined' if match.group(3) else 'applied')
    return found


def marker_state(text, change_id, nonce):
    match = re.search(marker_pattern(change_id, nonce), text or '')
    if not match:
        return None
    return 'declined' if match.group(1) else 'applied'


# ---------------------------------------------------------------- service data

def safe_string(value, field, limit):
    """Service text is untrusted and lands in files the user keeps and in the model's context:
    an HTML comment sequence refuses the whole batch, `<` and `>` are escaped so no sequence can be
    assembled across fields, control characters and fences are neutralised, and length is capped."""
    if value is None:
        return ''
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
    if not text:
        return ''
    require('<!--' not in text and '-->' not in text,
            f'RoadS returned an HTML comment sequence in {field}; nothing was written or acknowledged')
    text = re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]', ' ', text)
    text = text.replace('<', '&lt;').replace('>', '&gt;')
    text = re.sub(r'`{3,}', '`', text)
    text = re.sub(r'~{3,}', '~', text)
    if len(text) > limit:
        text = text[:limit].rstrip() + ' [truncated]'
    return text


def timestamp(value, field='timestamp'):
    """(raw, parsed) for an ISO 8601 value, or None. The raw spelling is kept, trimmed."""
    if value is None:
        return None
    raw = safe_string(value, field, 100).strip()
    if not raw:
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw.replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return raw, parsed


def issue_target(ctx_targets, produto):
    for name, target in (ctx_targets or {}).items():
        if name.strip().casefold() == (produto or '').strip().casefold():
            return {'produto': name, **target}
    return None


def normalise_changes(payload, issue_targets=None):
    """Defensive normalisation of the pending-changes answer. Unknown fields are dropped. A change
    without a usable id is refused (it could never be marked and the ack would still consume it),
    and so is one without a title unless it is a remove identified by its itemId."""
    require(isinstance(payload, dict) and isinstance(payload.get('changes'), list), 'RoadS answered without a changes array')
    changes, seen = [], set()
    for change in payload['changes']:
        if change is None:
            continue
        require(isinstance(change, dict), 'RoadS returned a change that is not an object; nothing was written or acknowledged')
        raw_id = change.get('id')
        change_id = str(raw_id) if isinstance(raw_id, (str, int)) and not isinstance(raw_id, bool) else ''
        require(re.fullmatch(ID_PATTERN, change_id), 'RoadS returned a change without a usable id; nothing was written or acknowledged')
        require(change_id not in seen, f'RoadS returned change {change_id} twice; nothing was written or acknowledged')
        seen.add(change_id)
        action = safe_string(change.get('action'), f'change {change_id} action', FIELD_LIMITS['action']).strip()
        item = change.get('item')
        require(item is None or isinstance(item, dict), f'RoadS returned change {change_id} with an item that is not an object')
        item = item or {}
        # A remove arrives with item and itemId both null: the queue row is written before the
        # delete and the foreign key is "on delete set null". The removed item's id, lane and title
        # travel in the payload, which also carries github_issue_url on a modify that links an issue.
        payload = change.get('payload') if isinstance(change.get('payload'), dict) else {}

        def safe_id(value):
            text = str(value) if isinstance(value, (str, int)) and not isinstance(value, bool) else None
            return text if text and re.fullmatch(ID_PATTERN, text) else None
        item_id = safe_id(change.get('itemId')) or safe_id(payload.get('item_id'))

        def field(name, payload_key=None):
            value = item.get(name)
            if (value is None or value == '') and payload_key:
                value = payload.get(payload_key)
            return safe_string(value, f'change {change_id} item.{name}', FIELD_LIMITS[name])
        title = field('title', 'title')
        require(title.strip() or (action == 'remove' and item_id),
                f'RoadS returned change {change_id} ({action or "no action"}) without a title; nothing was written or acknowledged. '
                'Either the change has no title or the service renamed the field: check the contract before running again.')
        effort_raw = field('effort')
        effort = next((e for e in EFFORTS if e.casefold() == effort_raw.strip().casefold()), None)
        issue_url = field('githubIssueUrl', 'github_issue_url')
        if not re.fullmatch(r'https?://[^\s<>"]+', issue_url) or re.search(r'&(?:lt|gt);', issue_url):
            issue_url = ''
        created = timestamp(change.get('createdAt'), f'change {change_id} createdAt')
        produto = field('produto')
        changes.append({
            'id': change_id, 'itemId': item_id, 'action': action,
            'knownAction': action in ACTIONS,
            'createdAt': created[0] if created else None,
            'payload': safe_string(change.get('payload'), f'change {change_id} payload', FIELD_LIMITS['payload']) or None,
            'needsIssue': action in ('add', 'modify', 'move_lane') and not issue_url,
            'issueTarget': issue_target(issue_targets, produto),
            'item': {
                'title': title, 'description': field('description'), 'produto': produto,
                'prioridade': field('prioridade'), 'effort': effort, 'effortRaw': effort_raw,
                'githubIssueUrl': issue_url or None, 'lane': field('lane'), 'laneId': field('laneId', 'lane_id') or None},
            'itemMissing': not change.get('item'),
            '_created': created,
        })
    return changes


def as_of(payload, changes):
    """From the server, never the local clock: its asOf, else the newest createdAt returned."""
    server = timestamp(payload.get('asOf'), 'asOf')
    if server:
        return server[0], 'server'
    newest = None
    for change in changes:
        if change['_created'] and (newest is None or change['_created'][1] > newest[1]):
            newest = change['_created']
    return (newest[0], 'max-createdAt') if newest else (None, None)


def _state_refusal(field, what):
    return f'RoadS returned a roadmap-state whose {field} {what}; nothing was written or acknowledged'


def state_id(value, field, optional=False):
    if value is None and optional:
        return None
    text = str(value) if isinstance(value, (str, int)) and not isinstance(value, bool) else ''
    require(re.fullmatch(ID_PATTERN, text), _state_refusal(field, 'is not a usable id'))
    return text


def state_text(value, field, limit, optional=True):
    require(isinstance(value, str) or (optional and value is None), _state_refusal(field, 'is not text'))
    return safe_string(value, f'roadmap-state {field}', limit)


def state_int(value, field, minimum, optional=False):
    if value is None and optional:
        return None
    require(isinstance(value, int) and not isinstance(value, bool) and value >= minimum,
            _state_refusal(field, f'is not an integer of at least {minimum}'))
    return value


def state_bool(value, field):
    require(isinstance(value, bool), _state_refusal(field, 'is not true or false'))
    return value


def state_list(value, field, optional=False):
    if value is None and optional:
        return []
    require(isinstance(value, list), _state_refusal(field, 'is not a list'))
    return value


def state_time(value, field, optional=True):
    if value is None and optional:
        return None
    parsed = timestamp(value, f'roadmap-state {field}') if isinstance(value, str) else None
    require(parsed, _state_refusal(field, 'is not an ISO 8601 instant'))
    return parsed


def state_date(value, field):
    require(isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value), _state_refusal(field, 'is not a YYYY-MM-DD date'))
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise Refusal(_state_refusal(field, 'is not a valid calendar date'))


def state_item(raw, where, in_sprint):
    require(isinstance(raw, dict), _state_refusal(where, 'is not an object'))
    item_id = state_id(raw.get('id'), f'{where}.id')
    where = f'{where} ({item_id})'
    url = state_text(raw.get('githubIssueUrl'), f'{where}.githubIssueUrl', FIELD_LIMITS['githubIssueUrl'])
    if not re.fullmatch(r'https?://[^\s<>"]+', url) or re.search(r'&(?:lt|gt);', url):
        url = ''
    status = raw.get('status')
    require(status in ITEM_STATUSES, _state_refusal(f'{where}.status', 'is not one of ' + '|'.join(ITEM_STATUSES)))
    updated = state_time(raw.get('updatedAt'), f'{where}.updatedAt')
    pending = [state_id(value, f'{where}.pendingChangeIds') for value in state_list(raw.get('pendingChangeIds'), f'{where}.pendingChangeIds')]
    return {
        'id': item_id, 'position': state_int(raw.get('position'), f'{where}.position', 1),
        'title': state_text(raw.get('title'), f'{where}.title', FIELD_LIMITS['title'], optional=False),
        'description': state_text(raw.get('description'), f'{where}.description', FIELD_LIMITS['description']),
        'produto': state_text(raw.get('produto'), f'{where}.produto', FIELD_LIMITS['produto']),
        'prioridade': state_text(raw.get('prioridade'), f'{where}.prioridade', FIELD_LIMITS['prioridade']),
        'effort': state_text(raw.get('effort'), f'{where}.effort', FIELD_LIMITS['effort']),
        'githubIssueUrl': url or None, 'issueNumber': state_int(raw.get('issueNumber'), f'{where}.issueNumber', 1, optional=True),
        'status': status, 'done': state_bool(raw.get('done'), f'{where}.done'),
        # overLimit is the service's decision and exists only on sprint items.
        'overLimit': state_bool(raw.get('overLimit'), f'{where}.overLimit') if in_sprint else False,
        'updatedAt': updated[0] if updated else None, 'pendingChangeIds': pending}


def normalise_state(payload):
    """Defensive reading of GET roadmap-state (schemaVersion 1). An unknown schema version, a
    field of the wrong type, an invalid date or a duplicate sprint refuses the whole answer:
    nothing is guessed, and there is no fallback to the local week."""
    require(isinstance(payload, dict), 'RoadS answered roadmap-state with something that is not an object')
    version = payload.get('schemaVersion')
    require(isinstance(version, int) and not isinstance(version, bool) and version == STATE_SCHEMA_VERSION,
            f'RoadS answered roadmap-state with schemaVersion {version!r}; only {STATE_SCHEMA_VERSION} is understood. '
            'Nothing was written or acknowledged; update Frontlights or take it to the RoadS owner.')
    state_as_of = state_time(payload.get('asOf'), 'asOf', optional=False)
    synced = state_time(payload.get('snapshotSyncedAt'), 'snapshotSyncedAt')
    sprints, seen = [], set()
    for index, raw in enumerate(state_list(payload.get('sprints'), 'sprints')):
        where = f'sprints[{index}]'
        require(isinstance(raw, dict), _state_refusal(where, 'is not an object'))
        sprint_id = state_id(raw.get('sprintId'), f'{where}.sprintId')
        require(sprint_id not in seen, _state_refusal(f'sprint {sprint_id}', 'appears twice'))
        seen.add(sprint_id)
        start, end = state_date(raw.get('startDate'), f'{where}.startDate'), state_date(raw.get('endDate'), f'{where}.endDate')
        require(end >= start, _state_refusal(f'sprint {sprint_id}', 'ends before it starts'))
        items = [state_item(item, f'{where}.items[{n}]', True) for n, item in enumerate(state_list(raw.get('items'), f'{where}.items'))]
        sprints.append({'sprintId': sprint_id, 'laneId': state_text(raw.get('laneId'), f'{where}.laneId', FIELD_LIMITS['laneId']),
                        'title': state_text(raw.get('title'), f'{where}.title', FIELD_LIMITS['title']),
                        'startDate': start.isoformat(), 'endDate': end.isoformat(), '_dates': (start, end),
                        'items': sorted(items, key=lambda i: i['position'])})
    groups = []
    for index, raw in enumerate(state_list(payload.get('groups'), 'groups', optional=True)):
        where = f'groups[{index}]'
        require(isinstance(raw, dict), _state_refusal(where, 'is not an object'))
        groups.append({'laneId': state_text(raw.get('laneId'), f'{where}.laneId', FIELD_LIMITS['laneId']),
                       'title': state_text(raw.get('title'), f'{where}.title', FIELD_LIMITS['title']),
                       'items': [state_item(item, f'{where}.items[{n}]', False)
                                 for n, item in enumerate(state_list(raw.get('items'), f'{where}.items'))]})
    removed = []
    for index, raw in enumerate(state_list(payload.get('removedPending'), 'removedPending', optional=True)):
        where = f'removedPending[{index}]'
        require(isinstance(raw, dict), _state_refusal(where, 'is not an object'))
        removed.append({'changeId': state_id(raw.get('changeId'), f'{where}.changeId'),
                        'itemId': state_id(raw.get('itemId'), f'{where}.itemId', optional=True),
                        'title': state_text(raw.get('title'), f'{where}.title', FIELD_LIMITS['title']),
                        'laneId': state_text(raw.get('laneId'), f'{where}.laneId', FIELD_LIMITS['laneId'])})
    stale = synced is None or state_as_of[1] - synced[1] > SNAPSHOT_MAX_AGE
    return {'asOf': state_as_of[0], 'snapshotSyncedAt': synced[0] if synced else None, 'snapshotStale': stale,
            'maxSprintItems': state_int(payload.get('maxSprintItems'), 'maxSprintItems', 1, optional=True),
            'timezone': state_text(payload.get('timezone'), 'timezone', FIELD_LIMITS['timezone']),
            'sprints': sprints, 'groups': groups, 'removedPending': removed}


def board_result(text):
    """The sync-board answer, reduced to what the user is told. Never raises: a board sync that
    failed is reported, and the user decides whether to go on with the last state."""
    try:
        answer = json.loads(text)
        require(isinstance(answer, dict) and isinstance(answer.get('ok'), bool), 'not the contract JSON')
        if not answer['ok']:
            reason = answer.get('reason')
            return {'ok': False, 'ran': False,
                    'reason': safe_string(reason, 'sync-board reason', FIELD_LIMITS['reason']) if isinstance(reason, str) and reason else 'error',
                    'message': safe_string(answer.get('message'), 'sync-board message', FIELD_LIMITS['message'])}
        roadmap = answer.get('roadmap') if isinstance(answer.get('roadmap'), dict) else {}

        def count(name):
            value = roadmap.get(name)
            return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
        synced = timestamp(answer.get('syncedAt'), 'sync-board syncedAt') if isinstance(answer.get('syncedAt'), str) else None
        result = {'ok': True, 'ran': answer.get('ran') is True, 'syncedAt': synced[0] if synced else None,
                  'added': count('added'), 'removed': count('removed'), 'issuesCreated': count('issuesCreated')}
        if roadmap.get('error'):
            result['error'] = safe_string(roadmap['error'], 'sync-board roadmap.error', FIELD_LIMITS['error'])
        return result
    except (ValueError, Refusal):
        return {'ok': False, 'ran': False, 'reason': 'invalid_response',
                'message': 'RoadS answered sync-board outside the contract (not JSON, another shape, or an HTML comment sequence)'}


# ---------------------------------------------------------------- transport

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http_default(method, url, headers, body):
    """Redirects are never followed, so the Authorization header cannot reach another host; a 3xx
    comes back as a status and is refused. Certificates are verified; the response is capped."""
    data = body.encode('utf-8') if body is not None else None
    request = urllib.request.Request(url, data=data, method=method,
                                     headers={**headers, 'Accept': 'application/json', 'User-Agent': 'frontlights-roadmap-sync',
                                              **({'Content-Type': 'application/json'} if data is not None else {})})
    opener = urllib.request.build_opener(NoRedirect, urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    try:
        response = opener.open(request, timeout=30)
    except urllib.error.HTTPError as error:
        response = error
    except (urllib.error.URLError, OSError) as error:
        raise Refusal(f'network error contacting RoadS ({type(error).__name__})')
    with response:
        raw = response.read(MAX_BODY_BYTES + 1)
    require(len(raw) <= MAX_BODY_BYTES, 'the RoadS response exceeded the size limit')
    return response.status if hasattr(response, 'status') else response.code, raw.decode('utf-8', 'replace')


def endpoint_url(base, leaf, query=None):
    parts = urllib.parse.urlsplit(base)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip('/') + '/' + leaf, query or '', ''))


def request(ctx, transport, method, url, body=None):
    secret = read_secret(ctx.secret_env)
    base, target = urllib.parse.urlsplit(ctx.endpoint), urllib.parse.urlsplit(url)
    base_path = base.path.rstrip('/')
    require(target.scheme == base.scheme and target.netloc == base.netloc
            and (target.path == base_path or target.path.startswith(base_path + '/')),
            'internal error: request URL left the approved endpoint')
    status, text = transport(method, url, {'Authorization': f'Bearer {secret}'}, body)
    require(not 300 <= status < 400, f'RoadS answered with a redirect (HTTP {status}); redirects are never followed, '
                                     'so the credential was not sent anywhere else. Check roadmapSync.endpoint.')
    require(status not in (401, 403), f'RoadS rejected the credential (HTTP {status}). Check that {ctx.secret_env} holds '
                                      'the current value, then restart Claude.')
    require(200 <= status < 300, f'RoadS answered HTTP {status}')
    return text


# ---------------------------------------------------------------- plan, ack and files

def read_text(path):
    path = Path(path)
    if not path.is_file():
        return None
    return path.read_bytes().decode('utf-8-sig', 'replace')


def has_bom(path):
    path = Path(path)
    return path.is_file() and path.read_bytes()[:3] == b'\xef\xbb\xbf'


def read_plan(ctx):
    """Every reader of a plan re-derives what it trusts: each target path must resolve again to
    exactly itself, each staged copy must sit inside the staging directory, asOf must still parse to
    its own spelling (it is POSTed), and the plan must have been made under the current nonce."""
    require(ctx.plan_path.is_file(), 'No plan found; run fetch first.')
    plan = read_json(ctx.plan_path)
    require(plan.get('project') == ctx.name, 'The stored plan belongs to another project; run fetch again.')
    staging = os.path.normcase(os.path.abspath(ctx.staging_dir)) + os.sep
    stored = plan.get('targets')
    require(isinstance(stored, dict) and isinstance(stored.get('roadmap'), dict), 'The stored plan has no roadmap target; run fetch again.')
    seen = set()
    for name, target in stored.items():
        require(isinstance(target, dict) and target.get('path'), f'The stored plan has no {name} target; run fetch again.')
        require(safe_target(ctx.scrum_root, relative_to_root(ctx, target['path'])) == target['path'],
                'The stored plan does not match the configuration; run fetch again.')
        require(os.path.normcase(target['path']) not in seen,
                'The stored plan resolves two targets to the same path; check the patterns.')
        seen.add(os.path.normcase(target['path']))
        staged = target.get('staged') or ''
        require(os.path.normcase(os.path.abspath(staged)).startswith(staging),
                f'The stored plan points the {name} staged copy outside the staging directory; run fetch again.')
        if os.path.lexists(staged):
            tag = reparse_tag(staged)
            require(not os.path.isdir(staged) and (tag == 0 or cloud_tag(tag)),
                    f'The stored plan points the {name} staged copy at a directory or reparse point; run fetch again.')
    if plan.get('asOf'):
        parsed = timestamp(plan['asOf'], 'the stored plan asOf')
        require(parsed and parsed[0] == plan['asOf'], 'The stored plan asOf was altered since fetch; run fetch again.')
    record = marker_record(ctx)
    require(record, 'No marker nonce is stored, so no marker can be verified; run fetch again.')
    require(plan.get('markerNonceId') == nonce_id(record['nonce']), 'The stored plan was made with a different marker nonce; run fetch again.')
    return plan


def marker_report(plan, nonce):
    texts = [read_text(target['path']) for target in plan['targets'].values()]
    missing, declined = [], []
    for change in plan['changes']:
        states = {marker_state(text, change['id'], nonce) for text in texts} - {None}
        if 'applied' in states:
            continue
        (declined if 'declined' in states else missing).append(change['id'])
    return missing, declined


def do_ack(ctx, transport, plan, result, nonce, confirm_declined):
    missing, declined = marker_report(plan, nonce)
    if missing:
        result.update(ack='refused', missingMarkers=missing, retryable=True,
                      message='Not acknowledged: these changes have no marker in the roadmap or sprint file: ' + ', '.join(missing))
        return 1
    # A declined change is consumed by the acknowledgement exactly like an applied one, so the
    # decline must be the user's, confirmed separately, never a conclusion drawn from service text.
    if declined:
        result['declined'] = declined
        if not confirm_declined:
            result.update(ack='refused', retryable=True, message=(
                'Not acknowledged yet: these changes are marked declined, and acknowledging consumes them at RoadS for good: '
                + ', '.join(declined) + '. Show the user each one and, only after they confirm each id, run ack with --confirm-declined.'))
            return 1
        result['declinedConfirmed'] = True
    if not plan.get('asOf'):
        result.update(ack='skipped', retryable=False, message=(
            'Nothing acknowledged: RoadS sent no asOf and no change carried a usable createdAt, so there is no point to '
            'acknowledge up to. Running ack again will not change that; take it to the RoadS owner.'))
        return 2
    try:
        assert_approved(ctx)
        answer = request(ctx, transport, 'POST', endpoint_url(ctx.endpoint, 'ack'), json.dumps({'asOf': plan['asOf']}))
    except Refusal as error:
        result.update(ack='failed', retryable=True, message=(
            f'Files are written and verified, but the acknowledgement failed: {error} Nothing is lost; the next sync skips '
            'the changes already marked and acknowledges them.'))
        return 2
    try:
        acked = json.loads(answer).get('acked')
    except (ValueError, AttributeError):
        acked = None
    write_json_atomic({'schemaVersion': 1, 'lastAckAsOf': plan['asOf'], 'lastAckAt': now_iso()}, ctx.state_path)
    plan['ackedAt'] = now_iso()
    write_json_atomic(plan, ctx.plan_path)
    result.update(ack='sent', asOf=plan['asOf'], acked=acked if isinstance(acked, int) else None)
    return 0


def backup_prefix(path):
    return '.' + Path(path).name + '.roads-backup'


def backup_generations(path):
    directory, prefix = Path(path).parent, backup_prefix(path)
    if not directory.is_dir():
        return []
    found = [p for p in directory.iterdir() if p.is_file() and (p.name == prefix or p.name.startswith(prefix + '-'))]
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def new_backup_path(path):
    stamp = dt.datetime.now().strftime('%Y%m%d-%H%M%S')
    for attempt in range(100):
        candidate = Path(path).parent / (backup_prefix(path) + '-' + stamp + (f'-{attempt}' if attempt else ''))
        if not candidate.exists():
            return candidate
    raise Refusal(f'Cannot find a free backup name beside {path}')


def hide(path):
    if sys.platform.startswith('win'):
        try:
            import ctypes
            attributes = ctypes.windll.kernel32.GetFileAttributesW(str(path))
            if attributes != -1:
                ctypes.windll.kernel32.SetFileAttributesW(str(path), attributes | 0x2)
        except (OSError, AttributeError):
            pass


def prune_backups(path):
    for stale in backup_generations(path)[BACKUP_GENERATIONS:]:
        try:
            os.chmod(stale, 0o666)
            stale.unlink()
        except OSError:
            pass


def write_text_atomic(path, text, bom, backup):
    """The previous content always exists in some file: the backup is created (never over an
    existing name) before the new content replaces the target."""
    path = Path(path)
    temporary = path.parent / f'.{path.name}.{secrets.token_hex(8)}.tmp'
    try:
        with open(temporary, 'wb') as handle:
            handle.write((b'\xef\xbb\xbf' if bom else b'') + text.encode('utf-8'))
        if path.exists():
            require(backup, f'internal error: refusing to replace {path} without a backup')
            with open(backup, 'xb') as handle:
                handle.write(path.read_bytes())
            hide(backup)
            os.replace(temporary, path)
            prune_backups(path)
        else:
            os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def write_manifest(staging, files):
    write_json_atomic({'schemaVersion': 1, 'createdAt': now_iso(), 'appliedAt': None, 'files': files}, Path(staging) / MANIFEST)


def mark_staging_applied(staging):
    path = Path(staging) / MANIFEST
    try:
        manifest = read_json(path)
        manifest['appliedAt'] = now_iso()
        write_json_atomic(manifest, path)
    except (OSError, ValueError):
        pass


def staging_edits(staging):
    """Staged files that differ from what fetch put there and were never applied. With no readable
    manifest every file counts as edited: "cannot tell" means there may be work worth keeping."""
    staging = Path(staging)
    if not staging.is_dir():
        return []
    files = [p for p in staging.iterdir() if p.is_file() and p.name != MANIFEST]
    if not files:
        return []
    try:
        manifest = read_json(staging / MANIFEST)
    except (OSError, ValueError):
        return sorted(p.name for p in files)
    if manifest.get('appliedAt'):
        return []
    return sorted(p.name for p in files if (manifest.get('files') or {}).get(p.name) != sha256(p))


def remove_stale_staging(ctx):
    parent, leaf = ctx.staging_dir.parent, ctx.staging_dir.name
    if not parent.is_dir():
        return
    cutoff = dt.datetime.now().timestamp() - 24 * 3600
    for directory in parent.iterdir():
        is_new, is_previous = directory.name.startswith(leaf + '.new-'), directory.name.startswith(leaf + '.previous-')
        # A `.previous-*` may be the only copy of drafted prose while the staging directory is
        # missing mid-swap, so it is removed only while the staging directory exists.
        if not directory.is_dir() or not (is_new or is_previous) or (is_previous and not ctx.staging_dir.is_dir()):
            continue
        if directory.stat().st_mtime > cutoff:
            continue
        for child in sorted(directory.rglob('*'), reverse=True):
            child.unlink() if child.is_file() else child.rmdir()
        directory.rmdir()


def remove_tree(path):
    path = Path(path)
    if not path.exists():
        return
    for child in sorted(path.rglob('*'), reverse=True):
        child.unlink() if child.is_file() else child.rmdir()
    path.rmdir()


# ---------------------------------------------------------------- operations

def op_status(root, today):
    result = {'configured': False}
    try:
        ctx = Context(root)
    except (Refusal, ValueError, OSError) as error:
        result.update(ok=True, exitCode=0, ready=False, message=str(error))
        return result
    state, pair, _ = approval_state(ctx)
    result.update(configured=True, project=ctx.name, secretEnvVar=ctx.secret_env,
                  secret='present' if secret_present(ctx.secret_env) else 'absent',
                  approval=state, endpoint=pair['endpoint'],
                  scrumRoot='exists' if os.path.isdir(ctx.scrum_root) else 'missing',
                  maxSprintItems=ctx.max_sprint_items, issueTargets=sorted(ctx.issue_targets))
    paths = targets(ctx, today)
    start, end = paths['week']
    result['week'] = {'start': start.isoformat(), 'end': end.isoformat()}
    for name in ('roadmap', 'sprint'):
        try:
            full = safe_target(ctx.scrum_root, paths[name])
            result[name] = {'path': full, 'exists': os.path.isfile(full), 'safe': True}
        except Refusal as error:
            result[name] = {'relative': paths[name], 'safe': False, 'problem': str(error)}
    try:
        result['lastAckAsOf'] = read_json(ctx.state_path).get('lastAckAsOf')
    except (OSError, ValueError):
        result['lastAckAsOf'] = None
    result['sinceAvailable'] = result['lastAckAsOf']
    record = marker_record(ctx)
    result['markerNonce'] = 'present' if record else 'absent'
    if record:
        created = timestamp(record.get('createdAt'))
        result['markerNonceCreatedAt'] = record.get('createdAt')
        result['markerNonceAgeDays'] = (dt.datetime.now(dt.timezone.utc) - created[1]).days if created else None
    result['stagedDraft'] = staging_edits(ctx.staging_dir)
    result['ready'] = (result['secret'] == 'present' and state == 'approved' and result['scrumRoot'] == 'exists'
                       and result['roadmap']['safe'] and result['sprint']['safe'])
    message = 'Roadmap sync is ready.' if result['ready'] else 'Roadmap sync is not ready; see the fields above.'
    if not record:
        message += (' No marker nonce is stored yet: the next fetch mints one. If this project has synced before, marker.json '
                    'was lost and every change already written will be offered again; check for duplicates before applying.')
    result.update(ok=True, exitCode=0, message=message)
    return result


def op_approve(root):
    ctx = Context(root)
    pair = approval_pair(ctx)
    write_json_atomic({'schemaVersion': 1, **pair, 'approvedAt': now_iso()}, ctx.approval_path)
    return {'ok': True, 'exitCode': 0, 'project': ctx.name, 'approval': 'approved', **pair,
            'message': f'Approved: {pair["secretEnvVar"]} may be sent to {pair["endpoint"]}.'}


def sync_board(ctx, transport):
    """POST <endpoint>/sync-board, the board's own "Sincronizar". Its failure never aborts the
    fetch: it is reported, and the user decides whether to go on with the last state."""
    try:
        text = request(ctx, transport, 'POST', endpoint_url(ctx.endpoint, 'sync-board'))
    except Refusal as error:
        return {'ok': False, 'ran': False, 'reason': 'request_failed', 'message': str(error)}
    return board_result(text)


def read_state(ctx, transport):
    """GET <endpoint>/roadmap-state. Without it there is no plan: no fallback to the local week."""
    try:
        text = request(ctx, transport, 'GET', endpoint_url(ctx.endpoint, 'roadmap-state'))
        payload = json.loads(text)
    except (Refusal, ValueError) as error:
        detail = str(error) if isinstance(error, Refusal) else 'the body is not JSON'
        raise Refusal(f'Could not read roadmap-state from RoadS ({detail}). There is no fallback: without it the sprints '
                      'cannot be resolved, so nothing was written or acknowledged.')
    return normalise_state(payload)


def sprint_targets(ctx, state, changes):
    """Each sprint of the state with a pending change gets its own file. The link is the
    service's: a change belongs to a sprint when an item of that sprint lists it in
    pendingChangeIds, or when removedPending puts its removal in that sprint's lane. A change
    reaching a sprint only through an item past the limit (overLimit) is listed apart and not
    written into that sprint file."""
    change_ids = {c['id'] for c in changes}
    sprints, files = [], {}
    for sprint in state['sprints']:
        within = [i for i in sprint['items'] if not i['overLimit']]
        over = [i for i in sprint['items'] if i['overLimit']]
        write = {cid for item in within for cid in item['pendingChangeIds'] if cid in change_ids}
        write |= {r['changeId'] for r in state['removedPending']
                  if r['laneId'] and r['laneId'] == sprint['laneId'] and r['changeId'] in change_ids}
        left_out = {cid for item in over for cid in item['pendingChangeIds'] if cid in change_ids} - write
        name = 'sprint:' + sprint['sprintId'] if write else None
        public = {key: value for key, value in sprint.items() if key != '_dates'}
        sprints.append({**public, 'items': within, 'outOfLimit': over, 'target': name})
        if name:
            start, end = sprint['_dates']
            files[name] = {'sprint': public, 'dates': (start, end), 'relative': sprint_relative(ctx, start, end),
                           'changeIds': sorted(write), 'outOfLimitChangeIds': sorted(left_out)}
    left_out_items = [item for sprint in sprints for item in sprint['outOfLimit'] if set(item['pendingChangeIds']) & change_ids]
    return sprints, files, left_out_items


def op_fetch(root, today, transport, since=None, discard_staged=False):
    # A refusal after the board sync still reports what the board sync did.
    result = {}
    try:
        return _fetch(root, today, transport, since, discard_staged, result)
    except (Refusal, ValueError, OSError, KeyError, TypeError) as error:
        result.update(ok=False, exitCode=1, message=str(error))
        return result


def _fetch(root, today, transport, since, discard_staged, result):
    ctx = Context(root)
    result['project'] = ctx.name
    read_secret(ctx.secret_env)
    assert_approved(ctx)
    paths = targets(ctx, today)
    roadmap_path = safe_target(ctx.scrum_root, paths['roadmap'])
    edits = staging_edits(ctx.staging_dir)
    require(not edits or discard_staged, (
        f'Refusing to fetch: the staging directory holds drafted prose that was never applied ({", ".join(edits)}) in '
        f'{ctx.staging_dir}. Run apply to write it, or copy it out first; only then fetch again with --discard-staged.'))
    query = None
    if since:
        stamp = timestamp(since)
        require(stamp, '--since must be an ISO 8601 timestamp')
        query = 'since=' + urllib.parse.quote(stamp[0], safe='')
    board = sync_board(ctx, transport)
    result.update(syncBoard=board, syncBoardFailed=not board['ok'])
    notes = []
    if not board['ok']:
        notes.append(f' The RoadS board sync failed ({board["reason"]}: {board["message"]}). What follows is the last state '
                     'RoadS holds: ask the user whether to go on with it before drafting anything.')
    state = read_state(ctx, transport)
    result.update(stateAsOf=state['asOf'], snapshotSyncedAt=state['snapshotSyncedAt'], snapshotStale=state['snapshotStale'])
    if state['snapshotStale']:
        notes.append(' The RoadS snapshot is ' + (f'stale (synced at {state["snapshotSyncedAt"]}, more than 24 h before {state["asOf"]})'
                                                  if state['snapshotSyncedAt'] else 'missing (never synced)') + '; tell the user.')
    text = request(ctx, transport, 'GET', endpoint_url(ctx.endpoint, 'pending-changes', query))
    try:
        payload = json.loads(text)
    except ValueError:
        raise Refusal('RoadS answered with a body that is not JSON')
    changes = normalise_changes(payload, ctx.issue_targets)
    if not changes:
        result.update(ok=True, exitCode=0, pending=0,
                      message='Nothing pending. No file was written and nothing was acknowledged.' + ''.join(notes))
        return result
    sprints, files, left_out = sprint_targets(ctx, state, changes)
    if left_out:
        notes.append(f' {len(left_out)} item(s) with a pending change are outside the sprint limit (overLimit): write them in '
                     'the roadmap only and tell the user which ones stayed out.')
    resolved = {'roadmap': roadmap_path}
    for name, info in files.items():
        resolved[name] = safe_target(ctx.scrum_root, info['relative'])
    clash = len({os.path.normcase(p) for p in resolved.values()}) != len(resolved)
    require(not clash, 'Two targets resolve to the same file (' + ', '.join(sorted(resolved)) + '); fix roadmapFile, '
                       'weekFolderPattern or sprintFilePattern so each sprint has its own file.')
    nonce, minted = marker_nonce(ctx)
    result['markerNonceMinted'] = minted
    stamp, source = as_of(payload, changes)
    texts = {name: read_text(path) for name, path in resolved.items()}
    for change in changes:
        states = {marker_state(text, change['id'], nonce) for text in texts.values()} - {None}
        change['alreadyApplied'] = bool(states)
        change['markerState'] = 'applied' if 'applied' in states else 'declined' if states else None
        change['marker'] = marker_text(change['id'], nonce)
        change['declinedMarker'] = marker_text(change['id'], nonce, True)
        change['sprintTargets'] = [name for name, info in files.items() if change['id'] in info['changeIds']]
        del change['_created']
    run = secrets.token_hex(8)
    staging_new = ctx.staging_dir.with_name(ctx.staging_dir.name + '.new-' + run)
    staging_previous = ctx.staging_dir.with_name(ctx.staging_dir.name + '.previous-' + run)
    remove_stale_staging(ctx)
    staging_new.mkdir(parents=True)
    # A sprint file that does not exist yet is staged empty; apply creates its folder and file.
    leaves = {name: ('roadmap--' if name == 'roadmap' else f'sprint--{files[name]["sprint"]["sprintId"]}--') + Path(path).name
              for name, path in resolved.items()}
    for name, leaf in leaves.items():
        (staging_new / leaf).write_bytes((texts[name] or '').encode('utf-8'))
    write_manifest(staging_new, {leaf: sha256(staging_new / leaf) for leaf in leaves.values()})
    if ctx.staging_dir.exists():
        os.replace(ctx.staging_dir, staging_previous)
    os.replace(staging_new, ctx.staging_dir)
    remove_tree(staging_previous)
    plan_targets = {}
    for name, path in resolved.items():
        target = {'kind': 'roadmap' if name == 'roadmap' else 'sprint', 'path': path, 'exists': texts[name] is not None,
                  'sha256': sha256(path), 'staged': str(ctx.staging_dir / leaves[name])}
        if name != 'roadmap':
            info = files[name]
            target.update(sprintId=info['sprint']['sprintId'], title=info['sprint']['title'],
                          startDate=info['sprint']['startDate'], endDate=info['sprint']['endDate'],
                          createsFolder=not os.path.isdir(os.path.dirname(path)),
                          template=None if texts[name] is not None else sprint_template(ctx, *info['dates']),
                          changeIds=info['changeIds'], outOfLimitChangeIds=info['outOfLimitChangeIds'])
        plan_targets[name] = target
    pending = [c['id'] for c in changes if not c['alreadyApplied']]
    start, end = paths['week']
    plan = {'schemaVersion': 1, 'project': ctx.name, 'markerNonceId': nonce_id(nonce), 'asOf': stamp, 'asOfSource': source,
            'stateAsOf': state['asOf'], 'week': {'start': start.isoformat(), 'end': end.isoformat()},
            'maxSprintItems': state['maxSprintItems'] or ctx.max_sprint_items, 'sprints': sprints,
            'targets': plan_targets, 'pending': pending, 'changes': changes}
    write_json_atomic(plan, ctx.plan_path)
    message = (f'{len(pending)} change(s) to apply; {len(changes) - len(pending)} already marked.' if pending
               else 'Every returned change is already marked in the files; run ack to acknowledge them.')
    message += ''.join(notes)
    if minted:
        message += (' This run minted a new marker nonce, so no existing marker counts any more. If this project has synced '
                    'before, marker.json was lost: changes already written are offered again; check and decline duplicates.')
    try:
        result['sinceAvailable'] = read_json(ctx.state_path).get('lastAckAsOf')
    except (OSError, ValueError):
        result['sinceAvailable'] = None
    result.update(ok=True, exitCode=0, planPath=str(ctx.plan_path), since=query, plan=plan, message=message)
    return result


def op_apply(root, transport, no_ack=False, allow_shrink=False, confirm_declined=False):
    # Every apply result states what is on disk, a refusal included, so `written` and `backups`
    # are never left for the reader to infer from their absence.
    result = {'written': [], 'backups': []}
    try:
        return _apply(root, transport, no_ack, allow_shrink, confirm_declined, result)
    except (Refusal, ValueError, OSError, KeyError, TypeError) as error:
        result.update(ok=False, exitCode=1, message=str(error))
        return result


def _apply(root, transport, no_ack, allow_shrink, confirm_declined, result):
    ctx = Context(root)
    result['project'] = ctx.name
    plan = read_plan(ctx)
    require('ackedAt' not in plan, 'This plan was already acknowledged; run fetch again.')
    nonce = marker_record(ctx)['nonce']
    plan_ids = {c['id'] for c in plan['changes']}
    planned, seen = [], set()
    # Phase one decides everything and touches nothing; phase two only writes.
    for name, target in plan['targets'].items():
        relative = relative_to_root(ctx, target['path'])
        path = safe_target(ctx.scrum_root, relative)
        require(path == target['path'], 'The stored plan does not match the configuration; run fetch again.')
        require(os.path.normcase(path) not in seen, f'The plan resolves both targets to {path}. Nothing was written.')
        seen.add(os.path.normcase(path))
        require(os.path.isfile(target['staged']), (
            f'Staged copy missing for the {name} file; run fetch again. Nothing was written. If a fetch was interrupted, '
            f'the draft may be in a {ctx.staging_dir.name}.previous-* folder; copy it out first.'))
        staged = Path(target['staged']).read_bytes().decode('utf-8-sig', 'replace')
        current = read_text(path)
        if (current is None and not staged) or (current is not None and staged == current):
            continue
        current_sha = sha256(path)
        require(current_sha == target['sha256'] or (target.get('appliedSha256') and current_sha == target['appliedSha256']),
                f'The {name} file changed after the plan was made ({path}). Nothing was written; sync again.')
        if current and not allow_shrink and len(staged) < int(len(current) * SHRINK_RATIO):
            raise Refusal(f'Refusing to write the {name} file: the staged copy has {len(staged)} characters against {len(current)} '
                          f'in {path}, dropping {len(current) - len(staged)}. Nothing was written. Show the user the diff; only '
                          'their confirmation lets it through (--allow-shrink).')
        current_markers, staged_markers = marker_inventory(current, nonce), marker_inventory(staged, nonce)
        lost = [i for i in current_markers if i not in staged_markers]
        if lost and not allow_shrink:
            raise Refusal(f'Refusing to write the {name} file: the staged copy lost the marker of {", ".join(lost)} that {path} '
                          'carries now, so it drops content a previous run wrote. Nothing was written. Show the user those '
                          'entries; they decide.')
        invented = [i for i in staged_markers if i not in current_markers and i not in plan_ids]
        require(not invented, f'Refusing to write the {name} file: the staged copy carries a marker for {", ".join(invented)}, '
                              'which is not in this plan. Nothing was written. Copy markers from the plan only.')
        # RoadS decides the sprint limit: a change that touches this sprint only through an item
        # past the limit is written in the roadmap and reported, never into this sprint file.
        over = [i for i in staged_markers if i not in current_markers and i in (target.get('outOfLimitChangeIds') or [])]
        require(not over, f'Refusing to write the {name} file: the staged copy carries a marker for {", ".join(over)}, '
                          'whose item is past the sprint limit (overLimit). Nothing was written. Write it in the roadmap only.')
        planned.append((name, target, path, relative, staged))
    written, backups = [], []
    try:
        for name, target, path, relative, staged in planned:
            # Checked again before a missing folder is created, so mkdir cannot follow a junction
            # that appeared after fetch, and again before the write.
            safe_target(ctx.scrum_root, relative)
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            safe_target(ctx.scrum_root, relative)
            replaced = os.path.isfile(path)
            backup = new_backup_path(path) if replaced else None
            write_text_atomic(path, staged, has_bom(path), backup)
            written.append(path)
            if backup:
                backups.append(str(backup))
            target['appliedSha256'] = sha256(path)
    finally:
        result['written'], result['backups'] = written, backups
        if written:
            try:
                write_json_atomic(plan, ctx.plan_path)
            except OSError:
                pass
            mark_staging_applied(ctx.staging_dir)
    if not planned:
        mark_staging_applied(ctx.staging_dir)
    missing, declined = marker_report(plan, nonce)
    if missing:
        result.update(ok=False, exitCode=1, missingMarkers=missing, message=(
            'Written, but these changes have no marker in either file, so nothing was acknowledged: ' + ', '.join(missing)
            + '. Add the markers to the staged copies and run apply again; this run\'s own write does not block the retry.'))
        return result
    result['verified'] = True
    if no_ack:
        if declined:
            result['declined'] = declined
        result.update(ok=True, exitCode=0, ack='not requested', message='Written and verified; not acknowledged (--no-ack).')
        return result
    code = do_ack(ctx, transport, plan, result, nonce, confirm_declined)
    result.update(ok=code == 0, exitCode=code)
    if code == 0:
        result['message'] = f'Written, verified and acknowledged up to {plan["asOf"]}.'
    return result


def op_ack(root, transport, confirm_declined=False):
    ctx = Context(root)
    result = {'project': ctx.name}
    plan = read_plan(ctx)
    code = do_ack(ctx, transport, plan, result, marker_record(ctx)['nonce'], confirm_declined)
    result.update(ok=code == 0, exitCode=code)
    if code == 0:
        result['message'] = f'Acknowledged up to {plan["asOf"]}.'
    return result


def op_rotate(root, today):
    ctx = Context(root)
    record = marker_record(ctx)
    require(record, 'There is no marker nonce to rotate; run fetch first.')
    edits = staging_edits(ctx.staging_dir)
    require(not edits, f'Refusing to rotate: the staging directory holds drafted prose never applied ({", ".join(edits)}).')
    if ctx.plan_path.is_file():
        try:
            pending = read_json(ctx.plan_path)
        except (OSError, ValueError):
            pending = None
        require(not pending or 'ackedAt' in pending, 'Refusing to rotate: a plan has not been acknowledged. Finish or discard it first.')
    paths = targets(ctx, today)
    fresh = new_nonce()
    rewritten, backups, rotated = [], [], []
    for name in ('roadmap', 'sprint'):
        path = safe_target(ctx.scrum_root, paths[name])
        text = read_text(path)
        if text is None:
            continue
        inventory = marker_inventory(text, record['nonce'])
        updated = text
        for change_id, state in inventory.items():
            updated = re.sub(marker_pattern(change_id, record['nonce']),
                             marker_text(change_id, fresh, state == 'declined').replace('\\', r'\\'), updated)
            rotated.append(change_id)
        if updated == text:
            continue
        backup = new_backup_path(path)
        write_text_atomic(path, updated, has_bom(path), backup)
        rewritten.append(path)
        backups.append(str(backup))
    write_marker_record(ctx, fresh, rotated=True)
    rotated = sorted(set(rotated))
    return {'ok': True, 'exitCode': 0, 'project': ctx.name, 'rewritten': rewritten, 'backups': backups,
            'rotatedMarkers': rotated, 'message': (
                f'Marker nonce rotated. Rewrote {len(rotated)} marker(s) in {len(rewritten)} file(s). Markers in earlier '
                "weeks' sprint files were not rewritten and no longer count; decline those changes if offered again.")}


def run(operation, root='.', today=None, transport=None, since=None, no_ack=False, allow_shrink=False,
        confirm_declined=False, discard_staged=False):
    today = today or dt.date.today()
    transport = transport or http_default
    try:
        if operation == 'status':
            result = op_status(root, today)
        elif operation == 'approve':
            result = op_approve(root)
        elif operation == 'fetch':
            result = op_fetch(root, today, transport, since, discard_staged)
        elif operation == 'apply':
            result = op_apply(root, transport, no_ack, allow_shrink, confirm_declined)
        elif operation == 'ack':
            result = op_ack(root, transport, confirm_declined)
        elif operation == 'rotate-markers':
            result = op_rotate(root, today)
        else:
            raise Refusal(f'Unknown operation: {operation}')
    except (Refusal, ValueError, OSError, KeyError, TypeError) as error:
        result = {'ok': False, 'exitCode': 1, 'message': str(error)}
    result['operation'] = operation
    result.setdefault('ok', result.get('exitCode') == 0)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('operation', choices=['status', 'approve', 'fetch', 'apply', 'ack', 'rotate-markers'])
    parser.add_argument('--root', default='.', help='project whose .frontlights/config.json declares roadmapSync')
    parser.add_argument('--since', help='ISO 8601; adds since=<stamp> to pending-changes. Never filled in automatically.')
    parser.add_argument('--no-ack', action='store_true')
    parser.add_argument('--allow-shrink', action='store_true', help="the user's confirmation that a file may lose content")
    parser.add_argument('--confirm-declined', action='store_true', help="the user's confirmation of every declined id")
    parser.add_argument('--discard-staged', action='store_true', help='let fetch replace a draft that was never applied')
    args = parser.parse_args()
    result = run(args.operation, args.root, since=args.since, no_ack=args.no_ack, allow_shrink=args.allow_shrink,
                 confirm_declined=args.confirm_declined, discard_staged=args.discard_staged)
    print(protect(json.dumps(result, indent=2, ensure_ascii=True)))
    return int(result['exitCode'])


if __name__ == '__main__':
    sys.exit(main())
