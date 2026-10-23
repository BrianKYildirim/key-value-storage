"""Load generator with client.py semantics: one sendall() and one recv(1024) per command.

  python3 bench/loadtest.py run --port 3490 --clients 4
      load an already running server and print one JSON result

  python3 bench/loadtest.py matrix --baseline-ref main
      start each server variant itself (the original server.py from git, the current one with
      observability off, and with it on), run 1, 4 and 16 clients against each, and print a table

Each client is a separate process, so client-side work does not share a GIL. Commands alternate
SET and GET over a fixed key space that is filled before measuring, so every SET rewrites a file
of the same size. Standard library only.
"""

import argparse
import json
import multiprocessing
import os
import platform
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _worker(host, port, index, commands, warmup, keys, barrier, results):
    sock = socket.create_connection((host, port), timeout=30)

    def command(i):
        key = b"k%d" % ((index * 7919 + i) % keys)
        payload = b"SET " + key + b" v%d" % i if i % 2 == 0 else b"GET " + key
        sock.sendall(payload)
        reply = sock.recv(1024)
        if not reply:
            raise RuntimeError("server closed the connection")

    for i in range(warmup):
        command(i)
    barrier.wait()
    latencies = []
    started = time.monotonic()
    for i in range(commands):
        t = time.perf_counter_ns()
        command(i)
        latencies.append(time.perf_counter_ns() - t)
    finished = time.monotonic()
    sock.sendall(b"quit")
    sock.close()
    results.put((started, finished, latencies))


def run_load(host, port, clients, commands, warmup=200, keys=100):
    """Returns throughput and latency percentiles for one run."""
    with socket.create_connection((host, port), timeout=30) as sock:  # fill the key space
        for k in range(keys):
            sock.sendall(b"SET k%d initial" % k)
            sock.recv(1024)
        sock.sendall(b"quit")
    ctx = multiprocessing.get_context("spawn" if os.name == "nt" else "fork")
    barrier = ctx.Barrier(clients)
    results = ctx.Queue()
    procs = [ctx.Process(target=_worker, args=(host, port, i, commands, warmup, keys, barrier, results))
             for i in range(clients)]
    for p in procs:
        p.start()
    collected = [results.get(timeout=300) for _ in procs]
    for p in procs:
        p.join(30)
        if p.exitcode != 0:
            raise RuntimeError("load worker failed")
    start = min(c[0] for c in collected)
    end = max(c[1] for c in collected)
    latencies = sorted(l for c in collected for l in c[2])
    q = statistics.quantiles(latencies, n=100, method="inclusive")
    return {"clients": clients, "commands": len(latencies), "seconds": end - start,
            "cmd_per_s": len(latencies) / (end - start),
            "p50_us": q[49] / 1000.0, "p95_us": q[94] / 1000.0, "p99_us": q[98] / 1000.0}


# ------------------------------------------------------------------- matrix

def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_listening(port, proc):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("server exited with %s" % proc.returncode)
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("server did not start")


