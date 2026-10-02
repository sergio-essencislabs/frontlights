#!/usr/bin/env python3
"""Start, inspect and stop the processes of one issue's browser test environment.

Helper behind the browser-test route of /frontlights. It reads the `browserTest` block of the
project's `.frontlights/config.json` and brings up each process in the issue's worktree.

Operations (each prints one JSON object on stdout):
  start   spawn every process, wait for its health URL (HTTP 2xx/3xx, polled until a deadline)
          and record pids and times in `<root>/.frontlights/serve/<issue>.json`.
  status  read that record and check whether each pid is still alive.
  stop    kill the whole process tree of each recorded process, remove the record and release the issue's
          port reservations (`reservas_liberadas`; also when there is no record, which still reports a refusal).

Automatic ports: a process declares `"port": "auto"` (the only rule: omitting `port` keeps the #9 behaviour,
where the port is read from the health URL). Its `argv` elements and `health` may use `{port}`; with "auto" the
health URL must carry it in place of the port (`http://127.0.0.1:{port}/`). For each such process start reserves a
free port (the OS offers one, the bind must succeed, and it must not be reserved by a live start or process of
another issue or worktree), records it
exclusively (O_EXCL) in `<git-common-dir>/frontlights-serve/ports/<port>.json` (shared by all worktrees of the
repository; outside Git: `<root>/.frontlights/serve/ports/`), and gives the process the environment variables
`PORT` and `FRONTLIGHTS_PORT_<NAME>` (name upper-cased, non-alphanumerics as `_`). A reservation whose coordinator
and process are both dead is an orphan and is reused, except for a grace of 60 s when the coordinator died after
marking the reservation `spawning` and before recording the child pid. `stop` and a failed start release the issue's
reservations.
A process that ignores its port fails the start as an infrastructure failure, naming the process and the port.
All the ports of one start are reserved under one acquisition of a short lock on `.reserve.lock`, reentrant for the
same start. It is an operating-system lock (Windows `msvcrt.locking` on one byte, POSIX `fcntl.flock`) taken on a file
that is created if missing and never moved nor deleted, so there is no window in which the lock is absent; the system
releases it by itself when the owner dies (even by kill -9), hence no stale-lock or pid logic. It is retried until
`RESERVE_LOCK_SECONDS` and then refused, in every path of the loop. It does not depend on hard links, so it works on
FAT/exFAT. A reservation file that cannot be read (or whose pid is not an integer from 1 to 2**31-1) is treated as
being written by its owner (alive) while it is recent, and as an orphan once it is older than 60 s. A port counts as
busy when it cannot be bound on 127.0.0.1, 0.0.0.0, 127.0.0.2 (when the platform has it) or (when IPv6 works) ::1 and
::, or when something already accepts connections there.
`stop` takes the same per-issue lock as `start` (`<issue>.lock`): while a start of that issue is in progress in the
same worktree, `stop` is refused instead of releasing the reservations the start is still using. That lock is
created, judged and (when its owner is dead, or it is empty and older than `EMPTY_LOCK_SECONDS`, the leftover of an
owner killed before writing its pid) replaced under a short operating-system lock on
`.frontlights/serve/.locks.guard`, so two callers never both replace the same stale lock. Its retry loop checks the
deadline on every pass.
Reservations are shared only among the worktrees of ONE repository (they live in that repository's Git common
directory): two different repositories, or a project outside Git, do not see each other's reservations, so both may be
offered the same candidate port until one of the servers binds it (the bind/connect probe then skips it). The start
output only says whether the reservations are shared (`reservas_compartilhadas`), not with whom. The race between two
repositories (or a project outside Git) is not prevented: when both are offered the same candidate port, the one that
loses it fails its start as an infrastructure failure (the process cannot bind the port) and can start again.
An I/O error while creating or writing a lock, a reservation or a record is an infrastructure refusal with JSON
(never a traceback), and the issue lock is released; `main` turns any other OSError that escapes into the same
refusal. A config error is refused as `uso` before anything is reserved or spawned: a health URL or `port` outside
1 to 65535 or of the wrong type, credentials in the health URL, an invalid `timeoutSeconds`, a NUL character in the
`argv`, `health` or `cwd`, and a `cwd` that is not text, is absolute, leaves the worktree or does not exist.
Limit of the `spawning` grace: between spawning the process and recording its pid in the reservation (the pid is known
only once Popen returns) the reservation holds for 60 s on the `spawning` mark alone. If the coordinator is killed in
that window and the server only starts listening after the 60 s, another start may be offered its port; a server that
listens sooner is seen by the bind/connect probe.
The variable name is the process name upper-cased with non-alphanumerics as `_`, so two `auto` processes named
`web-api` and `web_api` would get the same `FRONTLIGHTS_PORT_WEB_API`: such a config is refused (category `uso`).
Processes with a fixed port or without `port` do not receive the variable, so their names may collide.
The start output says whether the reservations are shared (`reservas_compartilhadas`).

Exit codes: 0 done; 1 refusal or failure (a failed start tears down whatever already came up and
names the failing process, classified as an infrastructure failure).

Processes are started from an argv list, never through a shell. Logins and passwords from the
config are never read into the record and are redacted from every string this helper prints.
"""

