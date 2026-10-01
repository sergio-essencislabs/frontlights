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
import re
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
MIN_SECRET = 4
MASK = '[redacted]'
KILL_WAIT_SECONDS = 15

_SECRETS = []


class Refusal(Exception):
    """A deliberate refusal: the message is safe to show the user."""

    def __init__(self, message, process=None, category=None, extra=None):
        super().__init__(message)
        self.process = process
        self.category = category or INFRASTRUCTURE
        self.extra = extra or {}


def require(condition, message, process=None, category=None):
    if not condition:
        raise Refusal(message, process, category)


def now_iso():
    return dt.datetime.now().astimezone().isoformat()


def protect(text):
    """Mask every known secret in one regex pass, longest first (never feeds on its own mask)."""
    found_secrets = sorted({secret for secret in _SECRETS if secret}, key=len, reverse=True)
    if not found_secrets:
        return text
    # the mask itself is matched first and replaced by itself, so masking twice changes nothing
    pattern = '|'.join([re.escape(MASK)] + [re.escape(secret) for secret in found_secrets])
    return re.sub(pattern, lambda found: MASK, text)


def scrub(value, skip=()):
    """Mask the string VALUES of a JSON-like value; keys and structure stay untouched.

    `skip` lists dict keys whose values are technical data (e.g. process identity) that must stay exact.
    """
    if isinstance(value, str):
        return protect(value)
    if isinstance(value, list):
        return [scrub(item, skip) for item in value]
    if isinstance(value, dict):
        return {key: item if key in skip else scrub(item, skip) for key, item in value.items()}
    return value


REGISTRY_EXACT = ('identity', 'startedAt')


