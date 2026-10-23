"""Opt-in tracing, storage inspection and runtime diagnostics for server.py.

Standard library only. All state lives on an Observer instance and every Server
gets its own, so servers in one process never share events, ids or counters.
server.py imports this module only when an observability option is used.

Data honesty: every value exposed here is actual state read from the running
server, a measurement of something that really happened (timestamps, durations,
counts, sizes), or a value derived from those. Derived values always appear
under a "derived" key together with the formula they come from, and the one
approximate size (memory use) is labelled as an estimate.
"""

import collections
import functools
import http.server
import json
import os
import platform
import queue
import socket
import socketserver
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit

SCHEMA_VERSION = 1

_STORE_VERBS = ("SET", "GET", "REMOVE", "PRINT")
_MAX_ERROR_TYPES = 64  # distinct exception names tracked before folding the rest into "other"


def _contained(method):
    """Run an instrumentation hook so that a failure inside it can never affect serving."""

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except Exception:
            self._internal_error()
            return None

    return wrapper


def _derived(value, formula):
    return {"value": value, "from": formula}


class _ConnState:
    """Per-handler-thread context: which connection and command events belong to."""

    __slots__ = ("conn", "peer", "opened_ns", "thread", "cmd", "recv_ns", "ready_ns", "send_bytes", "verb",
                 "rejected", "found", "step", "commands", "bytes_in", "bytes_out", "close_reason")

    def __init__(self, conn, peer, opened_ns, thread):
        self.conn = conn
        self.peer = peer
        self.opened_ns = opened_ns
        self.thread = thread  # (name, ident) of the handler thread that owns this state
        self.cmd = None
        self.recv_ns = None
        self.ready_ns = None
        self.send_bytes = None
        self.verb = None
        self.rejected = False
        self.found = None
        self.step = "recv"
        self.commands = 0
        self.bytes_in = 0
        self.bytes_out = 0
        self.close_reason = None