import argparse
import contextlib
import datetime as dt
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

POLL_SECONDS = 0.2
DEFAULT_TIMEOUT = 60
PORT_AUTO = 'auto'
INFRASTRUCTURE = 'infraestrutura'
USAGE = 'uso'
UNKNOWN_IDENTITY = 'desconhecida'
BATCH_UNSAFE = '&|^%<>!"'
MIN_SECRET = 4
MASK = '[redacted]'
KILL_WAIT_SECONDS = 15
PORT_ATTEMPTS = 200
RESERVE_LOCK_SECONDS = 20
ORPHAN_SECONDS = 60
EMPTY_LOCK_SECONDS = 5
PERSISTENT_ERRORS = 25
MAX_PID = 2 ** 31 - 1
REMOVE_ATTEMPTS = 40
CONNECT_SECONDS = 0.05

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


def io_failure(action, error):
    """A Refusal (infrastructure) for an I/O error: the user gets a message and JSON, never a traceback."""
    detail = error.strerror or type(error).__name__
    return Refusal(f'Falha de entrada e saída ao {action}: {detail}. Confira o espaço em disco, as permissões e '
                   'se o caminho é mesmo uma pasta ou um arquivo comum; depois tente de novo.')


@contextlib.contextmanager
def io_guard(action):
    """Turn any OSError raised inside the block into an infrastructure Refusal (see io_failure)."""
    try:
        yield
    except OSError as error:
        raise io_failure(action, error) from error


def count_denial(path, error, denied):
    """Count consecutive denials to create `path` while it does not exist; the last one is an infrastructure failure.

    A file being deleted can answer "denied" for a few milliseconds on Windows; `PERSISTENT_ERRORS` denials in a row
    with nothing there to compete are a real permission problem, not contention.
    """
    if os.path.exists(path):
        return 0
    if denied + 1 >= PERSISTENT_ERRORS:
        raise io_failure(f'criar {Path(path).name}', error) from error
    return denied + 1


def now_iso():
    return dt.datetime.now().astimezone().isoformat()


