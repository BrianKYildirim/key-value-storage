"""Shared helpers and golden data for the test suite (standard library only)."""

import io
import json
import os
import re
import select
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import observability  # noqa: E402
import server  # noqa: E402

SERVER_PY = os.path.join(REPO, "server.py")
TIMEOUT = 10  # seconds; every blocking call in the tests is bounded so a deadlock fails instead of hanging
SUBPROCESS_ENV = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")  # keep __pycache__ out of the repository

# Recorded from the original server.py (git 7ccdff6) run with its defaults: each payload was sent
# with one sendall() and answered by one recv(1024), like client.py does.
GOLDEN_SESSION = [
    (b"PRINT", b"Store is empty.\n"),
    (b"GET a", b"Key 'a' not found."),
    (b"SET a 1", b"Added key 'a' with value '1'\n"),
    (b"set B two", b"Added key 'B' with value 'two'\n"),
    (b"  GeT   a  ", b"1"),
    (b"SET a 2", b"Added key 'a' with value '2'\n"),
    (b"Set c\t3", b"Added key 'c' with value '3'\n"),
    (b"GET b", b"Key 'b' not found."),
    (b"PRINT", b"[KEY]: a\t[VALUE]: 2\n[KEY]: B\t[VALUE]: two\n[KEY]: c\t[VALUE]: 3\n"),
    (b"SET a", b"ERROR: SET command requires 2 arguments: key and value\n"),
    (b"SET a b c", b"ERROR: SET command requires 2 arguments: key and value\n"),
    (b"GET", b"ERROR: GET command requires 1 argument: key\n"),
    (b"GET a b", b"ERROR: GET command requires 1 argument: key\n"),
    (b"REMOVE", b"ERROR: REMOVE command requires 1 argument: key\n"),
    (b"REMOVE zzz", b"Key 'zzz' not found."),
    (b"remove B", b"Removed key 'B'.\n"),
    (b"FOO bar", b"ERROR: Unknown command 'FOO'\n"),
    (b"   ", b"ERROR: Empty command\n"),
    (b"\r\n", b"ERROR: Empty command\n"),
    (b"quit now", b"ERROR: Unknown command 'quit'\n"),
    (b"PRINT extra", b"[KEY]: a\t[VALUE]: 2\n[KEY]: c\t[VALUE]: 3\n"),
    ("SET ключ значение".encode(), "Added key 'ключ' with value 'значение'\n".encode()),
    (b"SET d 4\n", b"Added key 'd' with value '4'\n"),
    (b"PRINT", "[KEY]: a\t[VALUE]: 2\n[KEY]: c\t[VALUE]: 3\n[KEY]: ключ\t[VALUE]: значение\n"
               "[KEY]: d\t[VALUE]: 4\n".encode()),
]
GOLDEN_QUIT = b"QUIT"  # ends the golden session; the server closes the connection without replying
# What the original server wrote on Linux with a UTF-8 locale; the server now writes these bytes everywhere.
GOLDEN_STORE_TXT = "a\t2\nc\t3\nключ\tзначение\nd\t4\n".encode()
GOLDEN_RESTART_PRINT = GOLDEN_SESSION[-1][1]

# Human-readable stdout of the original server for: the golden session, a second connection sending
# "quit", a third that connects and closes, and a fourth sending undecodable bytes. "{N}" stands for
# the Nth client's address tuple; the "Server listening" line is checked separately (it now shows the
# port actually bound, which only differs from the original when the configured port is 0).
GOLDEN_STDOUT_TAIL = """\
Connection from {1}
Received from {1}: PRINT
Response sent to {1}: Store is empty.
Received from {1}: GET a
Response sent to {1}: Key 'a' not found.
Received from {1}: SET a 1
Response sent to {1}: Added key 'a' with value '1'
Received from {1}: set B two
Response sent to {1}: Added key 'B' with value 'two'
Received from {1}: GeT   a
Response sent to {1}: 1
Received from {1}: SET a 2
Response sent to {1}: Added key 'a' with value '2'
Received from {1}: Set c\t3
Response sent to {1}: Added key 'c' with value '3'
Received from {1}: GET b
Response sent to {1}: Key 'b' not found.
Received from {1}: PRINT
Response sent to {1}: [KEY]: a\t[VALUE]: 2
[KEY]: B\t[VALUE]: two
[KEY]: c\t[VALUE]: 3
Received from {1}: SET a
Response sent to {1}: ERROR: SET command requires 2 arguments: key and value
Received from {1}: SET a b c
Response sent to {1}: ERROR: SET command requires 2 arguments: key and value
Received from {1}: GET
Response sent to {1}: ERROR: GET command requires 1 argument: key
Received from {1}: GET a b
Response sent to {1}: ERROR: GET command requires 1 argument: key
Received from {1}: REMOVE
Response sent to {1}: ERROR: REMOVE command requires 1 argument: key
Received from {1}: REMOVE zzz
Response sent to {1}: Key 'zzz' not found.
Received from {1}: remove B
Response sent to {1}: Removed key 'B'.
Received from {1}: FOO bar
Response sent to {1}: ERROR: Unknown command 'FOO'
Received from {1}:\x20
Response sent to {1}: ERROR: Empty command
Received from {1}:\x20
Response sent to {1}: ERROR: Empty command
Received from {1}: quit now
Response sent to {1}: ERROR: Unknown command 'quit'
Received from {1}: PRINT extra
Response sent to {1}: [KEY]: a\t[VALUE]: 2
[KEY]: c\t[VALUE]: 3
Received from {1}: SET ключ значение
Response sent to {1}: Added key 'ключ' with value 'значение'
Received from {1}: SET d 4
Response sent to {1}: Added key 'd' with value '4'
Received from {1}: PRINT
Response sent to {1}: [KEY]: a\t[VALUE]: 2
[KEY]: c\t[VALUE]: 3
[KEY]: ключ\t[VALUE]: значение
[KEY]: d\t[VALUE]: 4
Received from {1}: QUIT
Client {1} requested to quit.
Connection with {1} closed.
Connection from {2}
Received from {2}: quit
Client {2} requested to quit.
Connection with {2} closed.
Connection from {3}
Client {3} disconnected.
Connection with {3} closed.
Connection from {4}
Error handling client {4}: 'utf-8' codec can't decode byte 0xff in position 0: invalid start byte
Connection with {4} closed.
"""
GOLDEN_STDOUT_SHUTDOWN = "Server is shutting down gracefully.\nServer socket closed.\n"


