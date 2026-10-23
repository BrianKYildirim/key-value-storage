# Multithreaded TCP Key-Value Store

A small key-value server written in Python with only the standard library. Clients connect over TCP and use a simple text protocol to store, read and delete short values by key. The server saves every change to a text file, so the data is still there after a restart.

It also has an optional **observability mode**. In that mode the server reports what it is doing internally: connections, threads, waits for the shared lock, and file writes. It reports these as structured data, measured from the running server.

This is an educational project, not a production database. There is no authentication or encryption, and the protocol is deliberately simple. The aim is to explore sockets, threads, locking and persistence, and to make that behaviour visible and testable.

## Demo

The video below shows how to start the server, connect with the client and run the commands.

https://github.com/user-attachments/assets/721b5ab5-2df6-498e-96f0-17ff72d300ff

It was recorded before the observability features and the fixes described in this README were added. The workflow it shows is unchanged.

## How it works, in plain terms

Think of the server as a front desk with one shared notebook.

- Several people (clients) can talk to the desk at the same time. Each one gets their own clerk, which in the code is a thread.
- The notebook has a single pen, which in the code is a lock. Only one clerk can read or change the notebook at a time, so two people can never overwrite each other's changes halfway through.
- After every change, the clerk copies the whole notebook into a file (`store.txt`). When the server starts again, it reads that file back.
- The desk serves at most 100 visitors at once by default. A clerk stays with their visitor until that visitor leaves, so when the desk is full, a new visitor waits in line until someone leaves.
- At closing time (Ctrl+C), each clerk finishes the request they are working on and then hangs up, and the desk closes.

Observability mode puts a window on the desk. You can watch who arrives, who is waiting for the pen and for how long, and when the notebook is copied to disk. You can look, but you can't change anything through it.

## Features

- TCP server with one thread per connected client, capped at a configurable number of clients (100 by default)
- Commands: `SET`, `GET`, `REMOVE`, `PRINT` and `quit`. Command names are case-insensitive.
- A shared in-memory dictionary protected by a re-entrant lock
- Persistence to `store.txt`, which is UTF-8, rewritten after every change and loaded at startup
- Clean shutdown on Ctrl+C: commands already running finish, and open connections are closed
- An interactive command-line client
- Optional observability: a structured event trace, a read-only HTTP inspection endpoint and an in-process API
- No third-party dependencies, for either the server or the tests
- A `unittest` test suite and a small load-testing script

## Requirements

- Python 3.8 or newer. The code avoids language features and library functions added after 3.8. The test suite has been run on Python 3.14 (Linux, under WSL2) and Python 3.10 (Windows), but not on 3.8 itself.
- Nothing to install.

## Quick start

### 1. Get the code

```bash
git clone https://github.com/BrianKYildirim/key-value-storage.git
cd key-value-storage
```

### 2. Start the server

```bash
python server.py
```

```text
Server listening on 0.0.0.0:3490
```

The server listens on port 3490 on all network interfaces. It keeps its data in `store.txt` in the directory you start it from.

### 3. Start the client

In a second terminal:

```bash
python client.py
```

The client always connects to `localhost:3490`. Here is a real session:

```text
Connected to server at localhost:3490
Enter command (or 'quit' to exit): SET username brian
Response: Added key 'username' with value 'brian'

Enter command (or 'quit' to exit): GET username
Response: brian
Enter command (or 'quit' to exit): set language Python
Response: Added key 'language' with value 'Python'

Enter command (or 'quit' to exit): GET nobody
Response: Key 'nobody' not found.
Enter command (or 'quit' to exit): PRINT
Response: [KEY]: username	[VALUE]: brian
[KEY]: language	[VALUE]: Python

Enter command (or 'quit' to exit): REMOVE username
Response: Removed key 'username'.

Enter command (or 'quit' to exit): quit
Exiting client.
```

