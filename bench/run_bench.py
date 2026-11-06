"""Reproducible load-test runner for key-value-storage: one command per environment.

  python bench/run_bench.py --env windows-native      (native Windows)
  python bench/run_bench.py --env ubuntu-wsl2         (Ubuntu under WSL2; run from a WSL-native clone)

Run it with CPython 3.11 (for example `uv run --no-project --python 3.11 python bench/run_bench.py ...`); the
code needs nothing beyond the standard library. Each run

  1. runs the test suite (python -m unittest discover -s tests -v);
  2. runs bench/loadtest.py matrix: every server variant (the original server.py from --baseline-ref, this
     version with observability off, with the ring buffer and inspection endpoint, and with the JSONL file
     and inspection endpoint) at each client count, interleaved over --repeat repeats, with a fresh server
     on 127.0.0.1 for every run;
  3. captures the example trace (unless --no-example-trace): a fresh server.py --trace-jsonl with an empty
     store gets exactly one `SET username brian` the way client.py sends it, then `quit`.

It writes

  bench/results/<YYYY-MM-DD>-<sha7>[-<tag>]/<env>/
      raw/           tests.txt, matrix.json, matrix.txt, matrix/ (per-command latencies in ns, one file per
                     run), example-trace.jsonl
      meta.json      bench-meta/1: hardware, OS, power, load, Python, workload, timer, validity, raw sha256
      summary.json   bench-summary/1, computed only from raw/ and meta.json

and prints the environment directory as its last line. It refuses a dirty tree unless --allow-dirty
(recorded in meta.json source.dirty) and never overwrites an existing environment directory.

  python bench/run_bench.py --resummarize bench/results/<session>/<env>
      rebuild summary.json from raw/ and meta.json alone

Labels (BENCH-05/06): cmd/s is throughput; per-command p50, p95 and p99 are per_op_latency (every command
is timed on its own, client side, send to reply). The ring and jsonl variants carry build
instrumented:kv-observability-ring and instrumented:kv-observability-jsonl; original and off are
uninstrumented. The example-trace timings are one observation each (n 1) from the JSONL build and carry
build instrumented:kv-observability-jsonl. The matrix and the example trace are marked invalid (kept in
raw/, left out of summary values) when the machine is on battery, when CPU busy at their start stays above
--max-busy-pct after waiting, or when the harness fails; on ubuntu-wsl2 a single matrix run is also invalid
when it starts with the 1-minute load average above --max-load. No individual sample is ever dropped. A
failing test makes the whole result invalid. Standard library only (Python 3.11).
"""

import argparse
import datetime
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.dont_write_bytecode = True  # no bench/__pycache__ in the measured tree
sys.path.insert(0, str(Path(__file__).resolve().parent))
import benchmeta  # noqa: E402
import loadtest  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
PROJECT = "key-value-storage"
REPO_SLUG = "BrianKYildirim/key-value-storage"
ENVIRONMENTS = ("windows-native", "ubuntu-wsl2")
TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
VARIANTS = ("original", "off", "ring", "jsonl")
BUILDS = {"original": "uninstrumented", "off": "uninstrumented", "ring": "instrumented:kv-observability-ring",
          "jsonl": "instrumented:kv-observability-jsonl"}
PERCENTILES = ((50, "p50_us"), (95, "p95_us"), (99, "p99_us"))
TEST_ARGS = ("-m", "unittest", "discover", "-s", "tests", "-v")  # run with the repo as working directory
TRACE_BUILD = BUILDS["jsonl"]
TRACE_COMMAND = b"SET username brian"  # the README walkthrough's command, a synthetic key and value
# example_trace timing key -> (event type, data field, definition)
TRACE_TIMINGS = (
    ("lock_wait_ns", "lock.acquired", "wait_ns", "time inside the store lock's underlying acquire"),
    ("save_duration_ns", "store.save_end", "duration_ns", "time to rewrite store.txt"),
    ("hold_ns", "lock.released", "hold_ns", "store lock held, from acquisition to the start of release"),
    ("reply_latency_ns", "conn.send", "latency_ns", "from recv() returning with the command to sendall() of the "
                                                     "reply returning"),
)
TRACE_BYTES = (("bytes_in", "conn.recv", "bytes"), ("store_bytes", "store.save_end", "bytes"),
               ("reply_bytes", "conn.send", "bytes"))
