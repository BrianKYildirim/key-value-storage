"""Captures real server.py runs over real TCP for the portfolio's key-value demo recordings.

The browser cannot host threads, sockets, shutdown with open connections or recv() framing, so the portfolio
shows recordings of real native runs for them. This script makes those recordings. Every scenario starts the
real server.py with --trace-jsonl on 127.0.0.1 in an empty temporary directory, drives real TCP clients the way
client.py does (one sendall() and one recv(1024) per command), stops the server with SIGINT so the trace ends
with server.stop, and asserts from the recorded events that the behavior it exists to show actually happened.
A scenario whose behavior is missing writes nothing (no retry loop: --attempt records which attempt this is).

Run it on Ubuntu under WSL2 only: on Windows before Python 3.13 the trace clock ticks every 15.625 ms, so
durations would be quantized (the trace records its clock in meta.json).

  python bench/record_scenarios.py --env ubuntu-wsl2 [--only contention,shutdown,framing,backlog]
         [--date YYYY-MM-DD] [--out DIR] [--attempt N] [--allow-dirty]

Output, per scenario, under <out>/<date>-<sha7>/ubuntu-wsl2/<scenario>/:
  meta.json            kv-recording/1, in the style of bench-meta/1 (hardware, OS, toolchain, source, raw sha256)
  raw/trace.jsonl      the server's trace (shutdown: trace-run-a.jsonl and trace-run-b.jsonl)
  raw/clients.jsonl    what the capture script's clients sent and received, on the script's own clock

It refuses a dirty tree unless --allow-dirty (recorded in meta.json; for dry runs written outside the
repository) and never overwrites a scenario directory. The default --out is bench/recordings in this repo.
Standard library only.
"""

import argparse
import datetime
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.dont_write_bytecode = True  # no bench/__pycache__ in the captured tree
sys.path.insert(0, str(Path(__file__).resolve().parent))
import benchmeta  # noqa: E402
import loadtest  # noqa: E402
import run_bench  # noqa: E402

REPO = run_bench.REPO
SCHEMA = "kv-recording/1"
ENV_ID = "ubuntu-wsl2"
MAX_RAW_BYTES = 1024 * 1024  # a raw file at or above this is refused: the page loads these files
CLIENT_NOTE = "clients.jsonl times are the capture script's own time.monotonic_ns, not the server's"
THREAD_RE = re.compile(r"^client 127\.0\.0\.1:\d+$")


class CaptureError(Exception):
    """The capture could not be carried out (as opposed to carried out without its behavior)."""


def log(msg):
    print("[record_scenarios] %s" % msg, file=sys.stderr, flush=True)


def die(msg, code=1):
    log("error: %s" % msg)
    sys.exit(code)


# ------------------------------------------------------------------- pure checks (tested without a server)

def read_events(path):
    """The events of a JSONL trace, in file order ([] when the file does not exist)."""
    p = Path(path)
    if not p.is_file():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").split("\n") if line.strip()]


def check_seq(events):
    """Problems with a trace's seq: it must start at 1 and rise by exactly 1 with no gap; an empty trace is one."""
    if not events:
        return ["the trace is empty"]
    problems = []
    if events[0].get("seq") != 1:
        problems.append("seq starts at %r, not 1" % events[0].get("seq"))
    for prev, cur in zip(events, events[1:]):
        if cur.get("seq") != prev.get("seq", 0) + 1:
            problems.append("seq goes from %r to %r (a gap or a repeat)" % (prev.get("seq"), cur.get("seq")))
            break
    return problems


def check_stop(events):
    """The trace must end with server.stop (the server was stopped with SIGINT and shut down cleanly)."""
    if not events or events[-1].get("type") != "server.stop":
        return ["the trace does not end with server.stop"]
    return []


def _of(events, etype):
    return [e for e in events if e.get("type") == etype]


def check_contention(events, clients=4):
    """The store lock was contended (some acquire waited), and each connection ran on its own handler thread."""
    problems = []
    contended = [e for e in _of(events, "lock.acquired")
                 if e.get("data", {}).get("contended") is True and (e.get("data", {}).get("wait_ns") or 0) > 0]
    if not contended:
        problems.append("no lock.acquired with contended true and wait_ns above 0")
    names = {e.get("thread") for e in _of(events, "conn.open")}
    if len(names) != clients:
        problems.append("%d distinct handler threads among conn.open events, expected %d" % (len(names), clients))
    for name in sorted(n for n in names if not THREAD_RE.match(str(n))):
        problems.append("handler thread name %r is not 'client 127.0.0.1:<port>'" % name)
    return problems