def protect(text):
    """Mask every known secret in one regex pass, longest first (never feeds on its own mask)."""
    found_secrets = sorted({secret for secret in _SECRETS if secret}, key=len, reverse=True)
    if not found_secrets:
        return text
    # the mask FIRST, replaced by itself (so masking twice changes nothing, even for a secret that is a prefix
    # of the mask), then the secrets from the longest to the shortest
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
    with io_guard(f'gravar {path.name}'):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.parent / f'.{path.name}.{secrets.token_hex(8)}.tmp'
        try:
            temporary.write_text(json.dumps(scrub(value, REGISTRY_EXACT), indent=2, ensure_ascii=False),
                                 encoding='utf-8')
            for attempt in range(REMOVE_ATTEMPTS):
                try:
                    os.replace(temporary, path)
                    break
                except PermissionError:  # Windows: a reader holds the target open for a moment
                    if attempt == REMOVE_ATTEMPTS - 1:
                        raise
                    time.sleep(min(0.002 * (attempt + 1), 0.05))
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
                    require(MASK not in value,
                            f'O {key} de um usuário em browserTest.users contém o texto {MASK}, que colide com o '
                            'marcador de máscara usado na saída e nos registros e impediria mascará-lo por inteiro. '
                            f'Use um {key} de teste sem esse texto.', category=USAGE)
                    require(len(value) >= MIN_SECRET,
                            f'O {key} de um usuário em browserTest.users tem menos de {MIN_SECRET} caracteres: '
                            'um segredo curto não pode ser mascarado com segurança na saída e nos registros. '
                            f'Use um {key} de teste com {MIN_SECRET} ou mais caracteres.', category=USAGE)
                    _SECRETS.extend([value, urllib.parse.quote(value, safe=''), urllib.parse.quote_plus(value)])
    processes = block.get('processes')
    require(isinstance(processes, list) and processes, 'browserTest.processes precisa ser uma lista não vazia.')
    names, variables = set(), {}
    for item in processes:
        require(isinstance(item, dict), 'Cada item de browserTest.processes precisa ser um objeto.')
        name = item.get('name')
        require(isinstance(name, str) and name and name not in names,
                'Cada processo precisa de um name único e não vazio.')
        names.add(name)
        argv = item.get('argv')
        texts = [item.get('health'), item.get('cwd')] + (argv if isinstance(argv, list) else [])
        require(not any(isinstance(text, str) and '\0' in text for text in texts),
                f'O processo {name} tem um caractere NUL no argv, no health ou no cwd: remova-o do config.', name,
                USAGE)
        require(isinstance(argv, list) and argv and all(isinstance(part, str) and part for part in argv),
                f'O processo {name} precisa de argv como lista de strings (sem shell embutido).', name)
        health = item.get('health')
        try:
            parts = urllib.parse.urlsplit(health) if isinstance(health, str) else None
        except ValueError:  # e.g. an unterminated IPv6 literal
            parts = None
        require(parts and parts.scheme in ('http', 'https') and parts.hostname,
                f'O processo {name} precisa de health como URL http(s).', name, USAGE)
        require('port' not in item or item['port'] == PORT_AUTO
                or isinstance(item['port'], int) and not isinstance(item['port'], bool),
                f'O processo {name} tem port inválida: use um inteiro ou "{PORT_AUTO}".', name, USAGE)
        require('@' not in parts.netloc, f'O health do processo {name} não pode ter usuário e senha na URL.', name,
                USAGE)
        timeout = item.get('timeoutSeconds', DEFAULT_TIMEOUT)
        require(isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and timeout > 0,
                f'O processo {name} tem timeoutSeconds inválido: use um número maior que zero.', name, USAGE)
        require(item.get('cwd') is None or isinstance(item['cwd'], str),
                f'O cwd do processo {name} precisa ser um texto (caminho relativo à worktree).', name, USAGE)
        # refused here, before anything is spawned: a port outside 1..65535 would otherwise fail after the spawn
        require(not isinstance(item.get('port'), int) or valid_port(item['port']),
                f'O processo {name} tem port fora da faixa de 1 a 65535.', name, USAGE)
        if not is_auto(item):  # an auto health URL carries {port}, filled in only after the reservation
            require(url_port(parts) is not None,
                    f'O health do processo {name} tem uma porta inválida: use uma porta de 1 a 65535 na URL, ou '
                    f'port "{PORT_AUTO}".', name, USAGE)
        if is_auto(item):  # only these receive FRONTLIGHTS_PORT_<NAME>, so only they can collide
            require(parts.netloc.endswith(':{port}'),
                    f'O processo {name} usa port "{PORT_AUTO}", então o health precisa ter {{port}} no lugar da '
                    'porta (por exemplo http://127.0.0.1:{port}/): sem isso a saúde não prova que o processo '
                    'escutou a porta reservada.', name, USAGE)
            variable = env_name(name)
            require(variable not in variables,
                    f'Os processos {variables.get(variable)} e {name} geram a mesma variável {variable} depois de '
                    'normalizar o nome (maiúsculas, e tudo que não é letra ou número vira "_"). Renomeie um deles.',
                    name, USAGE)
            variables[variable] = name
    return processes


def valid_port(value):
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 65535


def url_port(parts):
    """Port of a parsed http(s) URL (the scheme default when absent), or None when it is not 1..65535."""
    try:
        port = parts.port
    except ValueError:
        return None
    if port is None:
        return 443 if parts.scheme == 'https' else 80
    return port if valid_port(port) else None


def is_auto(item):
    return item.get('port') == PORT_AUTO