Some responses are followed by a blank line and others are not. That's because most server replies end with a newline, while `GET` replies and "not found" messages don't (see [Command reference](#command-reference)).

Meanwhile, the server prints one line for everything that happens:

```text
Connection from ('127.0.0.1', 57728)
Received from ('127.0.0.1', 57728): SET username brian
Response sent to ('127.0.0.1', 57728): Added key 'username' with value 'brian'
...
Client ('127.0.0.1', 57728) requested to quit.
Connection with ('127.0.0.1', 57728) closed.
```

Press Ctrl+C in the server terminal to stop it:

```text
Server is shutting down gracefully.
Server socket closed.
```

## Command reference

| Command | What it does | Reply |
|---|---|---|
| `SET key value` | Stores a value, or replaces the existing one | `Added key 'key' with value 'value'` (also when replacing) |
| `GET key` | Reads a value | The value itself, or `Key 'key' not found.` |
| `REMOVE key` | Deletes a key | `Removed key 'key'.` or `Key 'key' not found.` |
| `PRINT` | Lists every key and value, in the order the keys were first added | `[KEY]: key<TAB>[VALUE]: value`, one line per key, or `Store is empty.` |
| `quit` | Ends your connection | No reply; the server closes the connection |

Rules and details:

- **Case.** Command names are case-insensitive (`get`, `GET` and `GeT` all work). Keys and values are case-sensitive.
- **One word each.** Keys and values cannot contain spaces. The server splits a command on whitespace, so `SET name Brian Yildirim` has three arguments and is rejected.
- **Errors.** Mistakes produce one of these replies:
  - `ERROR: Empty command`
  - `ERROR: Unknown command 'FOO'`
  - `ERROR: SET command requires 2 arguments: key and value`
  - `ERROR: GET command requires 1 argument: key`
  - `ERROR: REMOVE command requires 1 argument: key`
- **Trailing newlines.** Most replies end with a newline. `GET` replies and `Key '…' not found.` don't. This matters if you write your own client.
- **Empty lines.** The client ignores empty lines, and `quit` works with or without surrounding spaces.

## How it works, technically

### Networking and threads

- `server.py` opens an IPv4 TCP socket with `SO_REUSEADDR`, binds it and calls `listen(10)`.
- The main thread loops on `accept()` and starts one daemon thread per connection. The threads are named `client <ip>:<port>`.
- A `threading.BoundedSemaphore` limits how many client threads exist at once (`--max-connections`, default 100). While every slot is taken, the server stops accepting. New clients wait in the operating system's listen backlog until a slot frees up; once that backlog is full, the OS may delay or refuse further connections.

### Protocol and framing

There is no message framing: **each `recv(1024)` the server performs is treated as one complete command**. That works well for interactive use, where you send one command and wait for its reply, but it has consequences:

- A command longer than 1024 bytes is split into two commands.
- Two commands sent back to back, without waiting for a reply, can arrive in a single `recv()` and be rejected as one malformed command.
- A reply longer than 1024 bytes, such as `PRINT` on a large store, arrives in more than one piece. `client.py` reads once per command, so the rest of the reply appears in front of the next one.
- A multi-byte UTF-8 character cut in half at the 1024-byte boundary can't be decoded, and the server closes that connection.

### Shared state and locking

- All clients share one `KeyValueStore`: a Python dictionary plus a `threading.RLock`.
- Every read and write takes the lock.
- `SET` and `REMOVE` hold the lock while they change the dictionary *and* rewrite the file. Two clients' writes therefore never interleave, and the file is written in the same order as the changes were made.

### Persistence

- `store.txt` holds one `key<TAB>value` pair per line. It is always written as UTF-8 with `\n` line endings, on every platform.
- The whole file is rewritten after every successful `SET` and `REMOVE`, so writes get slower as the store grows.
- At startup the file is read back. A non-empty line without a tab is skipped and reported, for example `Skipped 2 malformed line(s) in store.txt: expected 'key<TAB>value'.`.
- Earlier versions of this project wrote the file in the platform's default encoding. If a file isn't valid UTF-8, the server reads it in that encoding instead, and the next save converts it to UTF-8. One side effect: on Windows that encoding (usually cp1252) accepts almost any byte, so a damaged file there is read with wrong characters instead of being reported as an error.
- To start over with an empty store, stop the server and delete `store.txt`.

The file-handling limitations are listed under [Known limitations](#known-limitations).

### Shutdown

Ctrl+C, or `Server.shutdown()` when the server is embedded in Python code, stops the accept loop and closes the listening socket. Then, for each connection that is still open:

1. The server closes the *receiving* side of the connection. A command that is already running finishes, including its file write and its reply. Each handler then closes its connection and prints `Closing connection with (…): server is shutting down.`.
2. After up to 1 second, the server fully closes any connection that is still open. This step is needed on Windows, where step 1 does not wake a connection that is idle and waiting for input, so shutting down there takes about a second when idle clients are connected. On Linux, idle connections close immediately in step 1.
3. The server waits up to 5 seconds in total for these threads. Anything still running after that is ended when the process exits, because the threads are daemon threads.

On Windows, if a client has already sent its next command when the shutdown starts, Windows resets the connection and the reply to the running command may be lost. That command itself still completes.

### Logging

The human-readable lines go to standard output.

- **Buffered when redirected.** When stdout is redirected to a file or pipe, Python buffers it, so the lines appear late (often only at exit) and are lost if the process is killed. Run `python -u server.py > server.log` to write them immediately.
- **Encoding.** If stdout can't encode a character (for example under an ASCII-only locale), printing that line fails and the server closes that client's connection. When the character is in the command itself, this happens before the command runs.

## Configuration

`python server.py` with no options uses the defaults below. Each option can also be set with an environment variable. A command-line flag takes precedence over the environment variable.

| Option | Environment variable | Default | Meaning |
|---|---|---|---|
| `--host HOST` | `KVS_HOST` | `0.0.0.0` | Address to listen on. Use `127.0.0.1` to accept local connections only. |
| `--port PORT` | `KVS_PORT` | `3490` | TCP port. `0` lets the OS pick a free port. The startup line shows the real port, and it is also printed straight away on stderr as `Listening on HOST:PORT`, which helps when stdout is buffered. |
| `--store PATH` | `KVS_STORE` | `store.txt` | Persistence file, relative to the current directory. |
| `--max-connections N` | `KVS_MAX_CONNECTIONS` | `100` | Clients served at the same time; more wait until one leaves. |
| `--trace-jsonl PATH` | `KVS_TRACE_JSONL` | off | Observability: append every event as one JSON line (`-` = stderr). |
| `--inspect-port PORT` | `KVS_INSPECT_PORT` | off | Observability: serve the read-only inspection endpoint (`0` = free port). |
| `--inspect-host HOST` | `KVS_INSPECT_HOST` | `127.0.0.1` | Observability: address of the inspection endpoint. |
| `--redact-values` | `KVS_REDACT_VALUES=1` | off | Observability: hide values (not keys) in traces and inspection output. |
| `--trace-max-chars N` | | `256` | Observability: cut keys, values and messages to N characters. |
| `--trace-buffer N` | | `4096` | Observability: number of recent events kept for `/events`. |

`client.py` has no options. To connect it somewhere else, change `Client()` at the bottom of the file, for example to `Client(port=4000)`.

## Observability (optional)

### What it is

Most servers tell you very little about what happens inside them. This one can produce a step-by-step record of everything it does, plus a live view of its state. Every number in that record is one of three things:

- something read directly from the running server (like the current keys),
- something measured (like how long a thread waited for the lock), or
- a simple calculation from those two. Calculated values are always labelled with their formula.

Nothing is simulated or estimated without saying so.

All of it is off unless you turn it on. When it's off, the only extra work per command is a quick "is tracing on?" check at each instrumentation point (see [What observability costs](#what-observability-costs)). Observability never changes the replies clients get, and it never adds commands to the client protocol. It uses separate channels instead.

### Turning it on

```bash
# Every event as a line of JSON in trace.jsonl
python server.py --trace-jsonl trace.jsonl

# A read-only HTTP endpoint on localhost
python server.py --inspect-port 8765
curl -s http://127.0.0.1:8765/state
curl -s http://127.0.0.1:8765/diagnostics
curl -s "http://127.0.0.1:8765/events?since=0&limit=100"

# Both at once
python server.py --trace-jsonl trace.jsonl --inspect-port 8765
```

From Python, for tests or for embedding the server in another program:

```python
import threading
import observability
import server

observer = observability.Observer()                  # one observer per server
events = observer.subscribe(maxsize=10000)           # subscribe first to also see the startup events
srv = server.Server(host="127.0.0.1", port=0, store_path="demo-store.txt", observer=observer)
threading.Thread(target=srv.start, daemon=True).start()
srv.ready.wait()
print("listening on port", srv.bound_port)

event = events.get(timeout=1.0)    # next event, or None after the timeout; events.drain() returns all queued
observer.add_listener(print)       # call a function for every event, on its own thread
observer.state()                   # same data as /state
observer.diagnostics()             # same data as /diagnostics
srv.shutdown()                     # stops the server from any thread
```

### What a trace looks like

These are the real events for one `SET username brian`, from `trace.jsonl`. The `ts_ns`, `mono_ns`, `thread` and `thread_id` fields are cut out here with `…`:

```text
{"v":1,"seq":6,…,"type":"conn.recv",…,"conn":1,"cmd":1,"data":{"bytes":18}}
{"v":1,"seq":7,…,"type":"cmd.decode",…,"data":{"ok":true,"chars":18,"error_type":null}}
{"v":1,"seq":8,…,"type":"cmd.parse",…,"data":{"verb":"SET","verb_text":{…},"argc":2,"valid":true,"reason":null,"key":{"text":"username",…},"value":{"text":"brian",…}}}
{"v":1,"seq":9,…,"type":"lock.wait_start",…,"data":{"lock":"store","waiting":1,"owner_thread":null,"owner_thread_id":null}}
{"v":1,"seq":10,…,"type":"lock.acquired",…,"data":{"lock":"store","wait_ns":1743,"contended":false,"waiting":0}}
{"v":1,"seq":11,…,"type":"store.mutation",…,"data":{"op":"set","key":{"text":"username",…},"value":{"text":"brian",…},"existed":false,"keys":1,"generation":1}}
{"v":1,"seq":12,…,"type":"store.save_start",…,"data":{"path":"store.txt","generation":1,"keys":1}}
{"v":1,"seq":13,…,"type":"store.save_end",…,"data":{"path":"store.txt","ok":true,"bytes":15,"mtime_ns":1792788259931507991,"duration_ns":600415,"generation":1,"error_type":null,"error":null}}
{"v":1,"seq":14,…,"type":"lock.released",…,"data":{"lock":"store","hold_ns":750230}}
{"v":1,"seq":15,…,"type":"conn.send",…,"data":{"bytes":40,"send_ns":275364,"latency_ns":1152239}}
{"v":1,"seq":16,…,"type":"cmd.end",…,"data":{"verb":"SET","outcome":"ok","duration_ns":1152239}}
```

Read from top to bottom, this says:
1. 18 bytes arrived and decoded as a valid `SET`.
2. The thread got the lock almost immediately (1.7 µs, uncontended).
3. It added a new key, then rewrote `store.txt` (15 bytes, about 0.6 ms).
4. It released the lock after holding it for about 0.75 ms.
5. It sent a 40-byte reply about 1.15 ms after the command arrived.

### Inspection endpoint

The endpoint is plain HTTP and serves JSON. It only accepts `GET` and `HEAD`, and it has no way to run store commands.

| Route | Returns |
|---|---|
| `GET /` | The list of routes. |
| `GET /state?limit=N` | A consistent snapshot: up to `N` entries (default 1000) in `PRINT` order, the key count, a mutation counter (`generation`), the exact payload size, an estimated memory size, and the state of `store.txt` (exists, size, modification time, last successful save). |
| `GET /diagnostics` | Counters and live state: current and total connections, live client threads, commands by type and outcome, bytes in and out, lock statistics (acquisitions, contended acquisitions, total and maximum wait and hold times, threads waiting now, current owner), save and load statistics, key count, errors by exception type, uptime, and the trace's own counters (events emitted, dropped and evicted). |
| `GET /events?since=SEQ&limit=N` | Recent events with a sequence number above `SEQ` (default limit 500). To follow along, poll again with `since` set to the `next_since` value you received. |
| `GET /schema` | The event schema as JSON. |

Errors come back as JSON with an `error` field:
- `400` for a bad parameter
- `403` when the `Host` header isn't a localhost name
- `404` for an unknown path
- `405` for any method other than `GET` or `HEAD`
- `503` when no store exists yet

Part of a real `/state` response after `SET username brian`:

```json
{
  "store": {
    "keys": 1,
    "generation": 1,
    "entries": [{"key": {"text": "username", "chars": 8, "truncated": false, "redacted": false},
                 "value": {"text": "brian", "chars": 5, "truncated": false, "redacted": false}}],
    "payload_bytes": {"value": 13, "kind": "exact", "unit": "bytes", "method": "sum of UTF-8 encoded lengths of all keys and values"},
    "memory_bytes": {"value": 279, "kind": "estimate", "unit": "bytes", "method": "sys.getsizeof(dict) plus sys.getsizeof of each key and value; …"}
  },
  "file": {"exists": true, "size_bytes": 15, "path": "store.txt", "synced_generation": 1, "last_save": {"bytes": 15, "duration_ns": 600415, "…": "…"}, "…": "…"},
  "derived": {
    "file_reflects_latest_change": {"value": true, "from": "file.synced_generation == store.generation (null when unknown)"},
    "unsaved_mutations": {"value": 0, "from": "store.generation - file.synced_generation"},
    "file_matches_last_save_stat": {"value": true, "from": "file size_bytes and mtime_ns equal those recorded right after the last successful save …"}
  }
}
```

And part of `/diagnostics` from the same run, after one `SET` and two `GET`s:

```json
{
  "connections": {"current": 1, "total": 1},
  "commands": {"SET": {"ok": 1}, "GET": {"ok": 1, "not_found": 1}},
  "bytes": {"received": 40, "sent": 68},
  "locks": {"store": {"acquisitions": 4, "contended": 0, "wait_ns_max": 2089, "hold_ns_max": 750230, "waiting_now": 0, "owner_thread": null, "…": "…"}},
  "threads": {"live_handlers": 1, "process_total": 4},
  "trace": {"emitted": 37, "dropped": 0, "ring_evicted": 0, "internal_errors": 0, "…": "…"},
  "derived": {"uptime_ns": {"value": 348780591, "from": "taken.mono_ns - server.started_mono_ns"}, "…": "…"}
}
```

### Event reference

Every event is one JSON object with the same envelope: `v`, `seq`, `ts_ns`, `mono_ns`, `type`, `thread`, `thread_id`, `conn`, `cmd`, and a type-specific `data` object.

Each field has a *kind*:
- `actual`: state read from the server
- `measured`: a timestamp, duration, size or count of something that happened
- `id`: an identifier assigned by the observer
- `config`: a configured setting

All durations are differences of `time.monotonic_ns()`, in nanoseconds.

A command produces events in this order. Every command ends with exactly one `cmd.end`:

| Command | Events (all with the same `conn` and `cmd`) |
|---|---|
| `SET`, or `REMOVE` of an existing key | `conn.recv`, `cmd.decode`, `cmd.parse`, `lock.wait_start`, `lock.acquired`, `store.mutation`, `store.save_start`, `store.save_end`, `lock.released`, `conn.send`, `cmd.end` |
| `GET`, `PRINT`, or `REMOVE` of a missing key | `conn.recv`, `cmd.decode`, `cmd.parse`, `lock.wait_start`, `lock.acquired`, `store.lookup`, `lock.released`, `conn.send`, `cmd.end` |
| A rejected command (empty, wrong number of arguments, unknown) | `conn.recv`, `cmd.decode`, `cmd.parse`, `conn.send`, `cmd.end` |
| `quit` | `conn.recv`, `cmd.decode`, `conn.quit`, `cmd.end`, then `conn.close` |
| Bytes that are not valid UTF-8 | `conn.recv`, `cmd.decode` (`ok: false`), `error`, `cmd.end` (`decode_error`), then `conn.close` |

A connection starts with `conn.accept` (on the accepting thread) and `conn.open` (on its own thread). It ends with `conn.close`, which follows `conn.disconnect` when the client closed the connection or the server shut it down.

<details>
<summary>Full field reference for every event type</summary>

#### Envelope (every event)

| Field | Type | Kind | Unit / clock | Meaning |
|---|---|---|---|---|
| `v` | int | config | — | schema version |
| `seq` | int | id | — | per-server sequence number: 1 for the first event, +1 per event, no gaps at emission (consumers can still miss events, see drops) |
| `ts_ns` | int | measured | ns since the Unix epoch, wall clock | wall-clock time at emission, time.time_ns(); for display only, may jump |
| `mono_ns` | int | measured | ns, monotonic clock | time.monotonic_ns() at emission; never decreases as seq increases |
| `type` | str | actual | — | event type, see events |
| `thread` | str | actual | — | name of the thread that emitted the event |
| `thread_id` | int | actual | — | threading.get_ident() of the emitting thread |
| `conn` | int\|null | id | — | connection id, per server, from 1; null outside a connection |
| `cmd` | int\|null | id | — | command id, per server, from 1; one per non-empty recv(); null outside a command |
| `data` | object | actual | — | type-specific fields, see events |

#### TextField (keys, values, verbs, error messages)

| Field | Type | Kind | Meaning |
|---|---|---|---|
| `text` | str\|null | actual | the text, cut to max_chars; null when redacted |
| `chars` | int | measured | length of the original text in characters |
| `truncated` | bool | actual | true when text was cut to max_chars |
| `redacted` | bool | config | true when the value was hidden by --redact-values |

#### Event types

**`server.start`** (server): Server.start() was called.

| `data` field | Type | Kind | Unit / clock | Meaning |
|---|---|---|---|---|
| `host` | str | config | — | configured listen host |
| `port` | int | config | — | configured port (0 = ephemeral) |
| `store_path` | str | config | — | store file path as configured |
| `pid` | int | actual | — | process id |
| `python` | str | actual | — | Python version |

**`server.listen`** (server): listening socket bound and listening.

| `data` field | Type | Kind | Unit / clock | Meaning |
|---|---|---|---|---|
| `host` | str | config | — | listen host |
| `port` | int | actual | — | port actually bound (from getsockname) |

**`server.stop`** (server): Server.stop() closed the listening socket and the connections that were still open.

| `data` field | Type | Kind | Unit / clock | Meaning |
|---|---|---|---|---|
| `connections_open` | int | actual | — | connections whose handler had still not finished after the shutdown grace period (normally 0) |

**`conn.accept`** (connection): accept() returned a client (main thread).

| `data` field | Type | Kind | Unit / clock | Meaning |
|---|---|---|---|---|
| `peer_host` | str | actual | — | client address |
| `peer_port` | int | actual | — | client port |

**`conn.open`** (connection): handler thread started for the connection.

| `data` field | Type | Kind | Unit / clock | Meaning |
|---|---|---|---|---|
| `peer_host` | str | actual | — | client address |
| `peer_port` | int | actual | — | client port |

**`conn.recv`** (connection): recv(1024) returned data; starts a command.

| `data` field | Type | Kind | Unit / clock | Meaning |
|---|---|---|---|---|
| `bytes` | int | measured | bytes | bytes returned by this recv() |

**`cmd.decode`** (command): outcome of decoding the received bytes as UTF-8.

| `data` field | Type | Kind | Unit / clock | Meaning |
|---|---|---|---|---|
| `ok` | bool | measured | — | decode succeeded |
| `chars` | int\|null | measured | — | characters after decode() and strip() |
| `error_type` | str\|null | actual | — | exception class name, null on success |

**`cmd.parse`** (command): CommandParser validated the message.

| `data` field | Type | Kind | Unit / clock | Meaning |
|---|---|---|---|---|
| `verb` | str | actual | — | SET, GET, REMOVE, PRINT, EMPTY or UNKNOWN |
| `verb_text` | TextField\|null | actual | — | first token exactly as sent |
| `argc` | int | actual | — | number of tokens after the verb |
| `valid` | bool | actual | — | true when the command will be executed |
| `reason` | str\|null | actual | — | why it was rejected: empty, arity or unknown |
| `key` | TextField\|null | actual | — | key of a valid SET/GET/REMOVE |
| `value` | TextField\|null | actual | — | value of a valid SET (redactable) |

**`conn.quit`** (connection): client sent quit; the handler stops.

No `data` fields.

**`lock.wait_start`** (lock): a thread entered acquire() on the lock.

| `data` field | Type | Kind | Unit / clock | Meaning |
|---|---|---|---|---|
| `lock` | str | actual | — | name of the instrumented lock (always "store") |
| `waiting` | int | actual | — | threads inside acquire() on this lock, including this one; a count, not a queue order |
| `owner_thread` | str\|null | actual | — | thread recorded as holder at this instant. Non-null means it really held the lock then; null means free, or held but inside the few-bytecode window at the very start or end of a hold, so a contended acquisition can follow a null owner |
| `owner_thread_id` | int\|null | actual | — | ident of that holder |

**`lock.acquired`** (lock): acquire() obtained the lock.

| `data` field | Type | Kind | Unit / clock | Meaning |
|---|---|---|---|---|
| `lock` | str | actual | — | name of the instrumented lock (always "store") |
| `wait_ns` | int | measured | ns, monotonic clock | time spent inside the underlying acquire |
| `contended` | bool | measured | — | a non-blocking first attempt failed, i.e. another thread held the lock |
| `waiting` | int | actual | — | threads still inside acquire() after this one left |

**`lock.acquire_failed`** (lock): non-blocking or timed acquire() gave up.

| `data` field | Type | Kind | Unit / clock | Meaning |
|---|---|---|---|---|
| `lock` | str | actual | — | name of the instrumented lock (always "store") |
| `wait_ns` | int | measured | ns, monotonic clock | time spent trying |
| `waiting` | int | actual | — | threads still inside acquire() |

**`lock.released`** (lock): outermost release() by the owner, emitted just before the underlying release, so acquired and released events of the lock strictly alternate.

| `data` field | Type | Kind | Unit / clock | Meaning |
|---|---|---|---|---|
| `lock` | str | actual | — | name of the instrumented lock (always "store") |
| `hold_ns` | int | measured | ns, monotonic clock | from acquisition to the start of release, including any event emission inside the critical section |

**`store.load`** (persistence): KeyValueStore.load() finished.

| `data` field | Type | Kind | Unit / clock | Meaning |
|---|---|---|---|---|
| `path` | str | config | — | store file path as configured |
| `exists` | bool | actual | — | the file existed |
| `ok` | bool | actual | — | no exception while loading |
| `keys` | int | actual | — | keys in memory after loading |
| `bytes` | int\|null | measured | bytes | file size from os.stat() before reading |
| `lines_skipped` | int\|null | measured | — | non-empty lines without a tab, which load() skipped; null when the file is missing or loading failed |
| `duration_ns` | int\|null | measured | ns, monotonic clock | time spent loading |
| `error_type` | str\|null | actual | — | exception class name, null on success |
| `error` | TextField\|null | actual | — | str(exception), bounded; null on success |

**`store.lookup`** (store): a read of the store under the lock.

| `data` field | Type | Kind | Unit / clock | Meaning |
|---|---|---|---|---|
| `op` | str | actual | — | get, remove (key missing, nothing changed) or print |
| `key` | TextField\|null | actual | — | key looked up; null for print |
| `found` | bool\|null | actual | — | key was present; null for print |
| `keys` | int | actual | — | keys in the store |

**`store.mutation`** (store): the in-memory dict was changed.

| `data` field | Type | Kind | Unit / clock | Meaning |
|---|---|---|---|---|
| `op` | str | actual | — | set or remove |
| `key` | TextField | actual | — | key changed |
| `value` | TextField\|null | actual | — | new value for set (redactable), null for remove |
| `existed` | bool | actual | — | key was present before the change |
| `keys` | int | actual | — | keys in the store after the change |
| `generation` | int | measured | — | mutation counter after this change (+1 per mutation) |

**`store.save_start`** (persistence): save() starts rewriting the file.

| `data` field | Type | Kind | Unit / clock | Meaning |
|---|---|---|---|---|
| `path` | str | config | — | store file path as configured |
| `generation` | int | measured | — | mutation generation being written |
| `keys` | int | actual | — | keys being written |

**`store.save_end`** (persistence): save() finished.

| `data` field | Type | Kind | Unit / clock | Meaning |
|---|---|---|---|---|
| `path` | str | config | — | store file path as configured |
| `ok` | bool | actual | — | file written and closed without exception |
| `bytes` | int\|null | measured | bytes | exact file size from os.stat() after close |
| `mtime_ns` | int\|null | actual | ns since the Unix epoch, wall clock | file mtime from that os.stat() |
| `duration_ns` | int | measured | ns, monotonic clock | open, write and close |
| `generation` | int | measured | — | mutation generation this save wrote |
| `error_type` | str\|null | actual | — | exception class name, null on success |
| `error` | TextField\|null | actual | — | str(exception), bounded; null on success |

**`conn.send`** (connection): sendall() of the response returned.

| `data` field | Type | Kind | Unit / clock | Meaning |
|---|---|---|---|---|
| `bytes` | int | measured | bytes | length of the encoded response |
| `send_ns` | int | measured | ns, monotonic clock | duration of sendall() |
| `latency_ns` | int | measured | ns, monotonic clock | from recv() returning to sendall() returning |

**`cmd.end`** (command): a command finished (one per conn.recv).

| `data` field | Type | Kind | Unit / clock | Meaning |
|---|---|---|---|---|
| `verb` | str | actual | — | SET, GET, REMOVE, PRINT, QUIT, EMPTY, UNKNOWN, UNDECODED or UNPARSED |
| `outcome` | str | actual | — | ok, not_found, rejected, decode_error or error |
| `duration_ns` | int | measured | ns, monotonic clock | from recv() returning to this event |

**`conn.disconnect`** (connection): recv() returned 0 bytes, or failed because stop() closed the connection.

| `data` field | Type | Kind | Unit / clock | Meaning |
|---|---|---|---|---|
| `by` | str | actual | — | peer (the client closed) or server (stop() shut the connection down) |

**`conn.close`** (connection): handler finished; socket closed.

| `data` field | Type | Kind | Unit / clock | Meaning |
|---|---|---|---|---|
| `reason` | str\|null | actual | — | quit, disconnect, shutdown or error |
| `commands` | int | measured | — | commands received on this connection |
| `bytes_in` | int | measured | bytes | bytes received |
| `bytes_out` | int | measured | bytes | bytes sent |
| `duration_ns` | int | measured | ns, monotonic clock | from conn.open to close |

**`error`** (error): an exception was caught.

| `data` field | Type | Kind | Unit / clock | Meaning |
|---|---|---|---|---|
| `where` | str | actual | — | handler, accept, setup, load or save |
| `step` | str\|null | actual | — | handler step that failed: recv, decode, process or send |
| `error_type` | str | actual | — | exception class name |
| `message` | TextField | actual | — | str(exception), bounded |
</details>

### How far you can trust the numbers

- **Ordering.** `seq` and both timestamps of an event are taken together under one lock. As a result, `mono_ns` never goes down as `seq` goes up, and each thread's events appear in the order they happened. `lock.acquired` is recorded after the lock is obtained and `lock.released` just before it is given up, so for the store lock these two events strictly alternate, matching how the lock really changed hands.
- **Two clocks.** `ts_ns` is wall-clock time, useful for display only: the system clock can be adjusted (NTP, manual changes, sleep), so never subtract two `ts_ns` values. `mono_ns` and every `*_ns` duration use the monotonic clock, which never goes backwards. Monotonic values only mean something within one running process.
- **The GIL affects timings.** Durations are measured by Python code. A thread that is ready to run may have to wait for Python's global interpreter lock first, so measured waits and latencies include time spent waiting for other threads, including the observability threads themselves. Recording events also takes time, and `hold_ns` includes the events recorded while the lock is held.
- **Events are dropped, never waited for.** The server never waits for anyone reading the trace. Every consumer has a bounded queue. When a queue is full, new events for that consumer are dropped and counted (`trace.dropped`). The buffer behind `/events` keeps the newest events and counts the ones it pushes out (`trace.ring_evicted`). Counters in `/diagnostics` are updated as events happen, so drops never affect them. Errors inside the instrumentation, or in your own listener functions, are counted and never reach client handling.
- **"Waiting threads" on the lock.** Python's `RLock` exposes neither its owner nor its waiters, so the store lock is wrapped in a thin `InstrumentedRLock`. The wrapper keeps the RLock behaviour: re-entrant, usable with `with`, the same `acquire()` arguments.
  - `waiting` counts the threads currently inside `acquire()`, including the one that just arrived. It's a count, not a queue: the lock doesn't promise first-come-first-served, and the trace doesn't claim any order.
  - `contended` is a real observation: a first, non-blocking attempt failed because another thread held the lock.
  - A non-null `owner_thread` really held the lock at that instant. A null owner means the lock was free, or was in the brief moment at the very start or end of a hold.
- **How consistent a snapshot is.** `/state` copies the dictionary, reads the counters and checks `store.txt` while holding the store lock, so no command or file write can be half-finished. Everything in one snapshot describes the same moment. Doing this briefly holds the store lock, and that shows up in the trace as lock events from the `inspect-http` thread.
  - `/diagnostics` is not one atomic snapshot: its counters are read together, but the key count and thread counts are read separately.
  - `file_reflects_latest_change` comes from the server's own counters, so it can't see edits made to `store.txt` by other programs. `file_matches_last_save_stat` compares the file's current size and modification time with those recorded right after the last successful save.
- **Exact versus estimated.** Payload sizes and file sizes are exact. `memory_bytes` is an estimate based on `sys.getsizeof` and is labelled that way.
- **Bounded memory.**
  - The event buffer and every queue have fixed sizes.
  - Keys, values and messages in events are cut to `--trace-max-chars`.
  - `/state` returns at most `limit` entries.
  - Error counters keep at most 64 exception names.
- **Isolation.** All observability state belongs to one `Observer` object, and an observer can be attached to only one server. Several servers in one process never mix events or counters.
- **Security.** The endpoint binds to `127.0.0.1` by default and rejects requests whose `Host` header isn't a localhost name, as a protection against DNS rebinding. It has no authentication, encryption or CORS headers. If you bind it to another address, anyone who can reach that address can read every key, and every value unless `--redact-values` is set.
- **Redaction** hides values only. Keys and value lengths stay visible, and the server's normal stdout lines still contain values.

### What observability costs

These measurements come from `bench/loadtest.py`, run on one laptop: an Intel Core i7-1185G7, Windows 11 with WSL2, Python 3.14.4. Each client sent 2,000 commands, and every combination was repeated 3 times. The table shows the median throughput, with the lowest and highest of the three runs in brackets. Other programs were running on the machine at the same time, so treat these as rough indications for this setup, not as general performance figures.

| Clients | Original server | This version, observability off | Event buffer + inspection endpoint | JSONL file + inspection endpoint |
|---:|---:|---:|---:|---:|
| 1 | 8,800 (3,367–10,488) | 8,976 (6,815–9,188) | 8,014 (4,771–8,251) | 2,877 (2,051–3,317) |
| 4 | 5,768 (3,295–6,151) | 5,903 (4,576–6,368) | 4,751 (4,237–5,023) | 2,511 (957–2,517) |
| 16 | 6,214 (4,466–6,854) | 6,110 (5,850–6,776) | 4,010 (2,638–4,115) | 1,774 (1,257–1,921) |

<sub>Commands per second. "Original server" is `server.py` from commit `7ccdff6`, before this work.</sub>

What this shows:

- **Off.** With observability off, this version and the original were within the run-to-run noise of each other. A separate in-process comparison, without the network, measured about 0.1–0.2 microseconds of extra work per command.
- **Event buffer.** The in-memory event buffer that the inspection endpoint reads lowered median throughput by about 11%, 20% and 34% at 1, 4 and 16 clients. A likely reason the cost grows with more clients is that several events are recorded while the store lock is held, which lengthens the time other clients wait for it.
- **JSONL file.** Writing every event to a JSONL file cost the most: 58–71% lower median throughput. The likely reason is that encoding JSON competes with request handling for Python's global interpreter lock. To watch a running server, polling the inspection endpoint is the cheaper option.

## Testing

```bash
python -m unittest discover -s tests -v
```

The suite has 54 tests and uses only the standard library. They cover:

- **Golden protocol tests.** These replay a session recorded from the original version of the server. It includes errors, mixed case, extra whitespace, `quit`, `PRINT` on an empty and a filled store, and bytes that aren't valid UTF-8. The tests require identical replies, stdout lines and `store.txt` contents, with observability both off and on.
- **Tracing.** Every command type produces a complete, correctly ordered sequence of events, and the recorded changes match what ends up in `store.txt`.
- **Concurrency.** 16 clients write at the same time, both to separate keys and to the same key. Every write lands, the file matches memory, and the lock events are consistent.
- **Inspection.** The read-only endpoint, redaction, truncation, bounded buffers, and consumers that stall or crash.
- **Isolation.** Two servers in one process share no observability state.
- **The fixes.** UTF-8 storage regardless of locale, reading older files, reporting malformed lines, the connection limit, graceful shutdown, and the client's handling of empty lines.

On Linux (Python 3.14) all 54 pass. On Windows (Python 3.10), 46 pass and 8 are skipped because they rely on POSIX features (signals, FIFOs, file permissions, the C locale).

## Load testing

`bench/loadtest.py` measures throughput and latency the way `client.py` talks to the server: one send and one 1024-byte receive per command.
- Each simulated client runs in its own process.
- Commands alternate `SET` and `GET` over 100 keys that are filled in before measuring.

```bash
# Load a server that is already running
python bench/loadtest.py run --port 3490 --clients 4

# Start each variant itself and compare: the original server.py from git (commit 7ccdff6),
# this version with observability off, and with it on
python bench/loadtest.py matrix --baseline-ref 7ccdff6 --repeat 3
```

Results depend heavily on the machine and on what else it is doing. The script records the load average for each run. `--max-load` makes it wait for a quiet machine, and `--variants` runs only some variants.

## Project structure

```text
key-value-storage/
├── server.py          # TCP server, command parser, key-value store, command-line options
├── client.py          # interactive TCP client
├── observability.py   # optional tracing, inspection endpoint and diagnostics
├── tests/             # unittest suite (standard library only)
├── bench/loadtest.py  # load generator and before/after comparison
├── store.txt          # created at runtime in the working directory
└── README.md
```

## Known limitations

- **No message framing.** One `recv(1024)` is one command, with the consequences listed under [Protocol and framing](#protocol-and-framing).
- **The save isn't atomic.** `store.txt` is truncated and then rewritten in place. A crash or power loss during a write can leave it incomplete, and another program reading it at that moment can see a partial file. There is no `fsync`.
- **Failed saves are invisible to clients.** If writing `store.txt` fails, the server prints `[ERROR] Error saving data: …`, but the client still receives `Added key …` and the change exists only in memory.
- **A failed load can lose data.** If `store.txt` can't be fully read at startup, the server prints `Error loading data: …` and keeps only what it decoded before the error. The file is decoded in blocks of a few kilobytes, so for a small file that can be nothing. The next change then overwrites the file with what is in memory, so the rest is lost.
- **Single-word keys and values only.**
- **stdout is buffered when redirected and depends on the locale's encoding.** See [Logging](#logging).
- **No authentication or encryption** on either the store port or the inspection endpoint.
- **`client.py` is minimal.** It always connects to `localhost:3490` and reads one 1024-byte chunk per reply.

## Troubleshooting

**The client says "Connection refused".** Start the server first. If you started the server with a different `--port`, remember that `client.py` only connects to `localhost:3490`.

**"Address already in use" when starting the server.** Another program, often a server you already started, is using port 3490. Stop it, or pick another port with `--port`.

**My data disappeared.** The server reads and writes `store.txt` in the directory you start it from (or the path given with `--store`). Starting it from a different directory gives you a different, empty store.

**I redirected the output to a file and it is empty.** stdout is buffered when redirected. Use `python -u server.py > server.log`.

**A large `PRINT` looks cut off, and the rest shows up after the next command.** That's the 1024-byte framing limit described under [Protocol and framing](#protocol-and-framing).

## Possible future improvements

- Newline-delimited (or length-prefixed) messages, so commands and replies of any size work reliably
- Atomic saves (write to a temporary file, then `os.replace`) with `fsync`
- Report failed saves to the client instead of only in the server log
- Values containing spaces, for example quoted strings
- Host and port options for `client.py`
- Authentication and TLS
- Optional CORS or a small proxy, so a browser-based dashboard can read the inspection endpoint

## License

This project is available for educational and personal use. Add a formal license if you intend to distribute or reuse it publicly.