def check_shutdown(run_a, run_b):
    """Run A: the server ended every open connection itself and stopped. Run B: the restarted server loaded the
    store file the first run left."""
    problems = []
    opened = _of(run_a, "conn.open")
    if len(opened) < 2:
        problems.append("run A has %d conn.open events, expected at least 2" % len(opened))
    by_server = [e for e in _of(run_a, "conn.disconnect") if e.get("data", {}).get("by") == "server"]
    if not by_server:
        problems.append("run A has no conn.disconnect with by server")
    shutdown = [e for e in _of(run_a, "conn.close") if e.get("data", {}).get("reason") == "shutdown"]
    if not shutdown:
        problems.append("run A has no conn.close with reason shutdown")
    if opened and (len(by_server) != len(opened) or len(shutdown) != len(opened)):
        problems.append("run A: %d connections open, %d disconnected by the server, %d closed with reason shutdown"
                        % (len(opened), len(by_server), len(shutdown)))
    if not _of(run_a, "server.stop"):
        problems.append("run A has no server.stop")
    if not any(e.get("data", {}).get("exists") is True for e in _of(run_b, "store.load")):
        problems.append("run B has no store.load with exists true")
    return problems


def check_framing(events, client_log):
    """One oversize sendall was read as more than one conn.recv (the first exactly 1024 bytes), and a PRINT reply
    longer than 1024 bytes reached the client in more than one chunk."""
    problems = []
    over = next((r["bytes"] for r in client_log if r.get("op") == "send" and (r.get("bytes") or 0) > 1024), None)
    recvs = [e.get("data", {}).get("bytes") for e in _of(events, "conn.recv")]
    if over is None:
        problems.append("the client log has no send of more than 1024 bytes")
    elif not recvs:
        problems.append("the trace has no conn.recv")
    else:
        if recvs[0] != 1024:
            problems.append("the first conn.recv read %r bytes, expected exactly 1024" % recvs[0])
        total, used = 0, 0
        for n in recvs:
            total, used = total + n, used + 1
            if total >= over:
                break
        if used < 2 or total < over:
            problems.append("the oversize send of %d bytes was not read as more than one conn.recv" % over)
    if not any((e.get("data", {}).get("bytes") or 0) > 1024 for e in _of(events, "conn.send")):
        problems.append("no conn.send of more than 1024 bytes (a PRINT reply longer than one client read)")
    chunks = None
    for i, rec in enumerate(client_log):
        if rec.get("op") == "send" and str(rec.get("text", "")).strip().upper() == "PRINT":
            chunks = [r for r in client_log[i + 1:] if r.get("client") == rec.get("client") and r.get("op") == "recv"
                      and not r.get("timed_out") and r.get("bytes")]
            break
    if chunks is None:
        problems.append("the client log has no PRINT send")
    elif len(chunks) < 2 or chunks[0]["bytes"] != 1024:
        problems.append("the PRINT reply was not split: the client's first read after PRINT was %s bytes and %d "
                        "reads followed" % (chunks[0]["bytes"] if chunks else None, len(chunks)))
    return problems


def merged_recv(events, sizes):
    """True when one conn.recv held every payload of sizes (two sends read together). Not a problem when False."""
    return sum(sizes) in [e.get("data", {}).get("bytes") for e in _of(events, "conn.recv")]


def check_backlog(events):
    """With one connection slot, the second client was accepted only after the first connection closed."""
    accepts = _of(events, "conn.accept")
    if len(accepts) < 2:
        return ["%d conn.accept events, expected 2" % len(accepts)]
    first, second = accepts[0], accepts[1]
    closed = next((e for e in _of(events, "conn.close") if e.get("conn") == first.get("conn")), None)
    if closed is None:
        return ["the first connection has no conn.close"]
    if second["seq"] < closed["seq"]:
        return ["the second conn.accept (seq %d) is before the first connection's conn.close (seq %d)"
                % (second["seq"], closed["seq"])]
    return []


