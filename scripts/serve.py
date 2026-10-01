#!/usr/bin/env python3
"""Start, inspect and stop the processes of one issue's browser test environment.

Helper behind the browser-test route of /frontlights. It reads the `browserTest` block of the
project's `.frontlights/config.json` and brings up each process in the issue's worktree.

Operations (each prints one JSON object on stdout):
  start   spawn every process, wait for its health URL (HTTP 2xx/3xx, polled until a deadline)
          and record pids and times in `<root>/.frontlights/serve/<issue>.json`.
  status  read that record and check whether each pid is still alive.
  stop    kill the whole process tree of each recorded process and remove the record.

Exit codes: 0 done; 1 refusal or failure (a failed start tears down whatever already came up and
names the failing process, classified as an infrastructure failure).

Processes are started from an argv list, never through a shell. Logins and passwords from the
config are never read into the record and are redacted from every string this helper prints.
"""

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

POLL_SECONDS = 0.2
DEFAULT_TIMEOUT = 60
INFRASTRUCTURE = 'infraestrutura'
USAGE = 'uso'
UNKNOWN_IDENTITY = 'desconhecida'
BATCH_UNSAFE = '&|^%<>!"'

_SECRETS = []


class Refusal(Exception):
    """A deliberate refusal: the message is safe to show the user."""

    def __init__(self, message, process=None, category=None):
        super().__init__(message)
        self.process = process
        self.category = category or INFRASTRUCTURE


def require(condition, message, process=None, category=None):
    if not condition:
        raise Refusal(message, process, category)


def now_iso():
    return dt.datetime.now().astimezone().isoformat()


def protect(text):
    for secret in _SECRETS:
        if secret:
            text = text.replace(secret, '[redacted]')
    return text


def scrub(value):
    """Apply protect to every string of a JSON-like value (keys included)."""
    if isinstance(value, str):
        return protect(value)
    if isinstance(value, list):
        return [scrub(item) for item in value]
    if isinstance(value, dict):
        return {scrub(key): scrub(item) for key, item in value.items()}
    return value