def env_name(name):
    return 'FRONTLIGHTS_PORT_' + re.sub(r'[^A-Z0-9]', '_', name.upper())


def with_port(item, port):
    """Copy of the process with {port} replaced in every argv element and in the health URL."""
    token = str(port)
    return dict(item, port=port, autoPort=True, argv=[part.replace('{port}', token) for part in item['argv']],
                health=item['health'].replace('{port}', token))


def process_port(item):
    if isinstance(item.get('port'), int):
        return item['port']
    return url_port(urllib.parse.urlsplit(item['health']))


def process_cwd(root, item):
    base = Path(root).resolve()
    target = (base / item['cwd']).resolve() if item.get('cwd') else base
    require(target == base or base in target.parents,
            f'O cwd do processo {item["name"]} precisa ficar dentro da worktree da issue.', item['name'], USAGE)
    require(target.is_dir(), f'O cwd do processo {item["name"]} não existe.', item['name'], USAGE)
    return target


# ---------------------------------------------------------------- process control

def spawn(argv, cwd, env=None):
    executable = shutil.which(argv[0])
    if executable is None:
        raise OSError('executável não encontrado')
    options = {'cwd': str(cwd), 'stdin': subprocess.DEVNULL, 'stdout': subprocess.DEVNULL,
               'stderr': subprocess.DEVNULL}
    if env:
        options['env'] = dict(os.environ, **env)
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
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError):
        return False


def wait_healthy(child, item):
    timeout = item.get('timeoutSeconds', DEFAULT_TIMEOUT)  # validated by load_block, before anything is spawned
    reserved = f' (porta reservada {item["port"]})' if item.get('autoPort') else ''
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise Refusal(f'O processo {item["name"]} terminou antes de ficar saudável{reserved} '
                          f'(código {child.returncode}).', item['name'])
        if healthy(item['health']):
            return
        time.sleep(POLL_SECONDS)
    raise Refusal(f'O processo {item["name"]} não ficou saudável em {timeout} s{reserved}. Se ele ignora a porta '
                  'reservada, faça-o ler PORT, a variável FRONTLIGHTS_PORT_<NOME> ou {port} no argv.'
                  if reserved else f'O processo {item["name"]} não ficou saudável em {timeout} s.', item['name'])


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


def valid_pid(value):
    """True for an integer (not a bool) from 1 to 2**31-1: the only values a real pid can have."""
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= MAX_PID


def pid_alive(pid):
    if not valid_pid(pid):
        return False
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
    """Create <issue>.lock exclusively; a lock of a dead owner is replaced, an unreadable one refused.

    `start` and `stop` of the issue both take it. Creating, judging and replacing it happen under a short
    operating-system lock on `.frontlights/serve/.locks.guard` (never moved nor deleted): two callers can never both
    replace the same stale lock, and nobody reads a lock that is still being written. A lock that is EMPTY and older
    than `EMPTY_LOCK_SECONDS` is the leftover of an owner killed between creating and writing it (the creator holds
    the guard across both steps, so under the guard an empty lock has no live writer): it is replaced like the lock of
    a dead owner; a recent empty one is waited for until the deadline. Any other content that is not a pid stays
    refused. The loop has a single exit path per pass, the deadline check followed by a short sleep: no branch (a
    lock being deleted by its owner on Windows, a replacement that did not remove the file, an empty lock) skips it,
    so the call always ends within `RESERVE_LOCK_SECONDS` (the wait for the guard counts in it), plus the retries of
    one removal. An I/O error becomes an infrastructure refusal, and a lock created but not written is removed again;
    so does a denial that persists with no lock file there (`count_denial`), instead of waiting for the deadline.
    """
    path = lock_path(root, issue)
    name = relative(Path(root), path)
    with io_guard('criar a pasta de travas'):
        path.parent.mkdir(parents=True, exist_ok=True)
    unreadable = (f'O arquivo de trava {name} está ilegível, então não dá para saber se outro start ou stop está em '
                  f'andamento. Se nenhum start ou stop da issue {issue} estiver rodando, apague {name} à mão e tente '
                  'de novo.')
    deadline = time.monotonic() + RESERVE_LOCK_SECONDS
    guard = take_os_lock(path.parent / '.locks.guard',
                         f'Não foi possível obter a trava {name} em {RESERVE_LOCK_SECONDS} s: outro start ou stop '
                         'a mantém. Tente de novo em instantes.')
    denied = 0
    try:
        while True:
            try:
                descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                descriptor = None
            except PermissionError as error:  # Windows: being deleted by its owner, unless it never goes away
                descriptor = None
                denied = count_denial(path, error, denied)
            except OSError as error:
                raise io_failure('criar a trava', error) from error
            if descriptor is not None:
                try:
                    with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
                        handle.write(str(os.getpid()))
                except OSError as error:
                    remove_file(path)  # never leave an empty lock behind
                    raise io_failure('gravar a trava', error) from error
                return path
            try:
                text = path.read_text(encoding='utf-8')
            except (FileNotFoundError, PermissionError):
                text = None  # released (or being deleted) between the failed create and the read: try again
            except (OSError, ValueError):
                raise Refusal(unreadable, category=USAGE)
            if text is not None and not text.strip():
                if older_than(path, EMPTY_LOCK_SECONDS):
                    remove_file(path)  # the owner died before writing its pid; under the guard nobody else writes
            elif text is not None:
                owner = int(text) if re.fullmatch(r'\s*[0-9]{1,10}\s*', text) else None
                require(valid_pid(owner), unreadable, category=USAGE)
                require(not pid_alive(owner),
                        f'Outro start ou stop da issue {issue} está em andamento (pid {owner}). Aguarde-o terminar; '
                        f'se ele já não existe, apague {name} à mão.', category=USAGE)
                remove_file(path)  # dead owner: under the guard nobody else replaces it meanwhile
            require(time.monotonic() < deadline,
                    f'Não foi possível obter a trava {name}; tente de novo. Se o arquivo continua vazio, com data '
                    f'no futuro (relógio adiantado), apague {name} à mão.', category=USAGE)
            time.sleep(0.02)
    finally:
        release_os_lock(guard)