REDACTION_NOTE = ("raw text: absolute repo, temporary and home paths replaced by <repo>, <tmp> and <home>; "
                  "line endings normalized to LF")
WARMUP_POLICY = ("bench/loadtest.py: the key space is filled before measuring, then every client sends --warmup "
                 "unmeasured commands before a barrier; every run starts a fresh server")
OUTLIER_POLICY = "no samples dropped; whole runs marked invalid on precondition failure"
MEASURES_POLICY = ("throughput: commands per second of one run (all clients' measured commands / wall time from the "
                   "first client's start to the last client's end). per_op_latency: values are the per-run "
                   "percentiles over every measured command of all clients (inclusive quantiles, as loadtest.py "
                   "computes them); pooled is the same percentile over every command of the valid runs. "
                   "derived_ratio: per-repeat cmd/s of a variant / cmd/s of off at the same client count")
LABELS_POLICY = ("original and off are uninstrumented. ring (--inspect-port 0) is instrumented:kv-observability-ring "
                 "and jsonl (--trace-jsonl, --inspect-port 0) is instrumented:kv-observability-jsonl; a figure from "
                 "either must carry that label, and comparing it with off compares an instrumented build with an "
                 "uninstrumented one")


def log(msg):
    print("[run_bench] %s" % msg, file=sys.stderr, flush=True)


def die(msg, code=1):
    log("error: %s" % msg)
    sys.exit(code)


def rel(path, base=REPO):
    """A path shown relative to base with forward slashes (never absolute)."""
    try:
        return Path(os.path.relpath(Path(path).resolve(), Path(base).resolve())).as_posix()
    except ValueError:  # another drive on Windows
        return "<outside-repo>/%s" % Path(path).name


# ------------------------------------------------------------------- raw output: path redaction and LF

def redactor(paths):
    """Replaces absolute paths in captured text (both slash styles and the JSON-escaped double-backslash form),
    longest first across all paths."""
    forms = {}
    for path, token in paths:
        p = str(Path(path).resolve())
        for form in (p, p.replace("\\", "/"), p.replace("/", "\\"), p.replace("/", "\\").replace("\\", "\\\\")):
            if len(form) > 3:
                forms.setdefault(form, token)
    flags = re.IGNORECASE if os.name == "nt" else 0
    subs = [(re.compile(re.escape(f), flags), t) for f, t in sorted(forms.items(), key=lambda kv: -len(kv[0]))]

    def apply(data):
        text = data.replace(b"\r\n", b"\n").decode("utf-8", errors="surrogateescape")
        for rx, token in subs:
            text = rx.sub(token, text)
        return text.encode("utf-8", errors="surrogateescape")
    return apply


def redact_tree(path, redact):
    for f in ([path] if path.is_file() else sorted(path.rglob("*"))):
        if f.is_file():
            f.write_bytes(redact(f.read_bytes()))


# ------------------------------------------------------------------- validity gate

def gate(step, args, env):
    """Precondition sample before a measured step: AC power and CPU busy. Waits up to --quiet-timeout for quiet;
    returns the record kept in meta.json validity.runs."""
    start = time.monotonic()
    while True:
        busy = benchmeta.cpu_busy_pct(1.0)
        power = benchmeta.power_status()
        reasons = []
        if power["ac_line"] is False:
            reasons.append("on battery (AC line offline)")
        if busy is not None and busy > args.max_busy_pct:
            reasons.append("CPU busy %s%% > %s%% at start" % (busy, args.max_busy_pct))
        waited = round(time.monotonic() - start, 1)
        if not reasons or waited >= args.quiet_timeout:
            break
        log("%s: waiting for quiet (%s; waited %s s)" % (step, "; ".join(reasons), waited))
        time.sleep(4)
    if reasons:
        reasons[-1] += " after waiting %s s" % waited
    return {"step": step, "cpu_busy_pct": busy, "ac_line": power["ac_line"], "waited_s": waited,
            "busy_source": "WSL VM /proc/stat (not the Windows host)" if env == "ubuntu-wsl2" else "GetSystemTimes",
            "valid": not reasons, "reasons": reasons}


def child_env():
    """No __pycache__ in the measured tree; UTF-8 child stdio (a Windows pipe defaults to cp1252)."""
    return dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")