def _path_forms(path):
    p = str(path)
    forms = {p, p.replace("\\", "/"), p.replace("/", "\\"), p.replace("/", "\\").replace("\\", "\\\\")}
    return sorted(f for f in forms if len(f) > 3)


def path_leaks(text, paths):
    """Problems for every absolute path in paths (both slash styles and the JSON-escaped form) found in text."""
    problems = []
    for path in paths:
        for form in _path_forms(path):
            if form in text:
                problems.append("the text contains the absolute path %s" % form)
                break
    return problems


# ------------------------------------------------------------------- real clients and server

class ClientLog:
    """What the capture script's clients did, one JSON line each: client, op (connect, send, recv, close),
    mono_ns (the script's own time.monotonic_ns), bytes, text (UTF-8, errors replaced)."""

    def __init__(self):
        self.lines = []
        self._lock = threading.Lock()

    def add(self, client, op, nbytes=None, data=b"", **extra):
        with self._lock:
            rec = {"client": client, "op": op, "mono_ns": time.monotonic_ns(), "bytes": nbytes,
                   "text": data.decode("utf-8", errors="replace")}
            rec.update(extra)
            self.lines.append(rec)

    def dump(self):
        return "".join(json.dumps(r, sort_keys=True) + "\n" for r in self.lines).encode("utf-8")


class LoggedClient:
    """A TCP client with client.py's semantics: sendall() once per command, recv(1024) once per reply."""

    def __init__(self, name, port, clog):
        self.name, self.clog = name, clog
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=30)
        clog.add(name, "connect")

    def send(self, data):
        self.sock.sendall(data)
        self.clog.add(self.name, "send", len(data), data)

    def recv(self, timeout=30.0):
        """One recv(1024). None when nothing arrived within timeout; b"" when the server closed the connection."""
        self.sock.settimeout(timeout)
        try:
            data = self.sock.recv(1024)
        except socket.timeout:
            self.clog.add(self.name, "recv", 0, b"", timed_out=True)
            return None
        self.clog.add(self.name, "recv", len(data), data)
        return data

    def drain(self, quiet=0.5):
        """Read until nothing arrives for quiet seconds (or the connection closes). Returns the chunks."""
        chunks = []
        while True:
            data = self.recv(quiet)
            if not data:
                return chunks
            chunks.append(data)

    def close(self):
        self.sock.close()
        self.clog.add(self.name, "close")