def write_json_atomic(value, path):
    """Write the value masked once, as valid JSON, replacing the file atomically."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f'.{path.name}.{secrets.token_hex(8)}.tmp'
    try:
        temporary.write_text(json.dumps(scrub(value, REGISTRY_EXACT), indent=2, ensure_ascii=False),
                             encoding='utf-8')
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
    _SECRETS.clear()
    for user in block.get('users') or []:
        if isinstance(user, dict):
            for key in ('login', 'password'):
                value = user.get(key)
                if isinstance(value, str) and value:
                    require(len(value) >= MIN_SECRET,
                            f'O {key} de um usuário em browserTest.users tem menos de {MIN_SECRET} caracteres: '
                            'um segredo curto não pode ser mascarado com segurança na saída e nos registros. '
                            f'Use um {key} de teste com {MIN_SECRET} ou mais caracteres.', category=USAGE)
                    _SECRETS.extend([value, urllib.parse.quote(value, safe=''), urllib.parse.quote_plus(value)])
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


def _kill_signal(pid):
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'], capture_output=True, timeout=60)
    else:
        import signal
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def kill_tree(pid):
    """Kill the process tree of pid and report whether the root is really gone afterwards.

    The exit status of taskkill/killpg is not trusted: pid_alive decides. Returns True when dead.
    """
    _kill_signal(pid)
    deadline = time.monotonic() + KILL_WAIT_SECONDS
    while pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(POLL_SECONDS)
    return not pid_alive(pid)


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


def lock_path(root, issue):
    return Path(root) / '.frontlights' / 'serve' / f'{issue}.lock'


def relative(root, path):
    return path.relative_to(root).as_posix()


def acquire_lock(root, issue):
    """Create <issue>.lock exclusively; a lock of a dead owner is replaced, an unreadable one refused."""
    path = lock_path(root, issue)
    path.parent.mkdir(parents=True, exist_ok=True)
    name = relative(Path(root), path)
    for _ in range(3):
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                text = path.read_text(encoding='utf-8')
                owner = int(text.strip())
            except FileNotFoundError:
                continue  # released between the failed create and the read: try again
            except (OSError, ValueError):
                raise Refusal(f'O arquivo de trava {name} está ilegível, então não dá para saber se outro start '
                              f'está em andamento. Se nenhum start da issue {issue} estiver rodando, apague '
                              f'{name} à mão e rode start de novo.', category=USAGE)
            require(not pid_alive(owner),
                    f'Outro start da issue {issue} está em andamento (pid {owner}). Aguarde-o terminar; se ele '
                    f'já não existe, apague {name} à mão.', category=USAGE)
            try:
                if path.read_text(encoding='utf-8') == text:  # still the stale lock we just judged
                    path.unlink()
            except OSError:
                pass
            continue
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            handle.write(str(os.getpid()))
        return path
    raise Refusal(f'Não foi possível obter a trava {name}; tente de novo.', category=USAGE)


def release_lock(path):
    try:
        Path(path).unlink()
    except OSError:
        pass


def write_registry(root, issue, entries):
    write_json_atomic({'issue': issue, 'root': str(Path(root).resolve()), 'processes': entries},
                      registry_path(root, issue))


def start(config_file, root, issue):
    processes = load_block(config_file)
    lock = acquire_lock(root, issue)
    try:
        return start_locked(processes, root, issue)
    finally:
        release_lock(lock)


def start_locked(processes, root, issue):
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
            write_registry(root, issue, started)  # a crash from here on leaves something stop can tear down
            wait_healthy(child, item)
    except BaseException as failure:
        left = []
        for entry in reversed(started):
            try:
                dead = kill_tree(entry['pid'])
            except Exception:
                dead = False
            if not dead:
                left.append(entry)
        try:
            if left:
                write_registry(root, issue, left)  # keep only what survived, so stop can retry
            else:
                registry_path(root, issue).unlink(missing_ok=True)
        except OSError:
            pass
        if left and isinstance(failure, Refusal):
            names = ', '.join(f'{entry["name"]} (pid {entry["pid"]})' for entry in left)
            raise Refusal(f'{failure} Não foi possível derrubar: {names}; rode stop para tentar de novo.',
                          failure.process, failure.category,
                          {'left': [{'name': entry['name'], 'pid': entry['pid']} for entry in left]})
        raise
    return {'ok': True, 'issue': issue, 'processes': started}


def read_registry(root, issue):
    path = registry_path(root, issue)
    name = relative(Path(root), path)
    require(path.is_file(), f'Não há registro de processos para a issue {issue} nesta worktree.')
    try:
        record = json.loads(path.read_text(encoding='utf-8'))
        require(isinstance(record.get('processes'), list), 'registro inválido')
        for entry in record['processes']:
            require(isinstance(entry.get('pid'), int), 'registro inválido')
    except (OSError, ValueError, AttributeError, Refusal):
        raise Refusal(f'O registro da issue {issue} ({name}) está ilegível ou inválido, então start, status e '
                      f'stop não conseguem usá-lo. Confira se há processos da issue em execução, encerre-os '
                      f'à mão e apague {name}.')
    return record


def status(root, issue):
    record = read_registry(root, issue)
    # main masks the output: a secret that sits in a health URL query shows up as [redacted] here too
    processes = [dict(entry, alive=same_process(entry)) for entry in record['processes']]
    return {'ok': True, 'issue': issue, 'running': all(entry['alive'] for entry in processes),
            'processes': processes}


def stop(root, issue):
    record = read_registry(root, issue)
    name = relative(Path(root), registry_path(root, issue))
    stopped, left = [], []
    for entry in record['processes']:  # check every identity first, so a refusal kills nothing
        if pid_alive(entry['pid']):
            recorded, current = entry.get('identity'), process_identity(entry['pid'])
            require(recorded not in (None, UNKNOWN_IDENTITY) and current is not None,
                    f'Identidade desconhecida do processo {entry.get("name")} (pid {entry["pid"]}): '
                    'o registro não prova que o pid ainda é o processo iniciado, então ele não foi encerrado '
                    'para não derrubar um processo alheio. Confira e encerre manualmente; depois apague '
                    f'{name} à mão.')
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
            try:
                dead = kill_tree(entry['pid'])
            except Exception:
                dead = False
            stopped.append(dict(item, stopped=dead))
            if not dead:
                left.append(item)
    if left:
        raise Refusal('Nem todos os processos foram encerrados; o registro foi mantido para nova tentativa.',
                      extra={'left': left, 'processes': stopped})
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
        result = {'ok': False, 'error': str(refusal), 'category': refusal.category}
        if refusal.process:
            result['failedProcess'] = refusal.process
        result.update(refusal.extra)
        code = 1
    print(json.dumps(scrub(result), ensure_ascii=False))  # the single masking pass of the output
    return code


if __name__ == '__main__':
    sys.exit(main())