def run_step(argv, cwd=REPO):
    """(exit status, combined stdout+stderr bytes); a failure is returned, not raised."""
    p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=cwd, env=child_env(), check=False)
    return p.returncode, p.stdout or b""


# ------------------------------------------------------------------- parsing

def parse_unittest(text):
    """{ passed, failed, skipped, total } from the last `Ran N tests` and `OK (...)` / `FAILED (...)` lines."""
    ran = list(re.finditer(r"^Ran (\d+) tests? in ", text, re.M))
    if not ran:
        return None
    m = re.search(r"^(OK|FAILED)(?: \(([^)]*)\))?\s*$", text[ran[-1].end():], re.M)
    if not m:
        return None
    counts = {k.strip(): int(v) for k, v in re.findall(r"([a-z ]+)=(\d+)", m.group(2) or "")}
    total = int(ran[-1].group(1))
    failed = counts.get("failures", 0) + counts.get("errors", 0) + counts.get("unexpected successes", 0)
    skipped = counts.get("skipped", 0)
    return {"passed": total - failed - skipped, "failed": failed, "skipped": skipped, "total": total}


def skipped_tests(text):
    """[{ test, reason }] for every `name (module.Class.name) ... skipped 'reason'` line of unittest -v."""
    return [{"test": m.group(1), "reason": m.group(2)}
            for m in re.finditer(r"^\S+ \(([^)]+)\)(?:\n.*?)? \.\.\. skipped '(.*)'$", text, re.M)]


def trace_events(text):
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def trace_command(events):
    """Events of the first command in a trace (from its conn.recv to its cmd.end, same conn and cmd)."""
    recv = next((e for e in events if e.get("type") == "conn.recv"), None)
    if recv is None:
        return None, []
    conn, cmd = recv.get("conn"), recv.get("cmd")
    out = []
    for e in events:
        if e["seq"] < recv["seq"] or ("conn" in e and e["conn"] != conn) or ("cmd" in e and e["cmd"] != cmd):
            continue
        out.append(e)
        if e.get("type") == "cmd.end":
            break
    return recv, out


def summarize_trace(text, file, clock=None):
    """example_trace section: the four timings as n 1 observations, the three byte counts as exact integers.
    clock is meta.library.trace_clock; its resolution is attached to every timing."""
    recv, events = trace_command(trace_events(text))
    first = lambda t: next((e for e in events if e.get("type") == t), None)  # noqa: E731
    res = (clock or {}).get("resolution_ns")
    out = {"file": file, "command": TRACE_COMMAND.decode(), "conn": recv and recv.get("conn"),
           "cmd": recv and recv.get("cmd"), "build": TRACE_BUILD, "clock": clock,
           "note": "one command on a fresh server with an empty store; timings are a single observation, not a "
                   "distribution, and come from the JSONL build (instrumented:kv-observability-jsonl). Byte counts "
                   "are exact."}
    for key, etype, field, definition in TRACE_TIMINGS:
        e = first(etype)
        v = (e or {}).get("data", {}).get(field)
        if not isinstance(v, int):
            return None
        extra = {"resolution_ns": res}
        if res and v < res:
            extra["resolution_note"] = ("below one tick of the trace clock (%g ns): start and end fell in the same tick, "
                                        "so this is not a resolved duration (a 0 means under one tick, not zero)" % res)
        out[key] = stats([v], "ns", "request_wall_time", TRACE_BUILD, event=etype, field=field, definition=definition,
                         seq=e["seq"], **extra)
    for key, etype, field in TRACE_BYTES:
        v = ((first(etype) or {}).get("data") or {}).get(field)
        if not isinstance(v, int):
            return None
        out[key] = v
    end = first("cmd.end")
    out["outcome"] = end and end.get("data", {}).get("outcome")
    return out


# ------------------------------------------------------------------- statistics

def quantile(values, pct):
    """Inclusive percentile pct (1..99) of individual command latencies, as bench/loadtest.py computes it."""
    return statistics.quantiles(sorted(values), n=100, method="inclusive")[int(pct) - 1]


