# Parity set

This directory fixes what the native engine answers, stores and emits for a small set of command scripts, so the portfolio's browser demo can be compared against it exactly. The demo runs `server.py` and `observability.py` unchanged in Pyodide and repeats the step protocol below; any difference between the browser transcript and these goldens (after the masks below) is a bug in the browser build.

Nothing here is a hand-written expectation. `run_native.py` is the only writer of `golden/`.

## Layout

```
tests/parity/
├── README.md        this file
├── run_native.py    writes and checks the goldens (standard library only)
├── scripts/<name>.json   the command scripts (inputs)
└── golden/
    ├── <name>.json  the masked native transcript of scripts/<name>.json
    └── meta.json    what the goldens were made from
```

The portfolio vendors `scripts/` and `golden/` and compares byte for byte by sha256, so the files are LF in git (`.gitattributes`).

## Script format

```json
{"name": "restart", "steps": [
  {"op": "send", "text": "SET a 1"},
  {"op": "restart"},
  {"op": "reset"},
  {"op": "new_session"}
]}
```

- `name` equals the file name without `.json` and is not `meta`.
- `send` carries `text`, which is encoded as UTF-8 and must be 1 to 1024 bytes (the server reads at most that many bytes per `recv()`, and a zero-byte read is a closed connection, never a command). Whitespace-only text is allowed: it is real bytes and the server answers `ERROR: Empty command`.
- `restart` builds a new `Observer` and `Server` over the same `store.txt`, as if the server process had been stopped and started again.
- `reset` deletes `store.txt` first, then does what `restart` does.
- `new_session` builds a new `Session` on the same server, store and observer, as if a client had disconnected and another connected.

## Step protocol

The same sequence runs natively here and in Pyodide in the browser.

1. Boot (implicit, before the first step): a new `observability.Observer()` with its defaults (values not redacted, `max_chars` 256), a new `server.Server(store_path="store.txt", observer=observer)` that is never started, and a new `server.Session(server.parser)` with no client address and no observer. The working directory is empty at the start, and `store.txt` is relative to it. The script runs on the main thread, so every event's `thread` is `MainThread`. `start()`, `setup_server()` and every connection hook are never called, so there is no listener, no connection and no `conn.*` event.
2. Each step does its action (the boot above, or `send`, `restart`, `reset`, `new_session`). A `send` calls `session.handle(data, replies.append)` with `data` as `bytes`; `keep` is its return value and `replies` are the appended payloads decoded as UTF-8.
3. After every step, including the boot, the snapshot is taken in this order:
   1. `events`: `observer.recent_events(since, buffer_size)`, then `since` becomes `latest_seq`. Each event is serialized with `json.dumps(event, separators=(",", ":"))` (the separators `JsonlSink` uses) and read back, so the golden holds what a JSONL consumer reads.
   2. `state`: `observer.state()`, serialized with `json.dumps` (as the inspection endpoint does).
   3. `inspect_events`: the events drained again. `state()` takes the store lock, so it emits `lock.wait_start`, `lock.acquired` and `lock.released` itself; they are kept apart from the command's own events.
   4. `diagnostics`: `observer.diagnostics()`. It takes no store lock, so it emits nothing.
   5. `store_txt`: the text of `store.txt` read as UTF-8 with no newline translation, or `null` when the file does not exist.
4. Standard output is captured for the whole step, snapshot included, and recorded as `stdout`, a list of lines. Without a client address the session prints nothing; the store prints only when it skips malformed lines or fails to load or save.
5. `seq` restarts at one after a `restart` or `reset` because the Observer is new. Events drained after the boot of a restart are the load's events (with their lock events when the file exists).

## Golden format

`golden/<name>.json` is `{"script": "<name>", "steps": [<boot record>, <one record per step>]}`. Every record has `op` (`boot`, `send`, `restart`, `reset` or `new_session`), `events`, `state`, `inspect_events`, `diagnostics`, `store_txt` and `stdout`. A `send` record adds `text`, `keep` and `replies` (empty after `quit`). Written with `json.dumps(obj, indent=1, sort_keys=True, ensure_ascii=False) + "\n"` as UTF-8.

`golden/meta.json` records `schema` (`kv-parity/1`), `environment`, `python`, `command`, `date`, `sources` (sha256 of `observability.py` and `server.py` with LF line endings), `scripts` (sha256 of each script with LF line endings), `excluded_keys` and `excluded_paths`. `--check` fails when a script or a source changed after the goldens were written.

## What is masked, and why

A masked field whose value is not `null` is replaced by `"<x:TYPE>"`, where `TYPE` is `int`, `float`, `str`, `bool`, `list` or `dict`. A `null` stays `null`. So the type and the null-ness of every masked field are still compared; only the value is not. Everything else is compared exactly: `seq`, `thread`, `conn`, `cmd`, every other `data` key, counters, sizes, `payload_bytes`, file sizes and generations.

Excluded keys (masked wherever they occur):

| Key | Why it cannot match |
|---|---|
| `duration_ns` | measured duration (store load and save, last save and load) |
| `hold_ns` | measured time the store lock was held |
| `hold_ns_max` | maximum of measured hold times |
| `hold_ns_mean` | mean of measured hold times |
| `hold_ns_total` | sum of measured hold times |
| `latency_ns` | measured duration; only connection events carry it, so it is never emitted here |
| `mono_ns` | monotonic clock reading: a different epoch and resolution per runtime |
| `mtime_ns` | file system modification time: nanoseconds natively, whole milliseconds in the browser's file system |
| `owner_thread_id` | thread identity: `threading.get_ident()` is an address or handle that differs per runtime |
| `pid` | process id: Emscripten always reports a fixed value |
| `save_ns_max` | maximum of measured save durations |
| `save_ns_total` | sum of measured save durations |
| `send_ns` | measured duration; only connection events carry it, so it is never emitted here |
| `thread_id` | thread identity, as `owner_thread_id` |
| `ts_ns` | wall clock reading: whole milliseconds with float artifacts in the browser |
| `wait_ns` | measured time spent waiting for the store lock |
| `wait_ns_max` | maximum of measured wait times |
| `wait_ns_mean` | mean of measured wait times |
| `wait_ns_total` | sum of measured wait times |

Excluded path:

| Path | Why it cannot match |
|---|---|
| `state.store.memory_bytes.value` | `sys.getsizeof` depends on the pointer size and the Python version (an empty dict is a different size on CPython x86_64 than on Pyodide's 32-bit WebAssembly). The exact `payload_bytes` next to it is compared. |

`run_native.py` also refuses to write a golden that still holds a number under any other key ending in `_ns`, so a new clock field fails loudly instead of leaking a raw time into the goldens. Adding an exclusion needs a written reason in `run_native.py` and in this table.

## Commands

```bash
# Write the goldens (WSL Ubuntu, CPython 3.11, from the repository root)
python tests/parity/run_native.py --write

# Recompare, on any platform with CPython 3.11
python tests/parity/run_native.py --check      # PARITY_GOLDEN_OK, or PARITY_GOLDEN_DIFF with the first difference
```

`--write` prints `PARITY_GOLDEN_WRITTEN <n> scripts`; `--check` prints `PARITY_GOLDEN_OK <n> scripts`. Neither touches the repository except `golden/` on `--write`; each script runs in a fresh temporary directory.

## How the portfolio uses it

The portfolio vendors `scripts/` and `golden/`, repeats the step protocol above in Pyodide with the vendored `server.py` and `observability.py`, applies the same masks, and compares the results with these goldens exactly. Its tests also change one non-excluded field and require the comparison to fail, so the masks cannot silently hide a real difference.
