#!/usr/bin/env python3
"""
run_native.py - The only writer of the parity golden files.

Runs every script in tests/parity/scripts through the real engine (Observer, a never-started
Server and Session) on the main thread, in a temporary working directory, with the step
protocol the portfolio's browser demo repeats in Pyodide (see tests/parity/README.md), then
masks the fields that cannot match across runtimes and records one golden per script.

  python tests/parity/run_native.py --write   # write golden/<script>.json and meta.json
  python tests/parity/run_native.py --check   # regenerate in memory, compare bytes

Standard library only. Write on WSL Ubuntu with CPython 3.11; check anywhere.
"""

import argparse
import contextlib
import datetime
import hashlib
import io
import json
import os
import platform
import sys
import tempfile
import threading
from pathlib import Path

PARITY = Path(__file__).resolve().parent
REPO = PARITY.parents[1]
SCRIPTS = PARITY / "scripts"
GOLDEN = PARITY / "golden"
SCHEMA = "kv-parity/1"
# The engine files the portfolio vendors and runs in the browser.
SOURCES = ("observability.py", "server.py")
STORE = "store.txt"  # relative, like the browser's working directory
MAX_SEND = 1024  # server.py reads at most this many bytes per recv()
OPS = ("send", "restart", "reset", "new_session")

# Fields that cannot match between native CPython and Pyodide: time, identity and pointer-size
# values. Masked wherever the key occurs. A non-null value becomes "<x:TYPE>" and null stays null,
# so type and null-ness are still compared. Adding a name here needs a written reason in this list
# and in tests/parity/README.md; an over-broad mask would hide a real difference.
EXCLUDED_KEYS = (
    "duration_ns",  # measured duration (store.load, store.save_end, last_save, last_load)
    "hold_ns",  # measured time the store lock was held
    "hold_ns_max",  # maximum of measured lock hold times
    "hold_ns_mean",  # mean of measured lock hold times
    "hold_ns_total",  # sum of measured lock hold times
    "latency_ns",  # measured duration (connection events, never emitted without a connection)
    "mono_ns",  # monotonic clock reading: a different epoch and resolution per runtime
    "mtime_ns",  # file system modification time: native ns, whole milliseconds in the browser
    "owner_thread_id",  # thread identity: threading.get_ident() is an address or handle per runtime
    "pid",  # process id: Emscripten always reports a fixed value
    "save_ns_max",  # maximum of measured save durations
    "save_ns_total",  # sum of measured save durations
    "send_ns",  # measured duration (connection events, never emitted without a connection)
    "thread_id",  # thread identity, as owner_thread_id
    "ts_ns",  # wall clock reading: whole milliseconds with float artifacts in the browser
    "wait_ns",  # measured time spent waiting for the store lock
    "wait_ns_max",  # maximum of measured lock wait times
    "wait_ns_mean",  # mean of measured lock wait times
    "wait_ns_total",  # sum of measured lock wait times
)
# sys.getsizeof depends on pointer size and Python version ({} is 64 bytes on CPython 3.11 x86_64,
# 40 on Pyodide wasm32). The sibling payload_bytes (UTF-8 lengths) is exact and stays compared.
EXCLUDED_PATHS = ("state.store.memory_bytes.value",)
_isolation: list[tempfile.TemporaryDirectory] = []


def isolate() -> None:
    """Main thread, no bytecode in the repository, an empty temporary working directory. Call before importing the engine."""
    if threading.current_thread() is not threading.main_thread():
        raise SystemExit("run_native: must run on the main thread (events record the thread name)")
    sys.dont_write_bytecode = True
    tmp = tempfile.TemporaryDirectory(prefix="kv-parity-")
    _isolation.append(tmp)
    os.chdir(tmp.name)
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))