def stats(values, unit, measure, build, **extra):
    """Stats object: run-level values in run order; median and inclusive quartiles as the portfolio checker
    recomputes them."""
    vals = list(values)
    if len(vals) == 1:
        med = p25 = p75 = vals[0]
    else:
        med = statistics.median(vals)
        q = statistics.quantiles(vals, n=4, method="inclusive")
        p25, p75 = q[0], q[2]
    return {"n": len(vals), "values": vals, "median": med, "min": min(vals), "max": max(vals), "p25": p25, "p75": p75,
            "unit": unit, "measure": measure, "build": build, **extra}


def matrix_results(doc):
    """{(variant_key, clients): [run results in repeat order]} from loadtest.py's matrix JSON."""
    cells = {}
    for results in doc["runs"].values():
        for r in results:
            cells.setdefault((r["variant_key"], r["clients"]), []).append(r)
    for rs in cells.values():
        rs.sort(key=lambda r: r["rep"])
    return cells


def summarize_matrix(env_dir, doc, invalid, used):
    """(matrix, derived) sections from raw/matrix.json and raw/matrix/*.json, leaving out invalid run files."""
    matrix, by_rep = {}, {}
    for (key, clients), rs in sorted(matrix_results(doc).items(), key=lambda kv: (VARIANTS.index(kv[0][0]), kv[0][1])):
        rs = [r for r in rs if "raw/matrix/%s" % r["raw"] not in invalid]
        if not rs:
            continue
        build, per_run = BUILDS[key], []
        for r in rs:
            path = "raw/matrix/%s" % r["raw"]
            raw = json.loads((env_dir / path).read_text(encoding="utf-8"))
            per_run.append([v for client in raw["latency_ns"] for v in client])
            used.append(path)
        pooled = [v for run in per_run for v in run]
        cell = {"clients": clients, "commands_per_run": [len(run) for run in per_run],
                "raw": ["raw/matrix/%s" % r["raw"] for r in rs],
                "cmd_per_s": stats([r["cmd_per_s"] for r in rs], "cmd/s", "throughput", build,
                                   definition="one run's measured commands / its wall time")}
        for pct, name in PERCENTILES:
            cell[name] = stats([quantile(run, pct) / 1000.0 for run in per_run], "us", "per_op_latency", build,
                               percentile=pct, pooled=quantile(pooled, pct) / 1000.0, samples=len(pooled))
        matrix.setdefault(key, {})["c%d" % clients] = cell
        by_rep[(key, clients)] = {r["rep"]: r["cmd_per_s"] for r in rs}
    derived = {}
    for (key, clients), reps in sorted(by_rep.items(), key=lambda kv: (VARIANTS.index(kv[0][0]), kv[0][1])):
        off = by_rep.get(("off", clients))
        if key == "off" or not off:
            continue
        idx = sorted(set(reps) & set(off))
        if idx:
            derived["%s_vs_off_c%d" % (key, clients)] = stats(
                [reps[i] / off[i] for i in idx], "ratio", "derived_ratio", BUILDS[key], numerator=key, denominator="off",
                clients=clients, repeats=idx,
                definition="cmd/s of %s / cmd/s of off at %d client(s), paired by repeat (the two runs of one repeat "
                           "ran back to back)%s" % (key, clients, "" if BUILDS[key] == "uninstrumented" else
                                                    "; compares an instrumented build with an uninstrumented one"))
    return matrix, derived