def write_json_atomic(value, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f'.{path.name}.{secrets.token_hex(8)}.tmp'
    try:
        temporary.write_text(json.dumps(scrub(value), indent=2, ensure_ascii=False), encoding='utf-8')
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


# ---------------------------------------------------------------- configuration

def load_block(config_file):
    try:
        config = json.loads(Path(config_file).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        raise Refusal('Não foi possível ler o config informado em --config.')
    block = config.get('browserTest') if isinstance(config, dict) else None
    require(isinstance(block, dict), 'O config não tem o bloco browserTest.')
    for user in block.get('users') or []:
        if isinstance(user, dict):
            for key in ('login', 'password'):
                if isinstance(user.get(key), str):
                    _SECRETS.extend([user[key], urllib.parse.quote(user[key], safe=''),
                                     urllib.parse.quote_plus(user[key])])
    processes = block.get('processes')
    require(isinstance(processes, list) and processes, 'browserTest.processes precisa ser uma lista não vazia.')
    names = set()
    for item in processes:
        require(isinstance(item, dict), 'Cada item de browserTest.processes precisa ser um objeto.')
        name = item.get('name')
        require(isinstance(name, str) and name and name not in names,
                'Cada processo precisa de um name único e não vazio.')
        names.add(name)
        argv = item.get('argv')
        require(isinstance(argv, list) and argv and all(isinstance(part, str) and part for part in argv),
                f'O processo {name} precisa de argv como lista de strings (sem shell embutido).', name)
        health = item.get('health')
        parts = urllib.parse.urlsplit(health) if isinstance(health, str) else None
        require(parts and parts.scheme in ('http', 'https') and parts.hostname,
                f'O processo {name} precisa de health como URL http(s).', name)
        require('port' not in item or isinstance(item['port'], int) and not isinstance(item['port'], bool),
                f'O processo {name} tem port inválida: use um inteiro.', name)
    return processes


def process_port(item):
    if isinstance(item.get('port'), int):
        return item['port']
    parts = urllib.parse.urlsplit(item['health'])
    return parts.port or (443 if parts.scheme == 'https' else 80)


def process_cwd(root, item):
    base = Path(root).resolve()
    target = (base / item['cwd']).resolve() if item.get('cwd') else base
    require(target == base or base in target.parents,
            f'O cwd do processo {item["name"]} precisa ficar dentro da worktree da issue.', item['name'])
    require(target.is_dir(), f'O cwd do processo {item["name"]} não existe.', item['name'])
    return target


# ---------------------------------------------------------------- process control

def spawn(argv, cwd):
    executable = shutil.which(argv[0])
    if executable is None:
        raise OSError('executável não encontrado')
    options = {'cwd': str(cwd), 'stdin': subprocess.DEVNULL, 'stdout': subprocess.DEVNULL,
               'stderr': subprocess.DEVNULL}
    if os.name == 'nt':
        options['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        options['start_new_session'] = True
    return subprocess.Popen([executable] + argv[1:], **options)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirectHandler)


def healthy(url):
    try:
        with _OPENER.open(url, timeout=2) as answer:
            return 200 <= answer.status < 400
    except urllib.error.HTTPError as error:
        return 300 <= error.code < 400
    except (urllib.error.URLError, OSError, ValueError):
        return False


def wait_healthy(child, item):
    timeout = item.get('timeoutSeconds', DEFAULT_TIMEOUT)
    require(isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and timeout > 0,
            f'O processo {item["name"]} tem timeoutSeconds inválido.', item['name'])
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise Refusal(f'O processo {item["name"]} terminou antes de ficar saudável '
                          f'(código {child.returncode}).', item['name'])
        if healthy(item['health']):
            return
        time.sleep(POLL_SECONDS)
    raise Refusal(f'O processo {item["name"]} não ficou saudável em {timeout} s.', item['name'])


def guard_batch(argv, name):
    """On Windows a .cmd/.bat runs through cmd.exe, which would interpret shell metacharacters."""
    if os.name != 'nt':
        return
    executable = shutil.which(argv[0]) or ''
    if executable.lower().endswith(('.cmd', '.bat')):
        require(not any(char in part for part in argv[1:] for char in BATCH_UNSAFE),
                f'O processo {name} usa um .cmd/.bat (via cmd.exe): o argv não pode ter '
                f'metacaracteres de shell ({BATCH_UNSAFE}).', name)


def kill_tree(pid):
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'], capture_output=True, timeout=60)
    else:
        import signal
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def pid_alive(pid):
    if os.name == 'nt':
        import ctypes
        kernel = ctypes.windll.kernel32
        kernel.OpenProcess.restype = ctypes.c_void_p
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            ok = kernel.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        finally:
            kernel.CloseHandle(ctypes.c_void_p(handle))
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def process_identity(pid):
    """Creation time of a process as a string, or None when the platform cannot tell."""
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.windll.kernel32
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            created, spent, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
            if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(spent),
                                          ctypes.byref(kernel_time), ctypes.byref(user_time)):
                return None
            return str((created.dwHighDateTime << 32) | created.dwLowDateTime)
        finally:
            kernel.CloseHandle(handle)
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rpartition(')')[2].split()
        return fields[19]  # field 22: start time in clock ticks since boot
    except (OSError, IndexError):
        pass
    try:
        done = subprocess.run(['ps', '-o', 'lstart=', '-p', str(pid)], capture_output=True, text=True,
                              timeout=10, env=dict(os.environ, LC_ALL='C'))
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() or None


def same_process(entry):
    """True when the recorded pid is alive and (as far as known) is still the recorded process."""
    if not pid_alive(entry['pid']):
        return False
    recorded, current = entry.get('identity'), process_identity(entry['pid'])
    if recorded in (None, UNKNOWN_IDENTITY) or current is None:
        return True  # cannot tell: assume alive for refusing a start; never used to kill
    return recorded == current


# ---------------------------------------------------------------- operations

def registry_path(root, issue):
    return Path(root) / '.frontlights' / 'serve' / f'{issue}.json'


def start(config_file, root, issue):
    processes = load_block(config_file)
    if registry_path(root, issue).is_file():
        previous = read_registry(root, issue)
        require(not any(same_process(entry) for entry in previous['processes']),
                f'Já há processos em execução para a issue {issue}: rode stop antes de um novo start.',
                category=USAGE)
    started = []
    try:
        for item in processes:
            cwd = process_cwd(root, item)
            guard_batch(item['argv'], item['name'])
            try:
                child = spawn(item['argv'], cwd)
            except OSError:
                raise Refusal(f'Não foi possível iniciar o processo {item["name"]}.', item['name'])
            started.append({'name': item['name'], 'pid': child.pid, 'port': process_port(item),
                            'url': item['health'], 'startedAt': now_iso(),
                            'identity': process_identity(child.pid) or UNKNOWN_IDENTITY})
            wait_healthy(child, item)
    except BaseException as failure:
        left = []
        for entry in reversed(started):
            try:
                kill_tree(entry['pid'])
            except Exception:
                left.append(f'{entry["name"]} (pid {entry["pid"]})')
        if left and isinstance(failure, Refusal):
            raise Refusal(f'{failure} Não foi possível derrubar: {", ".join(left)}.',
                          failure.process, failure.category)
        raise
    write_json_atomic({'issue': issue, 'root': str(Path(root).resolve()), 'processes': started},
                      registry_path(root, issue))
    return {'ok': True, 'issue': issue, 'processes': started}


def read_registry(root, issue):
    path = registry_path(root, issue)
    require(path.is_file(), f'Não há registro de processos para a issue {issue} nesta worktree.')
    try:
        record = json.loads(path.read_text(encoding='utf-8'))
        require(isinstance(record.get('processes'), list), 'registro inválido')
        for entry in record['processes']:
            require(isinstance(entry.get('pid'), int), 'registro inválido')
    except (OSError, ValueError, AttributeError, Refusal):
        raise Refusal(f'O registro da issue {issue} está ilegível ou inválido.')
    return record


def status(root, issue):
    record = read_registry(root, issue)
    processes = [scrub(dict(entry, alive=same_process(entry))) for entry in record['processes']]
    return {'ok': True, 'issue': issue, 'running': all(entry['alive'] for entry in processes),
            'processes': processes}


def stop(root, issue):
    record = read_registry(root, issue)
    stopped = []
    for entry in record['processes']:  # check every identity first, so a refusal kills nothing
        if pid_alive(entry['pid']):
            recorded, current = entry.get('identity'), process_identity(entry['pid'])
            require(recorded not in (None, UNKNOWN_IDENTITY) and current is not None,
                    f'Identidade desconhecida do processo {entry.get("name")} (pid {entry["pid"]}): '
                    'não foi encerrado para não derrubar um processo alheio; confira e encerre manualmente.')
    for entry in record['processes']:
        item = {'name': entry.get('name'), 'pid': entry['pid']}
        if not pid_alive(entry['pid']):
            stopped.append(dict(item, stopped=True))
        elif process_identity(entry['pid']) != entry.get('identity'):
            # the pid now belongs to another process: the recorded one is already gone
            stopped.append(dict(item, stopped=True, reused=True))
        else:
            # Limit: only the recorded root pid is confirmed dead; grandchildren rely on taskkill /T
            # (Windows) or the process group (POSIX), since the standard library cannot list them.
            kill_tree(entry['pid'])
            deadline = time.monotonic() + 15
            while pid_alive(entry['pid']) and time.monotonic() < deadline:
                time.sleep(POLL_SECONDS)
            stopped.append(dict(item, stopped=not pid_alive(entry['pid'])))
    require(all(item['stopped'] for item in stopped),
            'Nem todos os processos foram encerrados; o registro foi mantido para nova tentativa.')
    registry_path(root, issue).unlink()
    return {'ok': True, 'issue': issue, 'processes': stopped}


def run(operation, config_file, root, issue):
    if operation == 'start':
        return start(config_file, root, issue)
    if operation == 'status':
        return status(root, issue)
    return stop(root, issue)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('operation', choices=('start', 'stop', 'status'))
    parser.add_argument('--config', required=True)
    parser.add_argument('--root', required=True)
    parser.add_argument('--issue', required=True, type=int)
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    try:
        result = run(args.operation, args.config, args.root, args.issue)
        code = 0
    except Refusal as refusal:
        result = {'ok': False, 'error': protect(str(refusal)), 'category': refusal.category}
        if refusal.process:
            result['failedProcess'] = refusal.process
        code = 1
    print(protect(json.dumps(scrub(result), ensure_ascii=False)))
    return code


if __name__ == '__main__':
    sys.exit(main())