def sha256_lf(path: Path) -> str:
    """sha256 of a text file as git stores it (CRLF turned into LF)."""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def dump(obj) -> bytes:
    return (json.dumps(obj, indent=1, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def mask(record):
    """The record with every excluded field replaced by its type, and a tripwire for unlisted clock fields."""

    def walk(value, path):
        if isinstance(value, list):
            return [walk(v, path) for v in value]
        if not isinstance(value, dict):
            return value
        out = {}
        for key, v in value.items():
            here = path + (key,)
            if v is not None and (key in EXCLUDED_KEYS or ".".join(here) in EXCLUDED_PATHS):
                out[key] = "<x:" + type(v).__name__ + ">"
            elif key.endswith("_ns") and isinstance(v, (int, float)) and not isinstance(v, bool):
                raise SystemExit(f"run_native: unmasked time field {'.'.join(here)}; add it to EXCLUDED_KEYS with a reason")
            else:
                out[key] = walk(v, here)
        return out

    return walk(record, ())


def jsonable(obj):
    """What a JSON consumer reads: the engine's inspection payloads, through json.dumps like InspectionServer."""
    return json.loads(json.dumps(obj, allow_nan=False))


class Runner:
    """One script run: the step protocol from tests/parity/README.md."""

    def __init__(self):
        import observability
        import server

        self._observability = observability
        self._server = server
        self.obs = self.srv = self.session = None
        self.since = 0

    def boot(self):
        self.obs = self._observability.Observer()  # defaults: no redaction, max_chars 256
        self.srv = self._server.Server(store_path=STORE, observer=self.obs)  # never started
        self.session = self._server.Session(self.srv.parser)  # no peer, no observer: no connection hooks
        self.since = 0

    def drain(self):
        ev = self.obs.recent_events(self.since, self.obs.buffer_size)
        if ev["derived"]["evicted_since"]["value"]:
            raise SystemExit("run_native: events left the ring buffer before they were drained")
        self.since = ev["latest_seq"]
        return [json.loads(json.dumps(e, separators=(",", ":"))) for e in ev["events"]]

    def store_txt(self):
        try:
            return Path(STORE).read_bytes().decode("utf-8")
        except FileNotFoundError:
            return None

    def snapshot(self):
        events = self.drain()
        state = jsonable(self.obs.state())
        inspect_events = self.drain()  # the lock events state() caused itself
        diagnostics = jsonable(self.obs.diagnostics())  # takes no store lock: emits nothing
        return {
            "events": events,
            "state": state,
            "inspect_events": inspect_events,
            "diagnostics": diagnostics,
            "store_txt": self.store_txt(),
        }

    def send(self, text):
        data = text.encode("utf-8")
        if not 1 <= len(data) <= MAX_SEND:
            raise SystemExit(f"run_native: send text must be 1 to {MAX_SEND} UTF-8 bytes, got {len(data)}")
        replies = []
        keep = self.session.handle(data, replies.append)
        return {"text": text, "keep": keep, "replies": [r.decode("utf-8") for r in replies]}

    def restart(self):
        self.boot()  # a new Observer and Server over the same store.txt

    def reset(self):
        with contextlib.suppress(FileNotFoundError):
            os.remove(STORE)
        self.boot()

    def new_session(self):
        self.session = self._server.Session(self.srv.parser)  # same server, store and observer

    def step(self, op, action):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            fields = action() or {}
            record = self.snapshot()
        record.update(fields)
        record["op"] = op
        record["stdout"] = out.getvalue().splitlines()
        return mask(record)

    def run(self, script):
        steps = [self.step("boot", self.boot)]
        for item in script["steps"]:
            op = item["op"]
            if op == "send":
                steps.append(self.step(op, lambda item=item: self.send(item["text"])))
            else:
                steps.append(self.step(op, getattr(self, op)))
        return {"script": script["name"], "steps": steps}


def load_scripts():
    scripts = {}
    for path in sorted(SCRIPTS.glob("*.json")):
        script = json.loads(path.read_text(encoding="utf-8"))
        name = script.get("name")
        if name != path.stem or name == "meta":
            raise SystemExit(f"run_native: {path.name}: name must equal the file name and not be 'meta'")
        if set(script) != {"name", "steps"} or not isinstance(script["steps"], list) or not script["steps"]:
            raise SystemExit(f"run_native: {path.name}: expected exactly name and a non-empty steps list")
        for item in script["steps"]:
            op = item.get("op") if isinstance(item, dict) else None
            want = {"op", "text"} if op == "send" else {"op"}
            if op not in OPS or set(item) != want or (op == "send" and not isinstance(item["text"], str)):
                raise SystemExit(f"run_native: {path.name}: bad step {item!r}")
        scripts[name] = script
    if not scripts:
        raise SystemExit("run_native: no scripts in tests/parity/scripts")
    return scripts


def meta(scripts):
    return {
        "schema": SCHEMA,
        "environment": "ubuntu-wsl2",
        "python": platform.python_version(),
        "command": "python tests/parity/run_native.py --write",
        "date": datetime.date.today().isoformat(),
        "sources": {name: sha256_lf(REPO / name) for name in SOURCES},
        "scripts": {f"scripts/{name}.json": sha256_lf(SCRIPTS / f"{name}.json") for name in scripts},
        "excluded_keys": sorted(EXCLUDED_KEYS),
        "excluded_paths": list(EXCLUDED_PATHS),
    }


def build():
    """Every script's golden bytes (in script order) and the meta object."""
    scripts = load_scripts()
    runner = Runner()
    base = os.getcwd()
    goldens = {}
    for name, script in scripts.items():
        work = os.path.join(base, name)  # a fresh directory per script, so no store.txt carries over
        os.makedirs(work)
        os.chdir(work)
        try:
            goldens[name] = dump(runner.run(script))
        finally:
            os.chdir(base)
    return goldens, meta(scripts)


def write(goldens, info):
    GOLDEN.mkdir(exist_ok=True)
    keep = {f"{name}.json" for name in goldens} | {"meta.json"}
    for stale in GOLDEN.glob("*.json"):
        if stale.name not in keep:
            stale.unlink()
    for name, data in goldens.items():
        (GOLDEN / f"{name}.json").write_bytes(data)
    (GOLDEN / "meta.json").write_bytes(dump(info))
    print(f"PARITY_GOLDEN_WRITTEN {len(goldens)} scripts")


def check(goldens, info):
    expected = {f"{name}.json" for name in goldens} | {"meta.json"}
    present = {p.name for p in GOLDEN.glob("*.json")}
    if present != expected:
        print(f"PARITY_GOLDEN_DIFF files: missing {sorted(expected - present)}, extra {sorted(present - expected)}")
        return 1
    for name, data in goldens.items():
        # A checkout may turn LF into CRLF; the JSON text itself never holds a raw CR (json escapes it).
        if (GOLDEN / f"{name}.json").read_bytes().replace(b"\r\n", b"\n") != data:
            print(f"PARITY_GOLDEN_DIFF {name}.json differs from the native result")
            return 1
    stored = json.loads((GOLDEN / "meta.json").read_text(encoding="utf-8"))
    for key in ("schema", "sources", "scripts", "excluded_keys", "excluded_paths"):
        if stored.get(key) != info[key]:
            print(f"PARITY_GOLDEN_DIFF meta.json {key}: {stored.get(key)} != {info[key]}")
            return 1
    print(f"PARITY_GOLDEN_OK {len(goldens)} scripts")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="write the golden files")
    mode.add_argument("--check", action="store_true", help="compare with them")
    args = ap.parse_args()
    isolate()
    try:
        goldens, info = build()
        if args.write:
            write(goldens, info)
            return 0
        return check(goldens, info)
    finally:
        os.chdir(PARITY)  # leave the temporary directory so it can be removed on Windows


if __name__ == "__main__":
    sys.exit(main())