def build_summary(env_dir, meta):
    """summary.json from raw/ and meta.json only (also what --resummarize runs)."""
    env_dir = Path(env_dir)
    runs = meta["validity"]["runs"]
    wl = meta["workload"]
    invalid = {r["file"] for r in runs if not r["valid"]}
    used = []
    summary = {"schema": "bench-summary/1", "project": meta["project"], "environment_id": meta["environment_id"],
               "source_sha": meta["source"]["sha"],
               "policy": {"quantile_method": "inclusive", "runs": wl["repeat"],
                          "runs_by_harness": {"matrix_repeats": wl["repeat"], "matrix_clients": wl["clients"]},
                          "warmup": WARMUP_POLICY, "outliers": OUTLIER_POLICY,
                          "values": "run-level: one value per matrix run (one repeat of one variant at one client "
                                    "count, fresh server), in repeat order",
                          "measures": MEASURES_POLICY, "labels": LABELS_POLICY,
                          "invalid_runs_excluded": sorted(invalid)}}
    if (env_dir / "raw/tests.txt").is_file():
        text = (env_dir / "raw/tests.txt").read_text(encoding="utf-8")
        tests = parse_unittest(text)
        if tests:
            summary["tests"] = tests
            summary["tests_skipped"] = skipped_tests(text)
            used.append("raw/tests.txt")
    step = next((r for r in runs if r["step"] == "matrix"), None)
    if step and step["valid"] and (env_dir / "raw/matrix.json").is_file():
        doc = json.loads((env_dir / "raw/matrix.json").read_text(encoding="utf-8"))
        used.append("raw/matrix.json")
        matrix, derived = summarize_matrix(env_dir, doc, invalid, used)
        if matrix:
            summary["matrix"] = matrix
        if derived:
            summary["derived"] = derived
    step = next((r for r in runs if r["step"] == "example_trace"), None)
    if step and step["valid"] and (env_dir / step["file"]).is_file():
        trace = summarize_trace((env_dir / step["file"]).read_text(encoding="utf-8"), step["file"],
                                meta.get("library", {}).get("trace_clock"))
        if trace:
            summary["example_trace"] = trace
            used.append(step["file"])
    summary["generated_from"] = sorted(used)
    return summary


# ------------------------------------------------------------------- run

def parse_args(argv):
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--env", choices=ENVIRONMENTS, help="environment id (required unless --resummarize)")
    p.add_argument("--out", help="results root (default bench/results in this repo; relative paths are from the cwd)")
    p.add_argument("--session-date", default=today, help="YYYY-MM-DD in the session directory name (default: today, UTC)")
    p.add_argument("--tag", help="optional session directory suffix: <date>-<sha7>-<tag>")
    p.add_argument("--allow-dirty", action="store_true", help="run on a tree with uncommitted tracked changes (recorded)")
    p.add_argument("--smoke", action="store_true", help="quick check: 1 repeat, 1 and 4 clients, 200 commands, 20 warm-up")
    p.add_argument("--repeat", type=int, help="interleaved repeats of the whole matrix (default 5; smoke 1)")
    p.add_argument("--clients", type=int, nargs="+", help="client counts (default 1 4 16; smoke 1 4)")
    p.add_argument("--commands", type=int, help="measured commands per client and run (default 2000; smoke 200)")
    p.add_argument("--warmup", type=int, help="unmeasured commands per client and run (default 200; smoke 20)")
    p.add_argument("--baseline-ref", default="7ccdff6", help="commit whose server.py is the original variant (7ccdff6)")
    p.add_argument("--example-trace", action=argparse.BooleanOptionalAction, default=True,
                   help="capture raw/example-trace.jsonl from one SET username brian (default on)")
    p.add_argument("--max-busy-pct", type=float, default=10.0,
                   help="the matrix is invalid when CPU busy at its start stays above this (default 10)")
    p.add_argument("--quiet-timeout", type=float, default=120.0,
                   help="seconds to wait for quiet before marking a step invalid (default 120)")
    p.add_argument("--max-load", type=float,
                   help="ubuntu-wsl2: loadtest.py waits before each run until the 1-minute load average is below "
                        "this, and a run that starts above it is invalid (default: half the logical CPUs)")
    p.add_argument("--resummarize", metavar="ENVDIR", help="rebuild ENVDIR/summary.json from raw/ and meta.json, then exit")
    args = p.parse_args(argv)
    if not args.resummarize and not args.env:
        p.error("--env is required")
    if args.tag is not None and not TAG_RE.match(args.tag):
        p.error("--tag must match %s" % TAG_RE.pattern)
    try:
        datetime.date.fromisoformat(args.session_date)
    except ValueError:
        p.error("--session-date must be YYYY-MM-DD")
    smoke = args.smoke
    args.repeat = args.repeat or (1 if smoke else 5)
    args.clients = args.clients or ([1, 4] if smoke else [1, 4, 16])
    args.commands = args.commands or (200 if smoke else 2000)
    args.warmup = args.warmup if args.warmup is not None else (20 if smoke else 200)
    if args.commands < 2:
        p.error("--commands must be at least 2")
    if args.env == "ubuntu-wsl2" and args.max_load is None:
        args.max_load = (os.cpu_count() or 2) / 2.0
    if args.env != "ubuntu-wsl2":
        args.max_load = None  # loadtest.py's --max-load needs os.getloadavg()
    return args