class Client:
    """client.py semantics: one sendall() and one recv(1024) per command."""

    def __init__(self, port, host="127.0.0.1"):
        self.sock = socket.create_connection((host, port), timeout=TIMEOUT)

    def cmd(self, payload):
        self.sock.sendall(payload)
        return self.sock.recv(1024)

    def send(self, payload):
        self.sock.sendall(payload)

    def recv(self):
        return self.sock.recv(1024)

    @property
    def address(self):
        return self.sock.getsockname()

    def close(self):
        self.sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()


def run_golden_connections(port, pause=0.3):
    """Drive the four golden connections. Returns (responses, client addresses in order)."""
    responses, addresses = [], []
    with Client(port) as c:
        addresses.append(c.address)
        for payload, _ in GOLDEN_SESSION:
            responses.append(c.cmd(payload))
        c.send(GOLDEN_QUIT)
        responses.append(c.recv())
    time.sleep(pause)  # let the handler print its last line before the next connection starts
    with Client(port) as c:
        addresses.append(c.address)
        c.send(b"quit")
        responses.append(c.recv())
    time.sleep(pause)
    with Client(port) as c:
        addresses.append(c.address)
    time.sleep(pause)
    with Client(port) as c:
        addresses.append(c.address)
        c.send(b"\xff\xfe")
        try:
            responses.append(c.recv())
        except ConnectionResetError:  # the server closed with unread data
            responses.append(b"")
    time.sleep(pause)
    return responses, addresses


GOLDEN_RESPONSES = [expected for _, expected in GOLDEN_SESSION] + [b"", b"", b""]


def expected_stdout_tail(addresses):
    text = GOLDEN_STDOUT_TAIL
    for i, address in enumerate(addresses, 1):
        text = text.replace("{%d}" % i, str(tuple(address)))
    return text


class EventLog:
    """Accumulates events from a Subscription and waits for specific ones."""

    def __init__(self, subscription):
        self.subscription = subscription
        self.events = []

    def pump(self):
        self.events.extend(self.subscription.drain())
        return self.events

    def wait_for(self, predicate, timeout=TIMEOUT):
        deadline = time.monotonic() + timeout
        seen = 0
        while True:
            self.pump()
            for event in self.events[seen:]:
                if predicate(event):
                    return event
            seen = len(self.events)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError("event not seen within %ss" % timeout)
            event = self.subscription.get(timeout=min(remaining, 0.05))
            if event is not None:
                self.events.append(event)

    def of_type(self, etype):
        return [e for e in self.pump() if e["type"] == etype]


class RunningServer:
    """Server(port=0) on a background thread, in a temporary directory, with stdout captured."""

    def __init__(self, observe=False, store_dir=None, observer=None, server_kwargs=None, **observer_kwargs):
        self.server_kwargs = server_kwargs or {}
        self._tmp = None
        if store_dir is None:
            self._tmp = tempfile.TemporaryDirectory()
            store_dir = self._tmp.name
        self.dir = store_dir
        self.store_path = os.path.join(store_dir, "store.txt")
        if observer is None and observe:
            observer = observability.Observer(**observer_kwargs)
        self.observer = observer
        self.log = EventLog(observer.subscribe(maxsize=1000000)) if observer is not None else None
        self.stdout = io.StringIO()

    def __enter__(self):
        self._threads_before = threading.active_count()
        self._saved_stdout, sys.stdout = sys.stdout, self.stdout
        try:
            self.server = server.Server(host="127.0.0.1", port=0, store_path=self.store_path,
                                        observer=self.observer, **self.server_kwargs)
            self.thread = threading.Thread(target=self.server.start, name="server-main", daemon=True)
            self.thread.start()
            if not self.server.ready.wait(TIMEOUT):
                raise AssertionError("server did not start")
        except BaseException:
            sys.stdout = self._saved_stdout
            raise
        self.port = self.server.bound_port
        return self

    def __exit__(self, *exc_info):
        try:
            self.server.shutdown()
            self.thread.join(TIMEOUT)
            if self.thread.is_alive():
                raise AssertionError("start() did not return after shutdown()")
            deadline = time.monotonic() + TIMEOUT
            while threading.active_count() > self._threads_before and time.monotonic() < deadline:
                time.sleep(0.01)  # handler threads finish once their clients are gone
        finally:
            sys.stdout = self._saved_stdout
            if self.log is not None:
                self.log.pump()
            if self._tmp is not None:
                self._tmp.cleanup()

    def read_store(self):
        with open(self.store_path, "rb") as f:
            return f.read()