def start_server(work, trace, extra_args=()):
    """server.py on 127.0.0.1 and a free port, store.txt in work, trace to trace. Returns (process, port) once the
    trace shows server.listen."""
    port = loadtest._free_port()
    cmd = [sys.executable, str(REPO / "server.py"), "--host", "127.0.0.1", "--port", str(port), "--store", "store.txt",
           "--trace-jsonl", str(trace), *extra_args]
    proc = subprocess.Popen(cmd, cwd=work, env=run_bench.child_env(), stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    if not run_bench._wait_for_event(trace, proc, "server.listen"):
        loadtest._stop(proc)
        raise CaptureError("server did not start listening (exit status %s)" % proc.poll())
    return proc, port


def stop_server(proc):
    """SIGINT, so the trace ends with server.stop. Returns problems (a killed server is one)."""
    loadtest._stop(proc)
    return ["the server did not stop on SIGINT within the timeout and was killed"] \
        if proc.returncode == -signal.SIGKILL else []


def wait_for_count(path, proc, etype, count, timeout=15.0):
    """Poll the trace until it holds count events of etype."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if len(_of(read_events(path), etype)) >= count:
                return True
        except ValueError:  # a line still being written
            pass
        if proc.poll() is not None:
            return False
        time.sleep(0.05)
    return False


# ------------------------------------------------------------------- scenarios

CONTENTION_CLIENTS = 4
CONTENTION_COMMANDS = 25


def run_contention(work, clog):
    """Four clients, released together by a barrier, each send SET counter <index> many times to one key."""
    trace, problems = os.path.join(work, "trace.jsonl"), []
    proc, port = start_server(work, trace)
    try:
        barrier, errors = threading.Barrier(CONTENTION_CLIENTS), []

        def worker(index):
            try:
                client = LoggedClient("client-%d" % index, port, clog)
                barrier.wait(30)
                for _ in range(CONTENTION_COMMANDS):
                    client.send(("SET counter %d" % index).encode())
                    client.recv()
                client.send(b"quit")
                client.close()
            except Exception as e:  # reported below; the capture is then refused
                errors.append("client %d: %s: %s" % (index, type(e).__name__, e))
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(CONTENTION_CLIENTS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(120)
        problems += errors
        if not wait_for_count(trace, proc, "conn.close", CONTENTION_CLIENTS):
            problems.append("fewer than %d conn.close events after the clients finished" % CONTENTION_CLIENTS)
    finally:
        problems += stop_server(proc)
    events = read_events(trace)
    problems += check_seq(events) + check_stop(events) + check_contention(events, CONTENTION_CLIENTS)
    contended = sum(1 for e in _of(events, "lock.acquired") if e["data"].get("contended") is True)
    return {
        "raw": {"trace.jsonl": Path(trace).read_bytes() if os.path.isfile(trace) else b""},
        "problems": problems,
        "workload": {"clients": CONTENTION_CLIENTS, "commands_per_client": CONTENTION_COMMANDS,
                     "command": "SET counter <client index>, one key shared by every client",
                     "start": "every client connects, waits on a threading.Barrier, then sends",
                     "client_semantics": "one sendall() and one recv(1024) per command, then quit",
                     "server_args": ["--host", "127.0.0.1", "--port", "<free>", "--store", "store.txt",
                                     "--trace-jsonl", "<trace file>"]},
        "notes": ["asserted before writing: at least one lock.acquired with contended true and wait_ns above 0, "
                  "and one handler thread per connection (%d distinct 'client 127.0.0.1:<port>' names)"
                  % CONTENTION_CLIENTS,
                  "the contended acquisitions in this capture: %d of %d lock.acquired events"
                  % (contended, len(_of(events, "lock.acquired")))],
        "summary": "contended acquisitions %d, events %d" % (contended, len(events)),
    }


def run_shutdown(work, clog):
    """Run A: two idle connections are open when the server is stopped with SIGINT. Run B: a new server over the
    same store.txt loads what run A saved."""
    trace_a, trace_b, problems = os.path.join(work, "trace-run-a.jsonl"), os.path.join(work, "trace-run-b.jsonl"), []
    proc, port = start_server(work, trace_a)
    clients = []
    try:
        clients = [LoggedClient("a-client-1", port, clog), LoggedClient("a-client-2", port, clog)]
        if not wait_for_count(trace_a, proc, "conn.open", 2):
            problems.append("run A: fewer than 2 conn.open events before the stop")
        clients[0].send(b"SET a 1")
        clients[0].recv()
    finally:
        problems += stop_server(proc)  # both connections are still open here
    for c in clients:
        if c.recv(5) != b"":
            problems.append("run A: %s did not see the server close its connection" % c.name)
        c.close()
    proc, port = start_server(work, trace_b)
    try:
        c = LoggedClient("b-client-1", port, clog)
        c.send(b"GET a")
        reply = c.recv()
        if reply != b"1":
            problems.append("run B: GET a returned %r, expected b'1'" % reply)
        c.send(b"quit")
        c.close()
        if not wait_for_count(trace_b, proc, "conn.close", 1):
            problems.append("run B: no conn.close after quit")
    finally:
        problems += stop_server(proc)
    a, b = read_events(trace_a), read_events(trace_b)
    for label, events in (("run A", a), ("run B", b)):
        problems += ["%s: %s" % (label, p) for p in check_seq(events) + check_stop(events)]
    problems += check_shutdown(a, b)
    return {
        "raw": {"trace-run-a.jsonl": Path(trace_a).read_bytes() if os.path.isfile(trace_a) else b"",
                "trace-run-b.jsonl": Path(trace_b).read_bytes() if os.path.isfile(trace_b) else b""},
        "problems": problems,
        "workload": {"run_a": "two clients connect; client 1 sends SET a 1 and reads the reply; both stay idle; "
                              "the server gets SIGINT with both connections open",
                     "run_b": "a new server.py in the same directory (same store.txt); one client sends GET a "
                              "and quit; the server gets SIGINT",
                     "server_args": ["--host", "127.0.0.1", "--port", "<free>", "--store", "store.txt",
                                     "--trace-jsonl", "<trace file>"]},
        "notes": ["asserted before writing: run A has conn.disconnect by server and conn.close with reason shutdown "
                  "for every open connection and ends with server.stop; run B has store.load with exists true",
                  "trace-run-a.jsonl and trace-run-b.jsonl are two separate server processes, each with its own "
                  "seq starting at 1 and its own mono_ns clock reading"],
        "summary": "run A %d events, run B %d events" % (len(a), len(b)),
    }


FRAMING_OVERSIZE = b"SET big " + b"x" * 1100  # one sendall of more than 1024 bytes
FRAMING_KEYS = 10
MERGED_SENDS = (b"SET m1 1", b"SET m2 2")  # two sendall calls back to back, no read in between


def run_framing(work, clog):
    """One oversize sendall, a PRINT reply longer than one client read, and (K4) a probe for two sends that the
    server reads together."""
    trace, problems = os.path.join(work, "trace.jsonl"), []
    proc, port = start_server(work, trace)
    try:
        c = LoggedClient("framing-1", port, clog)
        c.send(FRAMING_OVERSIZE)
        c.drain()  # the reply to the first 1024 bytes, then the reply to the rest read as a second command
        for i in range(FRAMING_KEYS):
            c.send(("SET k%02d %s" % (i, "v" * 40)).encode())
            c.recv()
        c.send(b"PRINT")
        c.recv()  # exactly 1024 bytes of a longer reply
        c.send(b"GET k00")
        c.drain()  # the rest of the PRINT reply arrives first, then the reply to GET
        c.send(b"quit")
        c.close()
        if not wait_for_count(trace, proc, "conn.close", 1):
            problems.append("no conn.close after quit")
    finally:
        problems += stop_server(proc)
    events = read_events(trace)
    problems += check_seq(events) + check_stop(events) + check_framing(events, clog.lines)
    raw = {"trace.jsonl": Path(trace).read_bytes() if os.path.isfile(trace) else b""}
    notes = ["asserted before writing: the oversize send was read by more than one conn.recv with the first exactly "
             "1024 bytes, and a PRINT reply longer than 1024 bytes reached the client in more than one chunk (the "
             "client's first read after PRINT is exactly 1024 bytes and the rest arrives on a later read)"]
    probe = {"observed": False}
    if not problems:
        mdir = os.path.join(work, "merged")
        os.mkdir(mdir)
        mtrace, mlog = os.path.join(mdir, "trace.jsonl"), ClientLog()
        mproc, mport = start_server(mdir, mtrace)
        try:
            m = LoggedClient("merged-1", mport, mlog)
            for payload in MERGED_SENDS:
                m.send(payload)
            m.drain()
            m.send(b"quit")
            m.close()
            if not wait_for_count(mtrace, mproc, "conn.close", 1):
                problems.append("merged-sends probe: no conn.close after quit")
        finally:
            problems += ["merged-sends probe: %s" % p for p in stop_server(mproc)]
        mevents = read_events(mtrace)
        problems += ["merged-sends probe: %s" % p for p in check_seq(mevents) + check_stop(mevents)]
        probe["observed"] = not problems and merged_recv(mevents, [len(p) for p in MERGED_SENDS])
        if probe["observed"]:
            raw["trace-merged.jsonl"], raw["clients-merged.jsonl"] = Path(mtrace).read_bytes(), mlog.dump()
            notes.append("merged-sends probe (a second server run, two sendall calls back to back with no read in "
                         "between): observed once, one conn.recv held both payloads; trace-merged.jsonl and "
                         "clients-merged.jsonl are included. It ran once; it was never retried")
        else:
            notes.append("merged-sends probe (a second server run, two sendall calls back to back with no read in "
                         "between): ran once and the two sends were read by separate conn.recv calls, so it is not "
                         "included (owner decision K4: kept only when a capture shows it). It was never retried")
    return {
        "raw": raw, "problems": problems,
        "workload": {"oversize_send_bytes": len(FRAMING_OVERSIZE), "oversize_command": "SET big <1100 x characters>",
                     "keys_set_before_print": FRAMING_KEYS, "print": "one PRINT, one recv(1024), then GET k00 and "
                     "reads until quiet", "merged_sends_probe": {"payloads": [p.decode() for p in MERGED_SENDS],
                                                                "observed": probe["observed"]},
                     "server_args": ["--host", "127.0.0.1", "--port", "<free>", "--store", "store.txt",
                                     "--trace-jsonl", "<trace file>"]},
        "notes": notes,
        "summary": "events %d, merged-sends probe observed %s" % (len(events), probe["observed"]),
    }


def run_backlog(work, clog):
    """--max-connections 1: the second client connects while the first holds the only slot, so it waits in the
    listen backlog and is accepted after the first connection closes."""
    trace, problems = os.path.join(work, "trace.jsonl"), []
    proc, port = start_server(work, trace, ["--max-connections", "1"])
    try:
        c1 = LoggedClient("backlog-1", port, clog)
        c1.send(b"SET a 1")
        c1.recv()
        c2 = LoggedClient("backlog-2", port, clog)  # the kernel completes this connect; the server has not accepted
        c2.send(b"GET a")
        if c2.recv(1.0) is not None:
            problems.append("client 2 received a reply while client 1 held the only slot")
        c1.send(b"quit")
        c1.close()
        reply = c2.recv(10)
        if reply != b"1":
            problems.append("client 2 received %r after client 1 quit, expected b'1'" % reply)
        c2.send(b"quit")
        c2.close()
        if not wait_for_count(trace, proc, "conn.close", 2):
            problems.append("fewer than 2 conn.close events after both clients quit")
    finally:
        problems += stop_server(proc)
    events = read_events(trace)
    problems += check_seq(events) + check_stop(events) + check_backlog(events)
    return {
        "raw": {"trace.jsonl": Path(trace).read_bytes() if os.path.isfile(trace) else b""},
        "problems": problems,
        "workload": {"server_args": ["--host", "127.0.0.1", "--port", "<free>", "--store", "store.txt",
                                     "--trace-jsonl", "<trace file>", "--max-connections", "1"],
                     "clients": "client 1 sends SET a 1 and keeps its connection; client 2 connects and sends GET a "
                                "while client 1 holds the slot; client 1 sends quit; client 2 then gets its reply"},
        "notes": ["asserted before writing: the second conn.accept comes after the first connection's conn.close "
                  "(the accept loop takes a connection slot before it calls accept)",
                  "client 2's first read in clients.jsonl is a timeout: nothing replied while client 1 held the slot"],
        "summary": "events %d" % len(events),
    }


SCENARIOS = {"contention": run_contention, "shutdown": run_shutdown, "framing": run_framing,
             "backlog": run_backlog}


# ------------------------------------------------------------------- output

def rel_out(out_root):
    """The --out directory as shown in meta: repo-relative when inside the repo, else the token <out>."""
    try:
        return Path(os.path.relpath(out_root, REPO)).as_posix() if _inside(out_root, REPO) else "<out>"
    except ValueError:
        return "<out>"


def _inside(path, base):
    path, base = Path(path).resolve(), Path(base).resolve()
    return path == base or base in path.parents


def shown_command(args):
    """The command as documented, with no absolute path in it."""
    cmd = ["python", "bench/record_scenarios.py", "--env", args.env]
    if args.date_given:
        cmd += ["--date", args.date]
    if args.only:
        cmd += ["--only", ",".join(args.only)]
    if args.out:
        cmd += ["--out", rel_out(Path(args.out).resolve())]
    if args.attempt != 1:
        cmd += ["--attempt", str(args.attempt)]
    if args.allow_dirty:
        cmd.append("--allow-dirty")
    return cmd


def leak_paths(out_root):
    paths = [REPO, Path.home(), Path(tempfile.gettempdir())]
    if not _inside(out_root, REPO):
        paths.append(out_root)
    return paths


def write_recording(args, name, result, source, dest, work, out_root):
    """Stage meta.json and raw/ in the capture's temporary directory, refuse a path leak or an oversize file, then
    copy into dest. Returns problems; nothing is written to dest when there are any."""
    stage = Path(work) / "stage" / name
    raw = stage / "raw"
    raw.mkdir(parents=True)
    problems = []
    for fname, data in result["raw"].items():
        (raw / fname).write_bytes(data)
        if len(data) >= MAX_RAW_BYTES:
            problems.append("raw/%s is %d bytes, at or above the %d byte limit" % (fname, len(data), MAX_RAW_BYTES))
    notes = list(result["notes"]) + [
        "attempt %d of this scenario; a scenario without its behavior writes nothing, and a rerun is recorded as a "
        "higher attempt" % args.attempt,
        "the server's stdout and stderr go to the null device; it is stopped with SIGINT, so every trace ends with "
        "server.stop"]
    if source["dirty"]:
        notes.append("dirty tree (--allow-dirty): modified %s" % source["modified"])
    meta = benchmeta.collect(
        "key-value-storage", ENV_ID, shown_command(args), result["workload"],
        extra={"schema": SCHEMA, "scenario": name, "source": source,
               "instrumentation": "kv-observability-jsonl: server.py with --trace-jsonl "
                                  "(build instrumented:kv-observability-jsonl)",
               "library": {"server": "server.py on 127.0.0.1 with --trace-jsonl",
                           "trace_clock": benchmeta.trace_clock(),
                           "third_party_libraries": "none (standard library only)"},
               "attempt": args.attempt, "client_side": CLIENT_NOTE, "notes": notes,
               "raw_files": benchmeta.raw_file_entries(stage)})
    benchmeta.write_json(stage / "meta.json", meta)
    paths = leak_paths(out_root)
    for f in sorted(p for p in stage.rglob("*") if p.is_file()):
        problems += ["%s: %s" % (f.relative_to(stage).as_posix(), p)
                     for p in path_leaks(f.read_text(encoding="utf-8", errors="replace"), paths)]
    if problems:
        return problems
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(stage, dest)
    return []


def parse_args(argv):
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--env", choices=(ENV_ID,), required=True, help="environment id (WSL Ubuntu only)")
    p.add_argument("--date", default=None, help="YYYY-MM-DD in the session directory name (default: today, UTC)")
    p.add_argument("--only", help="comma-separated scenario names (default: all of %s)" % ", ".join(SCENARIOS))
    p.add_argument("--out", help="output root (default bench/recordings in this repo)")
    p.add_argument("--attempt", type=int, default=1, help="which attempt this capture is, recorded in meta.json")
    p.add_argument("--allow-dirty", action="store_true",
                   help="capture from a tree with uncommitted tracked changes (recorded); dry runs written outside "
                        "the repository only")
    args = p.parse_args(argv)
    args.date_given = args.date is not None
    args.date = args.date or today
    try:
        datetime.date.fromisoformat(args.date)
    except ValueError:
        p.error("--date must be YYYY-MM-DD")
    if args.attempt < 1:
        p.error("--attempt must be at least 1")
    args.only = [s for s in (args.only or "").split(",") if s]
    unknown = [s for s in args.only if s not in SCENARIOS]
    if unknown:
        p.error("unknown scenario %s (choose from %s)" % (", ".join(unknown), ", ".join(SCENARIOS)))
    return args


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    run_bench.check_platform(args.env)
    source = benchmeta.git_source(REPO, run_bench.REPO_SLUG)
    if source["dirty"] and not args.allow_dirty:
        die("tracked files have uncommitted changes (%s); commit them, or pass --allow-dirty for a dry run written "
            "outside the repository" % ", ".join(source["modified"]))
    out_root = Path(args.out).resolve() if args.out else REPO / "bench" / "recordings"
    if args.allow_dirty and _inside(out_root, REPO):
        die("--allow-dirty is for dry runs: --out must be outside the repository")
    names = args.only or list(SCENARIOS)
    env_dir = out_root / ("%s-%s" % (args.date, source["sha"][:7])) / args.env
    for name in names:
        if (env_dir / name).exists():
            die("refusing to overwrite existing scenario directory %s" % (env_dir / name).as_posix(), 3)
    failed = []
    for name in names:
        clog = ClientLog()
        with tempfile.TemporaryDirectory(prefix="kvrec-") as work:
            try:
                result = SCENARIOS[name](work, clog)
            except Exception as e:  # the capture itself broke; reported, nothing written
                result = {"raw": {}, "problems": ["capture failed: %s: %s" % (type(e).__name__, e)], "notes": [],
                          "workload": {}, "summary": ""}
            problems = list(result["problems"])
            if not problems:
                result["raw"]["clients.jsonl"] = clog.dump()
                problems = write_recording(args, name, result, source, env_dir / name, work, out_root)
        if problems:
            failed.append(name)
            for problem in problems:
                log("%s: %s" % (name, problem))
            log("%s: nothing written (attempt %d)" % (name, args.attempt))
        else:
            log("%s: ok (attempt %d): %s" % (name, args.attempt, result["summary"]))
            print((env_dir / name).as_posix())
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