def release_lock(path):
    remove_file(path)


def write_registry(root, issue, entries):
    write_json_atomic({'issue': issue, 'root': str(Path(root).resolve()), 'processes': entries},
                      registry_path(root, issue))


def ports_location(root):
    """Folder for port reservations and whether it is shared by every worktree of the repository.

    It lives in the Git common directory (`frontlights-serve/ports`), so worktrees of one repository see each
    other's reservations. Without Git (or when Git cannot say) it falls back to `<root>/.frontlights/serve/ports`
    and the second value is False.
    """
    for flags in (['--path-format=absolute', '--git-common-dir'], ['--git-common-dir']):
        try:
            done = subprocess.run(['git', '-C', str(root), 'rev-parse', *flags], capture_output=True, text=True,
                                  encoding='utf-8', timeout=30)
        except (OSError, subprocess.SubprocessError):
            continue
        text = done.stdout.strip() if done.returncode == 0 else ''
        if text:
            common = Path(text)
            if not common.is_absolute():
                common = Path(root) / common
            if common.is_dir():
                return common / 'frontlights-serve' / 'ports', True
    return Path(root) / '.frontlights' / 'serve' / 'ports', False


def ports_dir(root):
    return ports_location(root)[0]


def candidate_ports():
    """Ports offered by the operating system (bind to port 0), one per attempt."""
    for _ in range(PORT_ATTEMPTS):
        with io_guard('pedir uma porta ao sistema'):
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
        yield port  # offered only after the probe socket is closed, so bindable() can test it


_IPV6 = []
_ALIAS = []
LOOPBACK_ALIAS = '127.0.0.2'


def ipv6_usable():
    if not _IPV6:
        try:
            with socket.socket(socket.AF_INET6) as sock:
                sock.bind(('::1', 0))
            _IPV6.append(True)
        except (OSError, AttributeError):
            _IPV6.append(False)
    return _IPV6[0]


def loopback_alias_usable():
    """True when the platform has the second loopback address (127.0.0.2); macOS has it only when configured."""
    if not _ALIAS:
        try:
            with socket.socket(socket.AF_INET) as sock:
                sock.bind((LOOPBACK_ALIAS, 0))
            _ALIAS.append(True)
        except OSError:
            _ALIAS.append(False)
    return _ALIAS[0]