def parse_store_file(path):
    """What KeyValueStore.load() would read back from path."""
    result = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                parts = line.split("\t", 1)
                if len(parts) == 2:
                    result[parts[0]] = parts[1]
    return result


def http_get(url, method="GET", headers=None):
    """(status, headers, body bytes) without raising on HTTP errors."""
    request = urllib.request.Request(url, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as e:
        try:
            return e.code, dict(e.headers), e.read()
        finally:
            e.close()


def http_json(url):
    status, headers, body = http_get(url)
    if status != 200:
        raise AssertionError("%s -> %s %r" % (url, status, body[:200]))
    return json.loads(body.decode("utf-8"))


# ---------------------------------------------------------------- schema checks

_TYPES = {"int": int, "bool": bool, "str": str, "object": dict}


def check_value(value, type_spec):
    """True when value matches a schema type such as "int|null" or "TextField"."""
    for alternative in type_spec.split("|"):
        if alternative == "null" and value is None:
            return True
        if alternative == "TextField" and isinstance(value, dict) and set(value) == {
                "text", "chars", "truncated", "redacted"}:
            return True
        expected = _TYPES.get(alternative)
        if expected is int and isinstance(value, bool):
            continue
        if expected is not None and isinstance(value, expected):
            return True
    return False


def event_problems(event, schema=None):
    """List of ways an event departs from observability.event_schema()."""
    schema = schema or observability.event_schema()
    problems = []
    if set(event) != set(schema["envelope"]):
        problems.append("envelope keys %s" % sorted(set(event) ^ set(schema["envelope"])))
        return problems
    for name, field in schema["envelope"].items():
        if not check_value(event[name], field["type"]):
            problems.append("envelope %s=%r is not %s" % (name, event[name], field["type"]))
    spec = schema["events"].get(event["type"])
    if spec is None:
        return problems + ["unknown type %r" % event["type"]]
    if set(event["data"]) != set(spec["data"]):
        problems.append("%s data keys differ: %s" % (event["type"], sorted(set(event["data"]) ^ set(spec["data"]))))
        return problems
    for name, field in spec["data"].items():
        if not check_value(event["data"][name], field["type"]):
            problems.append("%s.%s=%r is not %s" % (event["type"], name, event["data"][name], field["type"]))
    return problems


# ------------------------------------------------------------ subprocess server

class CliServer:
    """`python server.py ARGS` in a temp dir; port learned from the stderr notice (POSIX only)."""

    def __init__(self, args, cwd, env=None):
        # Unbuffered pipes: with a buffered reader, readline() could pull several lines into Python's
        # buffer and select() on the descriptor would then wait for data that has already arrived.
        self.proc = subprocess.Popen([sys.executable, SERVER_PY] + list(args), cwd=cwd,
                                     env=dict(SUBPROCESS_ENV, **(env or {})),
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        self.stderr_lines = []
        self.port = None
        self.inspect_url = None
        deadline = time.monotonic() + TIMEOUT
        while self.port is None:
            remaining = deadline - time.monotonic()
            ready, _, _ = select.select([self.proc.stderr], [], [], max(remaining, 0))
            if not ready:
                self.proc.kill()
                raise AssertionError("no port notice on stderr: %r" % self.stderr_lines)
            line = self.proc.stderr.readline().decode()
            if not line:
                self.proc.kill()
                raise AssertionError("server exited: %r" % self.stderr_lines)
            self.stderr_lines.append(line)
            match = re.match(r"Listening on .*:(\d+)$", line.strip())
            if match:
                self.port = int(match.group(1))
            match = re.match(r"Inspection endpoint: (\S+)$", line.strip())
            if match:
                self.inspect_url = match.group(1)

    def listening_line(self, host):
        return "Server listening on %s:%d\n" % (host, self.port)

    def interrupt(self):
        """Ctrl+C equivalent; returns (returncode, stdout text, remaining stderr text)."""
        self.proc.send_signal(signal.SIGINT)
        out, err = self.proc.communicate(timeout=TIMEOUT)
        return self.proc.returncode, out.decode(), err.decode()