class Observer:
    """Collects events, counters and state for exactly one Server.

    Producer side: server.py calls the hook methods below; each one is a no-op
    on internal failure (counted in diagnostics) and never blocks on consumers.
    Consumer side: subscribe(), add_listener(), recent_events(), state() and
    diagnostics(). Events are plain dicts shared between consumers; treat them
    as read-only.
    """

    def __init__(self, redact_values=False, max_chars=256, buffer_size=4096):
        if max_chars < 1 or buffer_size < 1:
            raise ValueError("max_chars and buffer_size must be at least 1")
        self.redact_values = bool(redact_values)
        self.max_chars = int(max_chars)
        self.buffer_size = int(buffer_size)
        # Guards every mutable field below. Held only for in-memory work, never
        # while blocking, and never while acquiring the store lock (lock order:
        # store lock -> self._lock).
        self._lock = threading.Lock()
        self._local = threading.local()
        self._server = None
        self._seq = 0
        self._ring = collections.deque(maxlen=self.buffer_size)
        self._ring_evicted = 0
        self._subs = []
        self._dropped_closed_subs = 0
        self._internal_errors = 0
        self._listener_errors = 0
        self._next_conn = 0
        self._next_cmd = 0
        self._conn_current = 0
        self._conn_total = 0
        self._handler_threads = set()
        self._commands = {}
        self._bytes_in = 0
        self._bytes_out = 0
        self._errors = {}
        self._locks = {}
        self._lock_stats = {}
        self._generation = 0
        self._synced_generation = 0
        self._saves_ok = 0
        self._saves_failed = 0
        self._save_ns_total = 0
        self._save_ns_max = 0
        self._last_save = None
        self._last_load = None
        self._started_ts_ns = None
        self._started_mono_ns = None
        self._stopped = False

    # ------------------------------------------------------------------ wiring

    def bind(self, server):
        """Attach to the Server that owns this observer (called by Server.__init__)."""
        if self._server is not None and self._server is not server:
            raise ValueError("an Observer belongs to exactly one Server")
        self._server = server

    def instrument_lock(self, name):
        """Return a new InstrumentedRLock that reports to this observer."""
        lock = InstrumentedRLock(self, name)
        with self._lock:
            self._locks[name] = lock
            self._lock_stats[name] = {"acquisitions": 0, "contended": 0, "failed": 0, "reentrant": 0,
                                      "wait_ns_total": 0, "wait_ns_max": 0, "hold_ns_total": 0, "hold_ns_max": 0}
        return lock

    # ---------------------------------------------------------- event plumbing

    def _text(self, s, redact=False):
        """Bounded, explicitly labelled representation of user-supplied text."""
        if s is None:
            return None
        n = len(s)
        if redact:
            return {"text": None, "chars": n, "truncated": False, "redacted": True}
        if n > self.max_chars:
            return {"text": s[:self.max_chars], "chars": n, "truncated": True, "redacted": False}
        return {"text": s, "chars": n, "truncated": False, "redacted": False}

    def _ids(self):
        """(conn, cmd, thread) of the calling handler thread, or Nones outside a connection."""
        state = getattr(self._local, "state", None)
        if state is None:
            return None, None, None
        return state.conn, state.cmd, state.thread

    def _append(self, etype, data, conn=None, cmd=None, thread=None):
        """Create and publish one event. Caller holds self._lock.

        thread is the caller's (name, ident) when already known (handler threads
        record it once per connection); otherwise it is looked up here.
        """
        if thread is None:
            current = threading.current_thread()
            thread = (current.name, current.ident)
        self._seq += 1
        event = {"v": SCHEMA_VERSION, "seq": self._seq, "ts_ns": time.time_ns(), "mono_ns": time.monotonic_ns(),
                 "type": etype, "thread": thread[0], "thread_id": thread[1], "conn": conn, "cmd": cmd,
                 "data": data}
        if len(self._ring) == self.buffer_size:
            self._ring_evicted += 1
        self._ring.append(event)
        for sub in self._subs:
            sub._offer(event)

    def _emit(self, etype, data):
        ids = self._ids()
        with self._lock:
            self._append(etype, data, *ids)

    def _count_error_locked(self, exc):
        name = type(exc).__name__
        if name not in self._errors and len(self._errors) >= _MAX_ERROR_TYPES:
            name = "other"
        self._errors[name] = self._errors.get(name, 0) + 1

    def _error_data(self, where, exc, step=None):
        return {"where": where, "step": step, "error_type": type(exc).__name__, "message": self._text(str(exc))}

    def _internal_error(self):
        with self._lock:
            self._internal_errors += 1

    def _count_listener_error(self):
        with self._lock:
            self._listener_errors += 1

    # ------------------------------------------------------- consumer plumbing

    def subscribe(self, maxsize=1024):
        """Return a Subscription receiving every event emitted from now on."""
        sub = Subscription(self, maxsize)
        with self._lock:
            self._subs.append(sub)
        return sub

    def _unsubscribe(self, sub):
        with self._lock:
            if sub in self._subs:
                self._subs.remove(sub)
                self._dropped_closed_subs += sub.dropped
            sub.closed = True

    def add_listener(self, callback, maxsize=1024, name="trace-listener"):
        """Call callback(event) for each event on a dedicated daemon thread.

        A slow callback only fills its own bounded queue (events are then dropped
        and counted); an exception from the callback is counted and ignored.
        """
        return Listener(self, callback, maxsize, name)

    def close(self):
        """Close every subscription (and so stop listeners and sinks fed by them)."""
        with self._lock:
            subs = list(self._subs)
        for sub in subs:
            sub.close()

    # ------------------------------------------------------------- server hooks

    @_contained
    def server_starting(self, server):
        with self._lock:
            self._started_ts_ns = time.time_ns()
            self._started_mono_ns = time.monotonic_ns()
            self._stopped = False
            self._append("server.start", {"host": server.host, "port": server.port,
                                          "store_path": server.kv_store.filepath, "pid": os.getpid(),
                                          "python": platform.python_version()})

    @_contained
    def server_listening(self, server):
        with self._lock:
            self._append("server.listen", {"host": server.host, "port": server.bound_port})

    @_contained
    def server_stopped(self, server):
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            self._append("server.stop", {"connections_open": self._conn_current})

    @_contained
    def error(self, where, exc):
        with self._lock:
            self._count_error_locked(exc)
            self._append("error", self._error_data(where, exc))

    @_contained
    def connection_accepted(self, address):
        with self._lock:
            self._next_conn += 1
            conn = self._next_conn
            self._conn_total += 1
            self._conn_current += 1
            self._append("conn.accept", {"peer_host": str(address[0]), "peer_port": address[1]}, conn)
        return conn

    @_contained
    def connection_opened(self, conn_id, address):
        thread = threading.current_thread()
        with self._lock:
            if conn_id is None:  # handle_client() called directly, without accept()
                self._next_conn += 1
                conn_id = self._next_conn
                self._conn_total += 1
                self._conn_current += 1
            self._handler_threads.add(thread)
            state = _ConnState(conn_id, address, time.monotonic_ns(), (thread.name, thread.ident))
            self._local.state = state
            self._append("conn.open", {"peer_host": str(address[0]), "peer_port": address[1]}, conn_id, None,
                         state.thread)

    @_contained
    def bytes_received(self, nbytes):
        now = time.monotonic_ns()
        state = self._local.state
        with self._lock:
            self._next_cmd += 1
            self._bytes_in += nbytes
            state.cmd = self._next_cmd
            state.recv_ns = now
            state.ready_ns = None
            state.verb = None
            state.rejected = False
            state.found = None
            state.step = "decode"
            state.commands += 1
            state.bytes_in += nbytes
            self._append("conn.recv", {"bytes": nbytes}, state.conn, state.cmd, state.thread)

    @_contained
    def decoded(self, message):
        state = self._local.state
        state.step = "process"
        with self._lock:
            self._append("cmd.decode", {"ok": True, "chars": len(message), "error_type": None},
                         state.conn, state.cmd, state.thread)

    @_contained
    def command_parsed(self, cmd, tokens, rejection):
        if cmd in _STORE_VERBS:
            verb = cmd
        else:
            verb = "EMPTY" if cmd is None else "UNKNOWN"
        key = value = None
        if rejection is None and verb in ("SET", "GET", "REMOVE"):
            key = self._text(tokens[1])
            if verb == "SET":
                value = self._text(tokens[2], self.redact_values)
        data = {"verb": verb, "verb_text": self._text(tokens[0]) if tokens else None,
                "argc": max(len(tokens) - 1, 0), "valid": rejection is None, "reason": rejection,
                "key": key, "value": value}
        state = getattr(self._local, "state", None)
        if state is not None:
            state.verb = verb
            state.rejected = rejection is not None
        self._emit("cmd.parse", data)

    @_contained
    def quit_requested(self):
        now = time.monotonic_ns()
        state = self._local.state
        state.verb = "QUIT"
        state.close_reason = "quit"
        with self._lock:
            self._append("conn.quit", {}, state.conn, state.cmd, state.thread)
            self._end_command_locked(state, "ok", now)

    @_contained
    def response_ready(self, nbytes):
        state = self._local.state
        state.step = "send"
        state.send_bytes = nbytes
        state.ready_ns = time.monotonic_ns()

    @_contained
    def response_sent(self):
        now = time.monotonic_ns()
        state = self._local.state
        if state.rejected:
            outcome = "rejected"
        elif state.found is False:
            outcome = "not_found"
        else:
            outcome = "ok"
        with self._lock:
            self._bytes_out += state.send_bytes
            state.bytes_out += state.send_bytes
            self._append("conn.send", {"bytes": state.send_bytes, "send_ns": now - state.ready_ns,
                                       "latency_ns": now - state.recv_ns}, state.conn, state.cmd, state.thread)
            self._end_command_locked(state, outcome, now)

    def _end_command_locked(self, state, outcome, now):
        verb = state.verb
        if verb is None:
            verb = "UNDECODED" if outcome == "decode_error" else "UNPARSED"
        counts = self._commands.setdefault(verb, {})
        counts[outcome] = counts.get(outcome, 0) + 1
        self._append("cmd.end", {"verb": verb, "outcome": outcome, "duration_ns": now - state.recv_ns},
                     state.conn, state.cmd, state.thread)
        state.cmd = None
        state.step = "recv"

    @_contained
    def peer_disconnected(self, by_server=False):
        state = self._local.state
        state.close_reason = "shutdown" if by_server else "disconnect"
        with self._lock:
            self._append("conn.disconnect", {"by": "server" if by_server else "peer"}, state.conn, None,
                         state.thread)

    @_contained
    def handler_error(self, exc):
        now = time.monotonic_ns()
        state = self._local.state
        step = state.step
        state.close_reason = "error"
        with self._lock:
            self._count_error_locked(exc)
            decode_failed = step == "decode" and isinstance(exc, UnicodeDecodeError)
            if decode_failed:
                self._append("cmd.decode", {"ok": False, "chars": None, "error_type": type(exc).__name__},
                             state.conn, state.cmd, state.thread)
            self._append("error", self._error_data("handler", exc, step), state.conn, state.cmd, state.thread)
            if state.cmd is not None:
                self._end_command_locked(state, "decode_error" if decode_failed else "error", now)

    @_contained
    def connection_closed(self):
        now = time.monotonic_ns()
        state = self._local.state
        self._local.state = None
        with self._lock:
            self._conn_current -= 1
            self._handler_threads.discard(threading.current_thread())
            self._append("conn.close", {"reason": state.close_reason, "commands": state.commands,
                                        "bytes_in": state.bytes_in, "bytes_out": state.bytes_out,
                                        "duration_ns": now - state.opened_ns}, state.conn, None, state.thread)

    # -------------------------------------------------------------- store hooks

    @_contained
    def load_skipped(self, store):
        with self._lock:
            self._last_load = {"exists": False, "ok": True}
            self._append("store.load", {"path": store.filepath, "exists": False, "ok": True,
                                        "keys": len(store.store), "bytes": None, "lines_skipped": None,
                                        "duration_ns": None, "error_type": None, "error": None}, *self._ids())

    @_contained
    def load_started(self, store):
        try:
            size = os.stat(store.filepath).st_size
        except OSError:
            size = None
        return time.monotonic_ns(), size, len(store.store)

    @_contained
    def load_finished(self, store, token, error=None, skipped=None):
        started, size, keys_before = token
        duration = time.monotonic_ns() - started
        with self._lock:
            if error is not None:
                self._synced_generation = None  # memory may hold a partial load
                self._count_error_locked(error)
            elif keys_before == 0:
                self._synced_generation = self._generation  # memory now mirrors the file
            else:
                self._synced_generation = None  # load() merged the file into existing data
            self._last_load = {"exists": True, "ok": error is None}
            self._append("store.load", {"path": store.filepath, "exists": True, "ok": error is None,
                                        "keys": len(store.store), "bytes": size, "lines_skipped": skipped,
                                        "duration_ns": duration,
                                        "error_type": None if error is None else type(error).__name__,
                                        "error": None if error is None else self._text(str(error))},
                         *self._ids())
            if error is not None:
                self._append("error", self._error_data("load", error), *self._ids())

    @_contained
    def store_lookup(self, op, key, found, keys):
        state = getattr(self._local, "state", None)
        if state is not None and found is not None:
            state.found = found
        self._emit("store.lookup", {"op": op, "key": self._text(key), "found": found, "keys": keys})

    @_contained
    def store_mutated(self, op, key, value, existed, keys):
        state = getattr(self._local, "state", None)
        if state is not None and op == "remove":
            state.found = True
        data = {"op": op, "key": self._text(key), "value": self._text(value, self.redact_values),
                "existed": existed, "keys": keys, "generation": None}
        ids = self._ids()
        with self._lock:
            self._generation += 1
            data["generation"] = self._generation
            self._append("store.mutation", data, *ids)

    @_contained
    def save_started(self, store):
        ids = self._ids()
        with self._lock:
            generation = self._generation
            self._append("store.save_start", {"path": store.filepath, "generation": generation,
                                              "keys": len(store.store)}, *ids)
        return time.monotonic_ns(), generation

    @_contained
    def save_finished(self, store, token, error=None):
        started, generation = token
        duration = time.monotonic_ns() - started
        size = mtime = None
        if error is None:
            try:  # exact bytes on disk, including any newline translation
                st = os.stat(store.filepath)
                size, mtime = st.st_size, st.st_mtime_ns
            except OSError:
                pass
        ids = self._ids()
        with self._lock:
            if error is None:
                self._saves_ok += 1
                self._synced_generation = generation
                self._last_save = {"ts_ns": time.time_ns(), "generation": generation, "bytes": size,
                                   "mtime_ns": mtime, "duration_ns": duration}
            else:
                self._saves_failed += 1
                self._count_error_locked(error)
            self._save_ns_total += duration
            self._save_ns_max = max(self._save_ns_max, duration)
            self._append("store.save_end", {"path": store.filepath, "ok": error is None, "bytes": size,
                                            "mtime_ns": mtime, "duration_ns": duration, "generation": generation,
                                            "error_type": None if error is None else type(error).__name__,
                                            "error": None if error is None else self._text(str(error))},
                         *ids)
            if error is not None:
                self._append("error", self._error_data("save", error), *ids)

    # --------------------------------------------------------------- lock hooks

    @_contained
    def _lock_wait_started(self, lock):
        holder = lock._holder
        ids = self._ids()
        with self._lock:
            lock.waiting += 1
            self._append("lock.wait_start", {"lock": lock.name, "waiting": lock.waiting,
                                             "owner_thread": holder[1] if holder else None,
                                             "owner_thread_id": holder[0] if holder else None}, *ids)

    @_contained
    def _lock_acquired(self, lock, acquired, wait_ns, contended):
        ids = self._ids()
        with self._lock:
            lock.waiting -= 1
            stats = self._lock_stats[lock.name]
            if acquired:
                stats["acquisitions"] += 1
                stats["contended"] += contended
                stats["wait_ns_total"] += wait_ns
                stats["wait_ns_max"] = max(stats["wait_ns_max"], wait_ns)
                self._append("lock.acquired", {"lock": lock.name, "wait_ns": wait_ns, "contended": contended,
                                               "waiting": lock.waiting}, *ids)
            else:
                stats["failed"] += 1
                self._append("lock.acquire_failed", {"lock": lock.name, "wait_ns": wait_ns,
                                                     "waiting": lock.waiting}, *ids)

    @_contained
    def _lock_reentered(self, lock):
        with self._lock:
            self._lock_stats[lock.name]["reentrant"] += 1

    @_contained
    def _lock_released(self, lock, hold_ns):
        ids = self._ids()
        with self._lock:
            stats = self._lock_stats[lock.name]
            stats["hold_ns_total"] += hold_ns
            stats["hold_ns_max"] = max(stats["hold_ns_max"], hold_ns)
            self._append("lock.released", {"lock": lock.name, "hold_ns": hold_ns}, *ids)

    # -------------------------------------------------------------- inspection

    def state(self, limit=1000):
        """Consistent read-only snapshot of the in-memory store and of the store file.

        The dict copy, the generation counters and the os.stat() of the file are
        all taken while holding the store lock, so no command or save can be
        half-way through. Truncation and redaction are applied afterwards.
        """
        server = self._server
        store = getattr(server, "kv_store", None)
        if store is None:
            raise LookupError("no store is bound to this observer yet")
        with store.lock:
            items = list(store.store.items())
            dict_size = sys.getsizeof(store.store)
            taken_ts, taken_mono = time.time_ns(), time.monotonic_ns()
            with self._lock:
                generation = self._generation
                synced = self._synced_generation
                last_save = dict(self._last_save) if self._last_save else None
            try:
                st = os.stat(store.filepath)
                file_state = {"exists": True, "size_bytes": st.st_size, "mtime_ns": st.st_mtime_ns,
                              "stat_error": None}
            except FileNotFoundError:
                file_state = {"exists": False, "size_bytes": None, "mtime_ns": None, "stat_error": None}
            except OSError as e:
                file_state = {"exists": None, "size_bytes": None, "mtime_ns": None, "stat_error": type(e).__name__}
        file_state["path"] = store.filepath
        file_state["synced_generation"] = synced
        file_state["last_save"] = last_save

        shown = items[:limit]
        entries = [{"key": self._text(k), "value": self._text(v, self.redact_values)} for k, v in shown]
        payload = sum(len(k.encode("utf-8")) + len(v.encode("utf-8")) for k, v in items)
        memory = dict_size + sum(sys.getsizeof(k) + sys.getsizeof(v) for k, v in items)
        if synced is None:
            reflects = unsaved = None
        else:
            reflects = synced == generation
            unsaved = generation - synced
        stat_matches = None
        if last_save is not None and file_state["exists"] is not None:
            stat_matches = (file_state["size_bytes"] == last_save["bytes"]
                            and file_state["mtime_ns"] == last_save["mtime_ns"])
        return {
            "v": SCHEMA_VERSION,
            "taken": {"ts_ns": taken_ts, "mono_ns": taken_mono},
            "store": {
                "keys": len(items),
                "generation": generation,
                "entries": entries,
                "entries_returned": len(entries),
                "entries_truncated": len(entries) < len(items),
                "payload_bytes": {"value": payload, "kind": "exact", "unit": "bytes",
                                  "method": "sum of UTF-8 encoded lengths of all keys and values"},
                "memory_bytes": {"value": memory, "kind": "estimate", "unit": "bytes",
                                 "method": "sys.getsizeof(dict) plus sys.getsizeof of each key and value; shallow, "
                                           "ignores allocator overhead and objects shared with other code"},
            },
            "file": file_state,
            "redaction": {"values_redacted": self.redact_values, "max_chars": self.max_chars},
            "derived": {
                "file_reflects_latest_change": _derived(
                    reflects, "file.synced_generation == store.generation (null when unknown)"),
                "unsaved_mutations": _derived(unsaved, "store.generation - file.synced_generation"),
                "file_matches_last_save_stat": _derived(
                    stat_matches, "file size_bytes and mtime_ns equal those recorded right after the last "
                                  "successful save (false means the file changed since, e.g. a failed save "
                                  "or an external edit)"),
            },
        }

    def recent_events(self, since=0, limit=500):
        """Events with seq > since still held in the bounded ring buffer."""
        with self._lock:
            events = list(self._ring)
            latest = self._seq
        oldest = events[0]["seq"] if events else latest + 1
        selected = [e for e in events if e["seq"] > since][:limit]
        return {
            "v": SCHEMA_VERSION,
            "since": since,
            "events": selected,
            "next_since": selected[-1]["seq"] if selected else min(since, latest),
            "oldest_seq": oldest if events else None,
            "latest_seq": latest,
            "derived": {
                "evicted_since": _derived(max(oldest - 1 - since, 0),
                                          "oldest_seq - 1 - since, floored at 0: events after 'since' that left "
                                          "the ring buffer before this request"),
            },
        }

    def diagnostics(self):
        """Counters and live state of the running server."""
        server = self._server
        store = getattr(server, "kv_store", None)
        keys = len(store.store) if store is not None else None  # len() of a dict needs no store lock
        now_ts, now_mono = time.time_ns(), time.monotonic_ns()
        with self._lock:
            handler_threads = list(self._handler_threads)
            commands = {verb: dict(outcomes) for verb, outcomes in self._commands.items()}
            locks = {}
            for name, lock in self._locks.items():
                holder = lock._holder
                stats = dict(self._lock_stats[name])
                stats.update({"waiting_now": lock.waiting,
                              "owner_thread": holder[1] if holder else None,
                              "owner_thread_id": holder[0] if holder else None})
                locks[name] = stats
            dropped = self._dropped_closed_subs + sum(sub.dropped for sub in self._subs)
            snapshot = {
                "v": SCHEMA_VERSION,
                "taken": {"ts_ns": now_ts, "mono_ns": now_mono},
                "connections": {"current": self._conn_current, "total": self._conn_total},
                "commands": commands,
                "bytes": {"received": self._bytes_in, "sent": self._bytes_out},
                "locks": locks,
                "persistence": {"saves_ok": self._saves_ok, "saves_failed": self._saves_failed,
                                "save_ns_total": self._save_ns_total, "save_ns_max": self._save_ns_max,
                                "last_save": dict(self._last_save) if self._last_save else None,
                                "last_load": dict(self._last_load) if self._last_load else None},
                "store": {"keys": keys, "generation": self._generation,
                          "synced_generation": self._synced_generation},
                "errors": dict(self._errors),
                "trace": {"emitted": self._seq, "dropped": dropped, "subscribers": len(self._subs),
                          "ring_capacity": self.buffer_size, "ring_size": len(self._ring),
                          "ring_evicted": self._ring_evicted, "internal_errors": self._internal_errors,
                          "listener_errors": self._listener_errors, "values_redacted": self.redact_values,
                          "max_chars": self.max_chars},
            }
            started_ts, started_mono = self._started_ts_ns, self._started_mono_ns
        live = [t for t in handler_threads if t.is_alive()]
        if len(live) != len(handler_threads):
            with self._lock:  # forget handler threads that died without reaching connection_closed()
                self._handler_threads.difference_update(t for t in handler_threads if not t.is_alive())
        snapshot["threads"] = {"live_handlers": len(live), "process_total": threading.active_count()}
        snapshot["server"] = {
            "host": getattr(server, "host", None), "port": getattr(server, "port", None),
            "bound_port": getattr(server, "bound_port", None),
            "store_path": store.filepath if store is not None else None,
            "max_connections": getattr(server, "max_connections", None),
            "running": bool(getattr(server, "is_running", False)), "pid": os.getpid(),
            "started_ts_ns": started_ts, "started_mono_ns": started_mono,
        }
        total_cmds = sum(sum(outcomes.values()) for outcomes in commands.values())
        lock_means = {}
        for name, stats in locks.items():
            n = stats["acquisitions"]
            lock_means[name] = {"wait_ns_mean": stats["wait_ns_total"] // n if n else None,
                                "hold_ns_mean": stats["hold_ns_total"] // n if n else None}
        snapshot["derived"] = {
            "uptime_ns": _derived(now_mono - started_mono if started_mono is not None else None,
                                  "taken.mono_ns - server.started_mono_ns"),
            "commands_total": _derived(total_cmds, "sum of all commands counts"),
            "lock_means": _derived(lock_means, "wait_ns_total / acquisitions and hold_ns_total / acquisitions "
                                               "(integer division)"),
            "unsaved_mutations": _derived(
                None if snapshot["store"]["synced_generation"] is None
                else snapshot["store"]["generation"] - snapshot["store"]["synced_generation"],
                "store.generation - store.synced_generation"),
        }
        return snapshot


class Subscription:
    """Bounded per-consumer event queue. The producer never waits: when the queue
    is full the new event is dropped for this consumer and counted in .dropped."""

    def __init__(self, observer, maxsize):
        if maxsize < 1:
            raise ValueError("maxsize must be at least 1")
        self._observer = observer
        self._queue = queue.Queue(maxsize)
        self.maxsize = maxsize
        self.dropped = 0  # updated while the observer lock is held
        self.closed = False

    def _offer(self, event):
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            self.dropped += 1

    def get(self, timeout=None):
        """Next event, or None if none arrives within timeout seconds."""
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def drain(self, limit=None):
        """Every event queued right now (up to limit), without waiting."""
        events = []
        while limit is None or len(events) < limit:
            try:
                events.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return events

    def close(self):
        self._observer._unsubscribe(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()


class Listener:
    """Runs a callback for each event on its own daemon thread (see Observer.add_listener)."""

    def __init__(self, observer, callback, maxsize, name):
        self.subscription = observer.subscribe(maxsize)
        self.errors = 0
        self._observer = observer
        self._callback = callback
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            event = self.subscription.get(timeout=0.2)
            if event is None:
                continue
            try:
                self._callback(event)
            except Exception:
                self.errors += 1
                self._observer._count_listener_error()

    def close(self, timeout=2.0):
        self._stop.set()
        self.subscription.close()
        self._thread.join(timeout)


class JsonlSink:
    """Appends every event as one JSON line to a file ("-" means stderr).

    Writing happens on the sink's own thread; if it falls behind, its bounded
    queue fills and further events are dropped and counted, never waited for.
    """

    def __init__(self, observer, path, maxsize=8192):
        if path == "-":
            self._stream, self._owns_stream = sys.stderr, False
        else:
            self._stream, self._owns_stream = open(path, "a", encoding="utf-8"), True
        self.subscription = observer.subscribe(maxsize)
        self.errors = 0
        self._observer = observer
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="trace-jsonl", daemon=True)
        self._thread.start()

    def _run(self):
        while True:
            event = self.subscription.get(timeout=0.2)
            if event is None:
                if self._stop.is_set():
                    return
                continue
            self._write([event] + self.subscription.drain())

    def _write(self, events):
        if not events:
            return
        try:
            self._stream.write("".join(json.dumps(e, separators=(",", ":")) + "\n" for e in events))
            self._stream.flush()
        except Exception:
            self.errors += 1
            self._observer._count_listener_error()

    def close(self, timeout=2.0):
        self._stop.set()
        self._thread.join(timeout)
        if not self._thread.is_alive():
            self._write(self.subscription.drain())
        self.subscription.close()
        if self._owns_stream:
            try:
                self._stream.close()
            except Exception:  # e.g. a broken pipe while flushing what is left
                self.errors += 1
                self._observer._count_listener_error()


class InstrumentedRLock:
    """threading.RLock wrapper that reports waits and holds to an Observer.

    Keeps RLock semantics: re-entrant, usable as a context manager, same
    acquire(blocking=True, timeout=-1) / release() behaviour and errors. Only a
    thread's outermost acquire/release is reported; nested acquisitions by the
    owner never wait and are only counted. The wrapper adds no ordering or
    fairness: whichever waiter the underlying RLock wakes gets the lock.
    """

    def __init__(self, observer, name):
        self._lock = threading.RLock()
        self._observer = observer
        self.name = name
        self.waiting = 0       # threads currently inside acquire(); maintained under the observer lock
        self._holder = None    # (thread ident, thread name) of the owner, assigned as one tuple
        self._depth = 0
        self._acquired_ns = 0

    def acquire(self, blocking=True, timeout=-1):
        if not blocking and timeout != -1:
            raise ValueError("can't specify a timeout for a non-blocking call")
        me = threading.get_ident()
        holder = self._holder
        if holder is not None and holder[0] == me:
            self._lock.acquire()  # re-entrant: the owner never blocks
            self._depth += 1
            self._observer._lock_reentered(self)
            return True
        self._observer._lock_wait_started(self)
        start = time.monotonic_ns()
        acquired = self._lock.acquire(False)
        contended = not acquired
        if contended and blocking:
            acquired = self._lock.acquire(True, timeout)
        now = time.monotonic_ns()
        if acquired:
            self._holder = (me, threading.current_thread().name)
            self._depth = 1
            self._acquired_ns = now
        self._observer._lock_acquired(self, acquired, now - start, contended)
        return acquired

    __enter__ = acquire

    def release(self):
        holder = self._holder
        if holder is None or holder[0] != threading.get_ident():
            self._lock.release()  # not ours: raises RuntimeError exactly like RLock
            return
        if self._depth > 1:
            self._depth -= 1
            self._lock.release()
            return
        hold_ns = time.monotonic_ns() - self._acquired_ns
        # Reported while still holding the lock (an in-memory append, no I/O), so that in the trace
        # every release of the lock comes before the next acquisition, exactly as it happens.
        self._observer._lock_released(self, hold_ns)
        self._holder = None
        self._depth = 0
        self._lock.release()

    def __exit__(self, *exc_info):
        self.release()

    def _is_owned(self):
        holder = self._holder
        return holder is not None and holder[0] == threading.get_ident()

    def __repr__(self):
        holder = self._holder
        return "<InstrumentedRLock %r owner=%r depth=%d waiting=%d>" % (
            self.name, holder[1] if holder else None, self._depth, self.waiting)


class _BadRequest(Exception):
    pass


def _int_param(query, name, default, minimum, maximum=None):
    values = query.get(name)
    if not values:
        return default
    try:
        value = int(values[-1])
    except ValueError:
        raise _BadRequest("%s must be an integer" % name)
    if value < minimum or (maximum is not None and value > maximum):
        raise _BadRequest("%s must be between %s and %s" % (name, minimum, maximum))
    return value


class _InspectionHandler(http.server.BaseHTTPRequestHandler):
    server_version = "kvs-inspect/%d" % SCHEMA_VERSION
    timeout = 5  # seconds; a stalled HTTP client cannot hold the inspector forever

    def do_GET(self):
        self._dispatch(send_body=True)

    def do_HEAD(self):
        self._dispatch(send_body=False)

    def _reject_method(self):
        self._send(405, {"error": "read-only endpoint: only GET and HEAD are allowed"}, True,
                   {"Allow": "GET, HEAD"})

    do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _reject_method

    def log_message(self, format, *args):
        pass  # keep the server's stdout and stderr exactly as they were

    def _dispatch(self, send_body):
        observer = self.server.observer
        allowed = self.server.allowed_hosts
        if allowed is not None and self.headers.get("Host", "") not in allowed:
            self._send(403, {"error": "unexpected Host header"}, send_body)
            return
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        try:
            if url.path == "/":
                payload = {"v": SCHEMA_VERSION, "read_only": True,
                           "endpoints": {"/state": "store snapshot and store-file state (?limit=N entries)",
                                         "/diagnostics": "counters, live connections, threads, lock and "
                                                         "persistence statistics",
                                         "/events": "recent events from the ring buffer (?since=SEQ&limit=N)",
                                         "/schema": "event schema"}}
            elif url.path == "/state":
                payload = observer.state(limit=_int_param(query, "limit", 1000, 0))
            elif url.path == "/diagnostics":
                payload = observer.diagnostics()
            elif url.path == "/events":
                payload = observer.recent_events(since=_int_param(query, "since", 0, 0),
                                                 limit=_int_param(query, "limit", 500, 1, observer.buffer_size))
            elif url.path == "/schema":
                payload = event_schema()
            else:
                self._send(404, {"error": "not found"}, send_body)
                return
        except _BadRequest as e:
            self._send(400, {"error": str(e)}, send_body)
            return
        except LookupError as e:
            self._send(503, {"error": str(e)}, send_body)
            return
        except Exception as e:
            observer._internal_error()
            self._send(500, {"error": type(e).__name__}, send_body)
            return
        self._send(200, payload, send_body)

    def _send(self, status, payload, send_body, headers=None):
        body = json.dumps(payload, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if send_body:
            self.wfile.write(body)


class _InspectionHTTPServer(http.server.HTTPServer):
    def server_bind(self):
        # HTTPServer.server_bind() would do a reverse DNS lookup (socket.getfqdn); skip it.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class _InspectionHTTPServer6(_InspectionHTTPServer):
    address_family = socket.AF_INET6


class InspectionServer:
    """Read-only HTTP endpoint serving state(), diagnostics(), recent_events() and the schema.

    Serves one request at a time on its own daemon thread. It has no access to
    the command parser, so it can never execute a store command.
    """

    def __init__(self, observer, host="127.0.0.1", port=0):
        server_class = _InspectionHTTPServer6 if ":" in host else _InspectionHTTPServer
        self._httpd = server_class((host, port), _InspectionHandler)
        self._httpd.observer = observer
        bound_host, bound_port = self._httpd.server_address[:2]
        self.address = (bound_host, bound_port)
        if host in ("127.0.0.1", "localhost", "::1"):
            # Only answer requests addressed to the loopback interface (defends against DNS rebinding).
            names = ("127.0.0.1", "localhost", "[::1]")
            self._httpd.allowed_hosts = frozenset(names + tuple("%s:%d" % (n, bound_port) for n in names))
        else:
            self._httpd.allowed_hosts = None
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="inspect-http", daemon=True)
        self._thread.start()

    @property
    def url(self):
        host = self.address[0]
        return "http://%s:%d/" % ("[%s]" % host if ":" in host else host, self.address[1])

    def close(self):
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(2.0)


def _field(type_, kind, meaning, unit=None, clock=None):
    field = {"type": type_, "kind": kind, "meaning": meaning}
    if unit:
        field["unit"] = unit
    if clock:
        field["clock"] = clock
    return field


def event_schema():
    """Machine-readable description of every event type (also served at /schema).

    kind: "actual" = state read from the server, "measured" = timestamp, duration,
    size or count of something that happened, "derived" = computed from those,
    "id" = identifier assigned by the observer, "config" = configured setting.
    """
    text = "TextField"
    T = _field
    path = T("str", "config", "store file path as configured")
    lock = T("str", "actual", "name of the instrumented lock (always \"store\")")
    err_type = T("str|null", "actual", "exception class name, null on success")
    err_text = T("TextField|null", "actual", "str(exception), bounded; null on success")
    return {
        "version": SCHEMA_VERSION,
        "envelope": {
            "v": T("int", "config", "schema version"),
            "seq": T("int", "id", "per-server sequence number: 1 for the first event, +1 per event, no gaps "
                                  "at emission (consumers can still miss events, see drops)"),
            "ts_ns": T("int", "measured", "wall-clock time at emission, time.time_ns(); for display only, "
                                          "may jump", "ns since the Unix epoch", "wall"),
            "mono_ns": T("int", "measured", "time.monotonic_ns() at emission; never decreases as seq increases",
                         "ns", "monotonic"),
            "type": T("str", "actual", "event type, see events"),
            "thread": T("str", "actual", "name of the thread that emitted the event"),
            "thread_id": T("int", "actual", "threading.get_ident() of the emitting thread"),
            "conn": T("int|null", "id", "connection id, per server, from 1; null outside a connection"),
            "cmd": T("int|null", "id", "command id, per server, from 1; one per non-empty recv(); null outside "
                                       "a command"),
            "data": T("object", "actual", "type-specific fields, see events"),
        },
        "text_field": {
            "text": T("str|null", "actual", "the text, cut to max_chars; null when redacted"),
            "chars": T("int", "measured", "length of the original text in characters"),
            "truncated": T("bool", "actual", "true when text was cut to max_chars"),
            "redacted": T("bool", "config", "true when the value was hidden by --redact-values"),
        },
        "events": {
            "server.start": {"category": "server", "description": "Server.start() was called", "data": {
                "host": T("str", "config", "configured listen host"),
                "port": T("int", "config", "configured port (0 = ephemeral)"),
                "store_path": path,
                "pid": T("int", "actual", "process id"),
                "python": T("str", "actual", "Python version"),
            }},
            "server.listen": {"category": "server", "description": "listening socket bound and listening", "data": {
                "host": T("str", "config", "listen host"),
                "port": T("int", "actual", "port actually bound (from getsockname)"),
            }},
            "server.stop": {"category": "server", "description": "Server.stop() closed the listening socket and "
                                                               "the connections that were still open",
                            "data": {"connections_open": T("int", "actual", "connections whose handler had still "
                                                                            "not finished after the shutdown "
                                                                            "grace period (normally 0)")}},
            "conn.accept": {"category": "connection", "description": "accept() returned a client (main thread)",
                            "data": {"peer_host": T("str", "actual", "client address"),
                                     "peer_port": T("int", "actual", "client port")}},
            "conn.open": {"category": "connection", "description": "handler thread started for the connection",
                          "data": {"peer_host": T("str", "actual", "client address"),
                                   "peer_port": T("int", "actual", "client port")}},
            "conn.recv": {"category": "connection", "description": "recv(1024) returned data; starts a command",
                          "data": {"bytes": T("int", "measured", "bytes returned by this recv()", "bytes")}},
            "cmd.decode": {"category": "command", "description": "outcome of decoding the received bytes as UTF-8",
                           "data": {"ok": T("bool", "measured", "decode succeeded"),
                                    "chars": T("int|null", "measured", "characters after decode() and strip()"),
                                    "error_type": err_type}},
            "cmd.parse": {"category": "command", "description": "CommandParser validated the message", "data": {
                "verb": T("str", "actual", "SET, GET, REMOVE, PRINT, EMPTY or UNKNOWN"),
                "verb_text": T(text + "|null", "actual", "first token exactly as sent"),
                "argc": T("int", "actual", "number of tokens after the verb"),
                "valid": T("bool", "actual", "true when the command will be executed"),
                "reason": T("str|null", "actual", "why it was rejected: empty, arity or unknown"),
                "key": T(text + "|null", "actual", "key of a valid SET/GET/REMOVE"),
                "value": T(text + "|null", "actual", "value of a valid SET (redactable)"),
            }},
            "conn.quit": {"category": "connection", "description": "client sent quit; the handler stops", "data": {}},
            "lock.wait_start": {"category": "lock", "description": "a thread entered acquire() on the lock", "data": {
                "lock": lock,
                "waiting": T("int", "actual", "threads inside acquire() on this lock, including this one; a "
                                              "count, not a queue order"),
                "owner_thread": T("str|null", "actual", "thread recorded as holder at this instant. Non-null "
                                                        "means it really held the lock then; null means free, "
                                                        "or held but inside the few-bytecode window at the very "
                                                        "start or end of a hold, so a contended acquisition can "
                                                        "follow a null owner"),
                "owner_thread_id": T("int|null", "actual", "ident of that holder"),
            }},
            "lock.acquired": {"category": "lock", "description": "acquire() obtained the lock", "data": {
                "lock": lock,
                "wait_ns": T("int", "measured", "time spent inside the underlying acquire", "ns", "monotonic"),
                "contended": T("bool", "measured", "a non-blocking first attempt failed, i.e. another thread "
                                                   "held the lock"),
                "waiting": T("int", "actual", "threads still inside acquire() after this one left"),
            }},
            "lock.acquire_failed": {"category": "lock", "description": "non-blocking or timed acquire() gave up",
                                    "data": {"lock": lock,
                                             "wait_ns": T("int", "measured", "time spent trying", "ns", "monotonic"),
                                             "waiting": T("int", "actual", "threads still inside acquire()")}},
            "lock.released": {"category": "lock", "description": "outermost release() by the owner, emitted just "
                                                                 "before the underlying release, so acquired and "
                                                                 "released events of the lock strictly alternate",
                              "data": {
                                  "lock": lock,
                                  "hold_ns": T("int", "measured", "from acquisition to the start of release, "
                                                                  "including any event emission inside the "
                                                                  "critical section", "ns", "monotonic"),
                              }},
            "store.load": {"category": "persistence", "description": "KeyValueStore.load() finished", "data": {
                "path": path,
                "exists": T("bool", "actual", "the file existed"),
                "ok": T("bool", "actual", "no exception while loading"),
                "keys": T("int", "actual", "keys in memory after loading"),
                "bytes": T("int|null", "measured", "file size from os.stat() before reading", "bytes"),
                "lines_skipped": T("int|null", "measured", "non-empty lines without a tab, which load() skipped; "
                                                           "null when the file is missing or loading failed"),
                "duration_ns": T("int|null", "measured", "time spent loading", "ns", "monotonic"),
                "error_type": err_type, "error": err_text,
            }},
            "store.lookup": {"category": "store", "description": "a read of the store under the lock", "data": {
                "op": T("str", "actual", "get, remove (key missing, nothing changed) or print"),
                "key": T(text + "|null", "actual", "key looked up; null for print"),
                "found": T("bool|null", "actual", "key was present; null for print"),
                "keys": T("int", "actual", "keys in the store"),
            }},
            "store.mutation": {"category": "store", "description": "the in-memory dict was changed", "data": {
                "op": T("str", "actual", "set or remove"),
                "key": T(text, "actual", "key changed"),
                "value": T(text + "|null", "actual", "new value for set (redactable), null for remove"),
                "existed": T("bool", "actual", "key was present before the change"),
                "keys": T("int", "actual", "keys in the store after the change"),
                "generation": T("int", "measured", "mutation counter after this change (+1 per mutation)"),
            }},
            "store.save_start": {"category": "persistence", "description": "save() starts rewriting the file",
                                 "data": {"path": path,
                                          "generation": T("int", "measured", "mutation generation being written"),
                                          "keys": T("int", "actual", "keys being written")}},
            "store.save_end": {"category": "persistence", "description": "save() finished", "data": {
                "path": path,
                "ok": T("bool", "actual", "file written and closed without exception"),
                "bytes": T("int|null", "measured", "exact file size from os.stat() after close", "bytes"),
                "mtime_ns": T("int|null", "actual", "file mtime from that os.stat()", "ns since the Unix epoch",
                              "wall"),
                "duration_ns": T("int", "measured", "open, write and close", "ns", "monotonic"),
                "generation": T("int", "measured", "mutation generation this save wrote"),
                "error_type": err_type, "error": err_text,
            }},
            "conn.send": {"category": "connection", "description": "sendall() of the response returned", "data": {
                "bytes": T("int", "measured", "length of the encoded response", "bytes"),
                "send_ns": T("int", "measured", "duration of sendall()", "ns", "monotonic"),
                "latency_ns": T("int", "measured", "from recv() returning to sendall() returning", "ns",
                                "monotonic"),
            }},
            "cmd.end": {"category": "command", "description": "a command finished (one per conn.recv)", "data": {
                "verb": T("str", "actual", "SET, GET, REMOVE, PRINT, QUIT, EMPTY, UNKNOWN, UNDECODED or UNPARSED"),
                "outcome": T("str", "actual", "ok, not_found, rejected, decode_error or error"),
                "duration_ns": T("int", "measured", "from recv() returning to this event", "ns", "monotonic"),
            }},
            "conn.disconnect": {"category": "connection", "description": "recv() returned 0 bytes, or failed "
                                                                     "because stop() closed the connection",
                                "data": {
                "by": T("str", "actual", "peer (the client closed) or server (stop() shut the connection down)"),
            }},
            "conn.close": {"category": "connection", "description": "handler finished; socket closed", "data": {
                "reason": T("str|null", "actual", "quit, disconnect, shutdown or error"),
                "commands": T("int", "measured", "commands received on this connection"),
                "bytes_in": T("int", "measured", "bytes received", "bytes"),
                "bytes_out": T("int", "measured", "bytes sent", "bytes"),
                "duration_ns": T("int", "measured", "from conn.open to close", "ns", "monotonic"),
            }},
            "error": {"category": "error", "description": "an exception was caught", "data": {
                "where": T("str", "actual", "handler, accept, setup, load or save"),
                "step": T("str|null", "actual", "handler step that failed: recv, decode, process or send"),
                "error_type": T("str", "actual", "exception class name"),
                "message": T(text, "actual", "str(exception), bounded"),
            }},
        },
    }