def bindable(port):
    """True when nothing else holds the port: it binds on every local address and nothing accepts connections.

    Addresses: 127.0.0.1, 0.0.0.0 and the loopback alias 127.0.0.2 (when the platform has it) and, when IPv6 works,
    ::1 and ::. A listener on any of them (or one the bind cannot see, like a specific address next to a wildcard on
    Windows) makes the port busy. Limit: a listener on another specific address (say 127.0.0.3) is seen only on
    platforms where the wildcard bind already fails because of it; the 127/8 block cannot be enumerated.
    """
    families = [(socket.AF_INET, '127.0.0.1'), (socket.AF_INET, '0.0.0.0')]
    connects = [(socket.AF_INET, '127.0.0.1')]
    if loopback_alias_usable():
        families.append((socket.AF_INET, LOOPBACK_ALIAS))
        connects.append((socket.AF_INET, LOOPBACK_ALIAS))
    if ipv6_usable():
        families += [(socket.AF_INET6, '::1'), (socket.AF_INET6, '::')]
        connects.append((socket.AF_INET6, '::1'))
    for family, address in families:
        try:
            with socket.socket(family) as sock:
                sock.bind((address, port))
        except OSError:
            return False
    for family, address in connects:
        with socket.socket(family) as sock:
            sock.settimeout(CONNECT_SECONDS)
            if sock.connect_ex((address, port)) == 0:
                return False
    return True


def read_brief(path, limit=65536):
    """Read a small file and close it at once (os.open without delete-sharing would block a delete for longer)."""
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_BINARY', 0))
    try:
        return os.read(descriptor, limit).decode('utf-8', errors='replace')
    finally:
        os.close(descriptor)


def remove_file(path):
    """Delete a file, retrying a few ms (Windows refuses while a reader has it open). True when it is gone."""
    for attempt in range(REMOVE_ATTEMPTS):
        try:
            os.unlink(path)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            time.sleep(min(0.002 * (attempt + 1), 0.05))
    return False


def older_than(path, seconds):
    try:
        return time.time() - os.stat(path).st_mtime > seconds
    except OSError:
        return False


def older_than_orphan(path):
    return older_than(path, ORPHAN_SECONDS)


_LOCAL_LOCK = threading.RLock()
_HELD = {}


def try_os_lock(descriptor):
    """Try once to take the exclusive operating-system lock on the file; True when taken."""
    if os.name == 'nt':
        import msvcrt
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def release_os_lock(descriptor):
    """Unlock and close; closing alone would also release it, so a failed unlock is not an error."""
    try:
        if os.name == 'nt':
            import msvcrt
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def take_os_lock(path, message):
    """Take the exclusive operating-system lock on `path` (retrying until the deadline) and return its descriptor.

    The file is created if missing (without O_EXCL) and never moved or deleted, so the lock is always on the same
    file and needs no hard links (FAT/exFAT work). The system releases it when the owner dies, even by kill -9.
    Every failed attempt (busy lock, or a file another process holds open without sharing) reaches the deadline
    check, which refuses with `message`. The descriptor is closed on every path that does not return it. An error
    that keeps opening the file (a folder in its place, a denied permission) is an infrastructure refusal with the
    reason after `PERSISTENT_ERRORS` tries in a row, not a wait for the deadline; an existing file that is held open
    by someone else is contention and keeps waiting.
    """
    deadline = time.monotonic() + RESERVE_LOCK_SECONDS
    failures = 0
    while True:
        descriptor = None
        try:
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_BINARY', 0))
            failures = 0
            if try_os_lock(descriptor):
                taken, descriptor = descriptor, None
                return taken
        except OSError as error:
            # a regular file that cannot be opened is held by someone else (Windows: no sharing): contention
            failures = 0 if os.path.isfile(path) else failures + 1
            if failures >= PERSISTENT_ERRORS:
                raise io_failure(f'abrir {Path(path).name}', error) from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
        require(time.monotonic() < deadline, message)
        time.sleep(0.02)


def take_reserve_file(directory):
    """Take the operating-system lock on `.reserve.lock` of the reservation folder and return its descriptor."""
    return take_os_lock(directory / '.reserve.lock',
                        f'Não foi possível obter a trava de reserva de portas em {RESERVE_LOCK_SECONDS} s: outra '
                        'reserva a mantém. O sistema libera a trava sozinho quando o dono termina; tente de novo em '
                        'instantes.')