def shown_command(argv):
    """The documented command with every path argument made repo-relative."""
    out, nxt, path_flags = ["python", "bench/run_bench.py"], False, ("--out", "--resummarize")
    for a in argv:
        if nxt:
            out.append(rel(a) if os.path.isabs(a) or os.sep in a or "/" in a else a)
            nxt = False
        elif a in path_flags:
            out.append(a)
            nxt = True
        elif any(a.startswith(f + "=") for f in path_flags):
            k, v = a.split("=", 1)
            out.append("%s=%s" % (k, rel(v)))
        else:
            out.append(a)
    return out


def check_platform(env):
    if env == "windows-native" and os.name != "nt":
        die("--env windows-native must run on native Windows")
    if env == "ubuntu-wsl2" and not benchmeta.is_wsl():
        die("--env ubuntu-wsl2 must run inside WSL2")


def resummarize(env_dir):
    env_dir = Path(env_dir)
    meta = json.loads((env_dir / "meta.json").read_text(encoding="utf-8"))
    benchmeta.write_json(env_dir / "summary.json", build_summary(env_dir, meta))
    print(env_dir.as_posix())


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    args = parse_args(argv)
    if args.resummarize:
        resummarize(args.resummarize)
        return 0
    env = args.env
    check_platform(env)
    source = benchmeta.git_source(REPO, REPO_SLUG)
    if source["dirty"] and not args.allow_dirty:
        die("tracked files have uncommitted changes (%s); commit them, or pass --allow-dirty to run anyway (the run "
            "is then recorded as dirty)" % ", ".join(source["modified"]))
    session = "%s-%s" % (args.session_date, source["sha"][:7]) + ("-%s" % args.tag if args.tag else "")
    out_root = Path(args.out).resolve() if args.out else REPO / "bench" / "results"
    env_dir = out_root / session / env
    if env_dir.exists():
        die("refusing to overwrite existing results directory %s; use another --tag or --session-date"
            % env_dir.as_posix(), 3)
    baseline = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--verify", args.baseline_ref + "^{commit}"],
                              capture_output=True, text=True)
    if baseline.returncode != 0:
        die("baseline ref %s is not a commit in this repository" % args.baseline_ref)
    (env_dir / "raw").mkdir(parents=True, exist_ok=False)
    measure(args, argv, env, env_dir, source, baseline.stdout.strip())
    print(env_dir.as_posix())
    return 0