def _start(variant, workdir, port):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    if variant["kind"] == "original":
        code = ("import sys; sys.path.insert(0, %r); import server; server.Server(host='127.0.0.1', port=%d).start()"
                % (variant["dir"], port))
        cmd = [sys.executable, "-c", code]
    else:
        cmd = [sys.executable, os.path.join(REPO, "server.py"), "--host", "127.0.0.1", "--port", str(port),
               "--store", os.path.join(workdir, "store.txt")]
        cmd += [a.replace("{dir}", workdir) for a in variant["args"]]
    proc = subprocess.Popen(cmd, cwd=workdir, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _wait_listening(port, proc)
    return proc


def _stop(proc):
    if os.name == "nt":
        proc.terminate()
    else:
        proc.send_signal(signal.SIGINT)
    try:
        proc.wait(15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _loadavg():
    try:
        return round(os.getloadavg()[0], 2)
    except (AttributeError, OSError):  # not available on Windows
        return None


def _wait_for_quiet(max_load, timeout=1800):
    """With --max-load, wait (up to timeout seconds) until the 1-minute load average drops below it."""
    if max_load is None:
        return
    deadline = time.monotonic() + timeout
    while (_loadavg() or 0) > max_load and time.monotonic() < deadline:
        time.sleep(5)


def environment():
    cpu = platform.processor() or "unknown"
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    cpu = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass
    return {"python": platform.python_implementation() + " " + platform.python_version(),
            "platform": platform.platform(), "cpu": cpu, "logical_cpus": os.cpu_count(),
            "third_party_libraries": "none (standard library only)"}


def matrix(args):
    tmp = tempfile.mkdtemp(prefix="kvs-bench-")
    try:
        _matrix(args, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _matrix(args, tmp):
    variants = []
    if args.baseline_ref and "original" in args.variants:
        source = subprocess.run(["git", "-C", REPO, "show", "%s:server.py" % args.baseline_ref], check=True,
                                capture_output=True).stdout
        os.makedirs(os.path.join(tmp, "original"))
        with open(os.path.join(tmp, "original", "server.py"), "wb") as f:
            f.write(source)
        rev = subprocess.run(["git", "-C", REPO, "rev-parse", "--short", args.baseline_ref], check=True,
                             capture_output=True, text=True).stdout.strip()
        variants.append({"name": "original server.py (%s)" % rev, "kind": "original",
                         "dir": os.path.join(tmp, "original")})
    variants += [v for v in [
        {"key": "off", "name": "new, observability off", "kind": "new", "args": []},
        {"key": "ring", "name": "new, on: ring buffer + inspection endpoint", "kind": "new",
         "args": ["--inspect-port", "0"]},
        {"key": "jsonl", "name": "new, on: JSONL file + inspection endpoint", "kind": "new",
         "args": ["--trace-jsonl", "{dir}/trace.jsonl", "--inspect-port", "0"]},
    ] if v["key"] in args.variants]
    runs = {}
    for rep in range(args.repeat):  # interleaved, so drift affects every variant alike
        for variant in variants:
            for clients in args.clients:
                _wait_for_quiet(args.max_load)
                workdir = tempfile.mkdtemp(dir=tmp)
                port = _free_port()
                proc = _start(variant, workdir, port)
                load_before = _loadavg()
                try:
                    result = run_load("127.0.0.1", port, clients, args.commands, args.warmup, args.keys)
                finally:
                    _stop(proc)
                result["loadavg_1m_before"], result["loadavg_1m_after"] = load_before, _loadavg()
                runs.setdefault((variant["name"], clients), []).append(result)
                print("rep %d  %-45s %2d clients  %8.0f cmd/s  p50 %7.1f us  p95 %7.1f us  load %s -> %s"
                      % (rep + 1, variant["name"], clients, result["cmd_per_s"], result["p50_us"],
                         result["p95_us"], load_before, result["loadavg_1m_after"]), file=sys.stderr, flush=True)
    summary = []
    for variant in variants:
        for clients in args.clients:
            rs = runs[(variant["name"], clients)]
            summary.append({"variant": variant["name"], "clients": clients, "runs": len(rs),
                            "cmd_per_s": statistics.median(r["cmd_per_s"] for r in rs),
                            "cmd_per_s_min": min(r["cmd_per_s"] for r in rs),
                            "cmd_per_s_max": max(r["cmd_per_s"] for r in rs),
                            "p50_us": statistics.median(r["p50_us"] for r in rs),
                            "p95_us": statistics.median(r["p95_us"] for r in rs)})
    report = {"environment": environment(),
              "settings": {"commands_per_client": args.commands, "warmup_per_client": args.warmup,
                           "keys": args.keys, "mix": "alternating SET/GET (50/50)", "repeat": args.repeat,
                           "statistic": "median over repeats", "server_stdout": "/dev/null",
                           "max_load": args.max_load},
              "summary": summary, "runs": {"%s|%d" % k: v for k, v in runs.items()}}
    if args.json:
        with open(args.json, "w") as f:
            json.dump(report, f, indent=1)
    print(markdown(report))


def markdown(report):
    lines = ["| variant | clients | cmd/s (median) | cmd/s (min–max) | p50 (µs) | p95 (µs) | median vs off |",
             "|---|---:|---:|---:|---:|---:|---:|"]
    off = {s["clients"]: s["cmd_per_s"] for s in report["summary"] if s["variant"] == "new, observability off"}
    for s in report["summary"]:
        ratio = "%+.1f%%" % ((s["cmd_per_s"] / off[s["clients"]] - 1) * 100) if s["clients"] in off else ""
        lines.append("| %s | %d | %.0f | %.0f–%.0f | %.1f | %.1f | %s |" % (
            s["variant"], s["clients"], s["cmd_per_s"], s["cmd_per_s_min"], s["cmd_per_s_max"], s["p50_us"],
            s["p95_us"], ratio))
    env = report["environment"]
    lines.append("")
    lines.append("%s on %s, %s (%s logical CPUs); %s" % (env["python"], env["platform"], env["cpu"],
                                                         env["logical_cpus"], env["third_party_libraries"]))
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="mode", required=True)
    run = sub.add_parser("run", help="load a running server")
    run.add_argument("--host", default="127.0.0.1")
    run.add_argument("--port", type=int, default=3490)
    run.add_argument("--clients", type=int, default=1)
    for p in (run, sub.add_parser("matrix", help="start each server variant and compare")):
        p.add_argument("--commands", type=int, default=2000, help="measured commands per client (default 2000)")
        p.add_argument("--warmup", type=int, default=200, help="unmeasured commands per client (default 200)")
        p.add_argument("--keys", type=int, default=100, help="size of the key space (default 100)")
    m = sub.choices["matrix"]
    m.add_argument("--baseline-ref", help="git ref whose server.py is the baseline, e.g. main")
    m.add_argument("--clients", type=int, nargs="+", default=[1, 4, 16])
    m.add_argument("--repeat", type=int, default=3)
    m.add_argument("--json", help="also write the full report to this file")
    m.add_argument("--max-load", type=float,
                   help="before each run, wait until the 1-minute load average is below this (POSIX only)")
    m.add_argument("--variants", nargs="+", default=["original", "off", "ring", "jsonl"],
                   choices=["original", "off", "ring", "jsonl"], help="which server variants to run (default all)")
    args = parser.parse_args()
    if args.mode == "run":
        print(json.dumps(run_load(args.host, args.port, args.clients, args.commands, args.warmup, args.keys)))
    else:
        matrix(args)


if __name__ == "__main__":
    main()