@contextlib.contextmanager
def reservation_lock(directory):
    """Short exclusive lock so choosing and recording ports is one step across processes and threads.

    Reentrant: a start that reserves several ports holds it once and never waits for itself.
    """
    directory = Path(directory)
    with io_guard('criar a pasta de reservas de portas'):
        directory.mkdir(parents=True, exist_ok=True)
    key = str(directory)
    with _LOCAL_LOCK:
        if _HELD.get(key):
            _HELD[key] += 1
            try:
                yield
            finally:
                _HELD[key] -= 1
            return
        descriptor = take_reserve_file(directory)
        _HELD[key] = 1
        try:
            yield
        finally:
            _HELD.pop(key, None)
            release_os_lock(descriptor)


def read_reservation(path):
    try:
        record = json.loads(read_brief(path))
        return record if isinstance(record, dict) and valid_pid(record.get('pid')) else None
    except (OSError, ValueError):
        return None


def reservation_live(path):
    """A reservation holds while its coordinator (a running start) or its started process is alive.

    Unreadable (a start that crashed between creating and writing it, or one still writing): alive while the
    file is recent, an orphan once it is older than 60 s.
    """
    record = read_reservation(path)
    if record is None:
        return not older_than_orphan(path)
    child = record.get('child')
    if same_process(record) or (isinstance(child, dict) and valid_pid(child.get('pid'))
                                and same_process(child)):
        return True
    # a coordinator that died between spawning the process and recording its pid leaves a process nobody tracks:
    # the "spawning" mark keeps the port for a grace period (a start that never got to spawn leaves no mark)
    return bool(record.get('spawning')) and not isinstance(child, dict) and not older_than_orphan(path)


def reserve_port(root, issue, name):
    """Reserve a free port for the process: record it exclusively and return it (the reservation path too).

    An I/O error (the system offers no candidate port, the record cannot be written) is an infrastructure refusal,
    and a record created but not written is removed again.
    """
    directory = ports_dir(root)
    denied = 0
    with reservation_lock(directory):
        for port in candidate_ports():
            path = directory / f'{port}.json'
            if path.exists():
                if reservation_live(path):
                    continue  # reserved by a live start/issue (maybe of another worktree)
                if not remove_file(path):
                    continue
            if not bindable(port):
                continue  # another program holds it
            record = {'issue': issue, 'process': name, 'root': str(Path(root).resolve()), 'pid': os.getpid(),
                      'identity': process_identity(os.getpid()) or UNKNOWN_IDENTITY, 'reservedAt': now_iso()}
            try:
                descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                continue
            except PermissionError as error:  # Windows: the file is being deleted, unless it never goes away
                denied = count_denial(path, error, denied)
                continue
            except OSError as error:
                raise io_failure('criar a reserva da porta', error) from error
            try:
                with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
                    handle.write(json.dumps(scrub(record, REGISTRY_EXACT), indent=2, ensure_ascii=False))
            except OSError as error:
                remove_file(path)  # never leave an empty reservation behind
                raise io_failure('gravar a reserva da porta', error) from error
            return port
    raise Refusal(f'Não foi possível reservar uma porta livre para o processo {name}.')


def reserve_ports(root, issue, names):
    """Reserve one port per name under a single acquisition of the lock; returns {name: port}."""
    with reservation_lock(ports_dir(root)):
        return {name: reserve_port(root, issue, name) for name in names}


def release_ports(root, issue, keep=()):
    """Remove this issue's reservations made from this worktree (except the ports in `keep`); returns the ports."""
    directory = ports_dir(root)
    here = str(Path(root).resolve())
    kept = {str(port) for port in keep}
    released = []
    try:
        paths = sorted(directory.glob('*.json')) if directory.is_dir() else []
    except OSError:  # an unreadable folder: nothing is reported as released; the reservations of a dead start are
        paths = []  # reclaimed as orphans by the next reserve
    for path in paths:
        record = read_reservation(path)
        if record and record.get('issue') == issue and record.get('root') == here and path.stem not in kept:
            if remove_file(path) and path.stem.isdigit():
                released.append(int(path.stem))
    return sorted(released)


def mark_spawning(root, port):
    """Mark the reservation just before the process is spawned (see reservation_live)."""
    path = ports_dir(root) / f'{port}.json'
    record = read_reservation(path)
    if record is not None:
        record['spawning'] = now_iso()
        write_json_atomic(record, path)