def _wait_for_event(path, proc, etype, timeout=15.0):
    """Poll the JSONL trace until an event of type etype is written (the sink flushes after every batch)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if any(e.get("type") == etype for e in trace_events(Path(path).read_text(encoding="utf-8"))):
                return True
        except (OSError, ValueError):  # not created yet, or a line still being written
            pass
        if proc.poll() is not None:
            return False
        time.sleep(0.05)
    return False


def capture_trace(work):
    """Start server.py with --trace-jsonl on 127.0.0.1 in an empty directory, send TRACE_COMMAND the way client.py
    does (one sendall, one recv(1024)), then quit. Readiness comes from the trace's server.listen event, not a
    probe connection, so the command is conn 1 as in the README. Returns (trace bytes, reply bytes, problems)."""
    trace, port, problems = os.path.join(work, "trace.jsonl"), loadtest._free_port(), []
    cmd = [sys.executable, str(REPO / "server.py"), "--host", "127.0.0.1", "--port", str(port), "--store", "store.txt",
           "--trace-jsonl", trace]
    proc = subprocess.Popen(cmd, cwd=work, env=child_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    reply = b""
    try:
        if not _wait_for_event(trace, proc, "server.listen"):
            problems.append("server did not start listening (exit status %s)" % proc.poll())
        else:
            with loadtest.socket.create_connection(("127.0.0.1", port), timeout=30) as sock:
                sock.sendall(TRACE_COMMAND)
                reply = sock.recv(1024)
                sock.sendall(b"quit")
            if not _wait_for_event(trace, proc, "conn.close"):
                problems.append("no conn.close event in the trace after quit")
    finally:
        loadtest._stop(proc)
    data = Path(trace).read_bytes() if os.path.isfile(trace) else b""
    if not data:
        problems.append("the trace file is empty")
    return data, reply, problems


def measure(args, argv, env, env_dir, source, baseline_sha):
    raw = env_dir / "raw"
    redact = redactor([(REPO, "<repo>"), (Path(tempfile.gettempdir()), "<tmp>"), (Path.home(), "<home>")])
    py, linux = sys.executable, env == "ubuntu-wsl2"
    size = ["--repeat", str(args.repeat), "--clients", *map(str, args.clients), "--commands", str(args.commands),
            "--warmup", str(args.warmup)]
    load = ["--max-load", "%g" % args.max_load] if args.max_load is not None else []
    matrix_args = ["matrix", "--baseline-ref", args.baseline_ref, *size, *load]
    workload = {
        "smoke": args.smoke, "repeat": args.repeat, "clients": args.clients, "commands_per_client": args.commands,
        "warmup_per_client": args.warmup, "keys": 100, "mix": "alternating SET/GET (50/50), client.py semantics: "
        "one sendall() and one recv(1024) per command", "variants": list(VARIANTS),
        "variant_args": {"original": "server.Server(host='127.0.0.1', port=<free>).start() from git show "
                                     "<baseline>:server.py", "off": [], "ring": ["--inspect-port", "0"],
                         "jsonl": ["--trace-jsonl", "<run dir>/trace.jsonl", "--inspect-port", "0"]},
        "order": "interleaved: repeat, then variant, then client count; fresh server and store per run",
        "baseline_ref": args.baseline_ref, "baseline_sha": baseline_sha, "max_load": args.max_load,
        "example_trace": args.example_trace,
        "steps": [["python", *TEST_ARGS],
                  ["python", "bench/loadtest.py", *matrix_args, "--json", "raw/matrix.json", "--raw-dir", "raw/matrix"]]
        + ([["python", "server.py", "--host", "127.0.0.1", "--port", "<free>", "--store", "store.txt", "--trace-jsonl",
             "raw/example-trace.jsonl"], "one connection: send %r, recv(1024), send 'quit'" % TRACE_COMMAND.decode()]
           if args.example_trace else []),
        "validity": {"max_busy_pct": args.max_busy_pct, "quiet_timeout_s": args.quiet_timeout,
                     "max_load": args.max_load},
    }
    command = shown_command(argv)
    started = benchmeta.collect(PROJECT, env, command, workload)
    log("collected environment (%s); measuring into %s" % (started["hardware"]["cpu_model"], env_dir.as_posix()))

    log("python %s" % " ".join(TEST_ARGS))
    tests_code, text = run_step([py, *TEST_ARGS])
    (raw / "tests.txt").write_bytes(redact(text))
    tests = parse_unittest(redact(text).decode("utf-8", "replace"))
    log("tests: %s" % tests)

    runs = []
    g = gate("matrix", args, env)
    g["file"] = "raw/matrix.json"
    log("bench/loadtest.py %s" % " ".join(matrix_args))
    code, text = run_step([py, "bench/loadtest.py", *matrix_args, "--json", str(raw / "matrix.json"),
                           "--raw-dir", str(raw / "matrix")])
    (raw / "matrix.txt").write_bytes(redact(text))
    for p in (raw / "matrix.json", raw / "matrix"):
        if p.exists():
            redact_tree(p, redact)
    doc = None
    if code == 0 and (raw / "matrix.json").is_file():
        doc = json.loads((raw / "matrix.json").read_text(encoding="utf-8"))
    else:
        g["valid"], g["reasons"] = False, g["reasons"] + ["loadtest.py matrix exit status %s" % code]
    runs.append(g)
    for (key, clients), rs in sorted(matrix_results(doc).items() if doc else []):
        for r in rs:
            la, reasons = r.get("loadavg_1m_before"), []
            if args.max_load is not None and la is not None and la > args.max_load:
                reasons.append("1-minute load average %s > %s at start" % (la, args.max_load))
            runs.append({"step": "matrix_run", "file": "raw/matrix/%s" % r["raw"], "rep": r["rep"], "variant": key,
                         "clients": clients, "loadavg_1m_before": la, "valid": not reasons, "reasons": reasons})

    reply = None
    if args.example_trace:
        g = gate("example_trace", args, env)
        g["file"] = "raw/example-trace.jsonl"
        log("example trace: %s" % TRACE_COMMAND.decode())
        with tempfile.TemporaryDirectory(prefix="kvs-trace-") as work:
            data, reply, problems = capture_trace(work)
        (raw / "example-trace.jsonl").write_bytes(redact(data))
        if not problems and not summarize_trace(redact(data).decode("utf-8"), g["file"]):
            problems.append("the trace lacks one of the example_trace events")
        g["valid"], g["reasons"] = g["valid"] and not problems, g["reasons"] + problems
        runs.append(g)

    ended = benchmeta.collect(PROJECT, env, command, workload)
    reasons = ["%s: %s" % (r["file"], x) for r in runs for x in r["reasons"]]
    if tests_code != 0 or not tests or tests["failed"]:
        reasons.append("raw/tests.txt: test suite failed (exit status %s; %s)" % (tests_code, tests))
    notes = [REDACTION_NOTE,
             "matrix.txt is loadtest.py's stderr progress lines and markdown table; matrix.json is its --json report "
             "with rep, variant_key and raw added to every run"]
    if linux:
        notes.append("cpu_busy_pct and loadavg describe the WSL VM only, not Windows host activity (RESEARCH Pitfall 4)")
    else:
        notes.append("no load average on Windows: loadtest.py --max-load is not used and runs are not gated one by "
                     "one; the CPU busy gate runs once before the matrix")
    if source["dirty"]:
        notes.append("dirty tree (--allow-dirty): modified %s, untracked %s" % (source["modified"], source["untracked"]))
    if args.smoke:
        notes.append("smoke run: reduced repeats, client counts and commands; not for claims")
    if started["power"]["ac_line"] != ended["power"]["ac_line"]:
        notes.append("AC line changed during the run: %s -> %s" % (started["power"]["ac_line"], ended["power"]["ac_line"]))
    if tests and tests["skipped"]:
        notes.append("%d of %d tests skipped on this platform; raw/tests.txt and summary tests_skipped list each one "
                     "with its reason (D-12)" % (tests["skipped"], tests["total"]))
    if args.example_trace:
        notes.append("example trace: server.py runs in an empty temporary directory with --store store.txt, so the "
                     "trace's path is store.txt as in the README; the server is stopped after its conn.close event"
                     + (" with TerminateProcess, so the trace has no server.stop event" if not linux else
                        " with SIGINT, so the trace ends with server.stop"))
        clock = benchmeta.trace_clock()
        if clock["resolution_ns"] >= 1e6:
            notes.append("trace timings use %s with %g ns resolution here, so sub-tick durations in "
                         "raw/example-trace.jsonl (and in the jsonl and ring variants' events) read as 0; back "
                         "sub-millisecond trace figures with a finer-clock environment" % (clock["name"], clock["resolution_ns"]))

    meta = started
    meta["load"]["loadavg_1m_end"] = ended["load"]["loadavg_1m_start"]
    meta["load"]["cpu_busy_pct_end"] = ended["load"]["cpu_busy_pct_start"]
    meta.update({
        "session_date": args.session_date, "tag": args.tag, "source": source,
        "library": {"third_party_libraries": "none (standard library only)",
                    "server": "server.py on 127.0.0.1, stdout and stderr to the null device",
                    "baseline": {"ref": args.baseline_ref, "sha": baseline_sha},
                    "trace_clock": benchmeta.trace_clock(),
                    "example_trace_reply": reply.decode("utf-8", "replace") if reply is not None else None},
        "instrumentation": ("kv-observability-ring: the ring variant (--inspect-port 0, build "
                            "instrumented:kv-observability-ring); kv-observability-jsonl: the jsonl variant "
                            "(--trace-jsonl, --inspect-port 0, build instrumented:kv-observability-jsonl); "
                            "original and off are uninstrumented"),
        "validity": {"valid": not reasons, "reasons": reasons, "runs": runs, "tests": tests},
        "power_end": ended["power"], "date_utc_end": ended["date_utc"], "notes": notes,
    })
    meta["raw_files"] = benchmeta.raw_file_entries(env_dir)
    benchmeta.write_json(env_dir / "meta.json", meta)
    benchmeta.write_json(env_dir / "summary.json", build_summary(env_dir, meta))
    log("valid" if not reasons else "INVALID: %s" % "; ".join(reasons))


if __name__ == "__main__":
    sys.exit(main())