def attach_child(root, port, child_entry):
    """Record the started process in its reservation, so it keeps holding after the start command exits."""
    path = ports_dir(root) / f'{port}.json'
    record = read_reservation(path)
    if record is not None:
        record['child'] = {'pid': child_entry['pid'], 'identity': child_entry['identity']}
        write_json_atomic(record, path)


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
    cwds = [process_cwd(root, item) for item in processes]  # a bad cwd is refused before anything is reserved
    started = []
    try:
        ports = reserve_ports(root, issue, [item['name'] for item in processes if is_auto(item)])
        for item, cwd in zip(processes, cwds):
            env = None
            if is_auto(item):
                port = ports[item['name']]
                env = {'PORT': str(port), env_name(item['name']): str(port)}
                item = with_port(item, port)
            guard_batch(item['argv'], item['name'])
            if env:
                mark_spawning(root, port)
            try:
                child = spawn(item['argv'], cwd, env)
            except (OSError, ValueError):  # ValueError: e.g. a NUL character in argv
                raise Refusal(f'Não foi possível iniciar o processo {item["name"]}.', item['name'])
            started.append({'name': item['name'], 'pid': child.pid, 'port': process_port(item),
                            'url': item['health'], 'startedAt': now_iso(),
                            'identity': process_identity(child.pid) or UNKNOWN_IDENTITY})
            if env:
                attach_child(root, port, started[-1])
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
        except (OSError, Refusal):  # best effort: the original failure is what the user must see
            pass
        release_ports(root, issue, keep=[entry['port'] for entry in left])
        if left and isinstance(failure, Refusal):
            names = ', '.join(f'{entry["name"]} (pid {entry["pid"]})' for entry in left)
            raise Refusal(f'{failure} Não foi possível derrubar: {names}; rode stop para tentar de novo.',
                          failure.process, failure.category,
                          {'left': [{'name': entry['name'], 'pid': entry['pid']} for entry in left]})
        raise
    return {'ok': True, 'issue': issue, 'processes': started,
            'reservas_compartilhadas': ports_location(root)[1]}


def read_registry(root, issue):
    path = registry_path(root, issue)
    name = relative(Path(root), path)
    require(path.is_file(), f'Não há registro de processos para a issue {issue} nesta worktree.')
    try:
        record = json.loads(path.read_text(encoding='utf-8'))
        require(isinstance(record.get('processes'), list), 'registro inválido')
        for entry in record['processes']:
            require(valid_pid(entry.get('pid')), 'registro inválido')
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
    """Stop the issue's processes under the same lock as start: a start in progress makes stop refuse."""
    lock = acquire_lock(root, issue)
    try:
        return stop_locked(root, issue)
    finally:
        release_lock(lock)


def stop_locked(root, issue):
    try:
        record = read_registry(root, issue)
    except Refusal as refusal:
        if not registry_path(root, issue).is_file():
            # no record at all (a start that died early): nothing to kill, but its reservations must not leak;
            # an unreadable record may still describe live processes, so then the reservations stay
            refusal.extra = dict(refusal.extra, reservas_liberadas=release_ports(root, issue))
        raise
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
    try:
        registry_path(root, issue).unlink()
    except OSError as error:
        detail = error.strerror or type(error).__name__
        raise Refusal(f'Os processos foram encerrados, mas não foi possível apagar o registro {name} ({detail}). '
                      'Apague-o à mão ou rode stop de novo.',
                      extra={'processes': stopped, 'reservas_liberadas': release_ports(root, issue)}) from error
    return {'ok': True, 'issue': issue, 'processes': stopped, 'reservas_liberadas': release_ports(root, issue)}


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
    except (Refusal, OSError) as failure:
        # the safety net: an I/O error that escaped every specific handler is still a refusal with JSON
        refusal = failure if isinstance(failure, Refusal) else io_failure('executar a operação', failure)
        result = {'ok': False, 'error': str(refusal), 'category': refusal.category}
        if refusal.process:
            result['failedProcess'] = refusal.process
        result.update(refusal.extra)
        code = 1
    print(json.dumps(scrub(result), ensure_ascii=False))  # the single masking pass of the output
    return code


if __name__ == '__main__':
    sys.exit(main())
