"""End-to-end regression of `python server.py`: stdout lines, responses and store.txt, via real subprocesses."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest

from support import (GOLDEN_RESPONSES, GOLDEN_STDOUT_SHUTDOWN, GOLDEN_STORE_TXT, SERVER_PY, SUBPROCESS_ENV, TIMEOUT,
                     CliServer, Client, event_problems, expected_stdout_tail, http_json, run_golden_connections,
                     server)

POSIX = os.name == "posix"


@unittest.skipUnless(POSIX, "uses SIGINT and select() on pipes")
class CliStdoutTest(unittest.TestCase):
    def run_cli(self, extra_args):
        with tempfile.TemporaryDirectory() as d:
            cli = CliServer(["--host", "127.0.0.1", "--port", "0"] + extra_args(d), cwd=d)
            try:
                responses, addresses = run_golden_connections(cli.port)
                inspected = http_json(cli.inspect_url + "diagnostics") if cli.inspect_url else None
            finally:
                code, stdout, stderr = cli.interrupt()
            with open(os.path.join(d, "store.txt"), "rb") as f:
                store_txt = f.read()
            trace = None
            if os.path.exists(os.path.join(d, "trace.jsonl")):
                with open(os.path.join(d, "trace.jsonl"), encoding="utf-8") as f:
                    trace = [json.loads(line) for line in f]
        return code, stdout, cli.stderr_lines + [stderr], responses, addresses, store_txt, inspected, trace, cli

    def assert_matches_original(self, result):
        code, stdout, _, responses, addresses, store_txt, _, _, cli = result
        self.assertEqual(code, 0)
        self.assertEqual(responses, GOLDEN_RESPONSES)
        self.assertEqual(store_txt, GOLDEN_STORE_TXT)
        self.assertEqual(stdout, cli.listening_line("127.0.0.1") + expected_stdout_tail(addresses)
                         + GOLDEN_STDOUT_SHUTDOWN)

    def test_observability_off(self):
        result = self.run_cli(lambda d: [])
        self.assert_matches_original(result)
        self.assertRegex("".join(result[2]), r"^Listening on 127\.0\.0\.1:\d+\n$")  # only the port-0 notice

    def test_observability_on_keeps_stdout_identical(self):
        result = self.run_cli(lambda d: ["--trace-jsonl", os.path.join(d, "trace.jsonl"), "--inspect-port", "0"])
        self.assert_matches_original(result)
        inspected, trace = result[6], result[7]
        self.assertEqual(inspected["connections"]["total"], 4)
        # The JSONL trace is complete: contiguous seq from the store load to the server stop.
        self.assertEqual([e["seq"] for e in trace], list(range(1, len(trace) + 1)))
        self.assertEqual(trace[0]["type"], "store.load")
        self.assertEqual(trace[-1]["type"], "server.stop")
        problems = [p for e in trace for p in event_problems(e)]
        self.assertEqual(problems, [])
        self.assertEqual(len([e for e in trace if e["type"] == "conn.close"]), 4)

    def test_both_modes_print_the_same_lines(self):
        off = self.run_cli(lambda d: [])
        on = self.run_cli(lambda d: ["--trace-jsonl", "-", "--inspect-port", "0", "--redact-values"])

        def normalized(result):
            stdout, addresses, cli = result[1], result[4], result[8]
            stdout = stdout.replace(cli.listening_line("127.0.0.1"), "Server listening on 127.0.0.1:<port>\n")
            for i, address in enumerate(addresses, 1):
                stdout = stdout.replace(str(tuple(address)), "<client %d>" % i)
            return stdout.splitlines()

        self.assertEqual(normalized(off), normalized(on))
        # With "-" the trace goes to stderr, never to stdout.
        self.assertTrue(any('"type":"server.stop"' in chunk for chunk in on[2]))
        self.assertNotIn('"type"', on[1])


def port_is_free(port):
    """True when nothing listens on port. Like server.py, ignore TIME_WAIT leftovers of earlier runs."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


@unittest.skipUnless(POSIX, "uses SIGINT")
class NoOptionsTest(unittest.TestCase):
    def test_python_server_py_with_no_options(self):
        if not port_is_free(3490):
            self.skipTest("port 3490 is in use")
        with tempfile.TemporaryDirectory() as d:
            proc = subprocess.Popen([sys.executable, SERVER_PY], cwd=d, env=SUBPROCESS_ENV, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + TIMEOUT
                while True:  # the probe that succeeds becomes the first (empty) connection on stdout
                    try:
                        probe = socket.create_connection(("127.0.0.1", 3490), timeout=1)
                        break
                    except OSError:
                        if time.monotonic() > deadline:
                            raise
                        time.sleep(0.02)
                probe_address = probe.getsockname()
                probe.close()
                time.sleep(0.3)
                with Client(3490) as c:
                    address = c.address
                    self.assertEqual(c.cmd(b"SET k v"), b"Added key 'k' with value 'v'\n")
                    self.assertEqual(c.cmd(b"GET k"), b"v")
                    c.send(b"quit")
                    self.assertEqual(c.recv(), b"")
                time.sleep(0.3)
            finally:
                proc.send_signal(2)
                out, err = proc.communicate(timeout=TIMEOUT)
            with open(os.path.join(d, "store.txt"), "rb") as f:  # store.txt in the working directory
                self.assertEqual(f.read(), b"k\tv\n")
        p, a = str(tuple(probe_address)), str(tuple(address))
        self.assertEqual(out.decode(), "".join([
            "Server listening on 0.0.0.0:3490\n",
            "Connection from %s\n" % p, "Client %s disconnected.\n" % p, "Connection with %s closed.\n" % p,
            "Connection from %s\n" % a,
            "Received from %s: SET k v\n" % a, "Response sent to %s: Added key 'k' with value 'v'\n" % a,
            "Received from %s: GET k\n" % a, "Response sent to %s: v\n" % a,
            "Received from %s: quit\n" % a, "Client %s requested to quit.\n" % a,
            "Connection with %s closed.\n" % a,
            GOLDEN_STDOUT_SHUTDOWN]))
        self.assertEqual(err, b"")
        self.assertEqual(proc.returncode, 0)


class ArgumentDefaultsTest(unittest.TestCase):
    def setUp(self):
        self.saved = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith("KVS_")}
        self.addCleanup(os.environ.update, self.saved)

    def test_defaults_are_the_original_ones(self):
        args = server.parse_args([])
        self.assertEqual((args.host, args.port, args.store, args.max_connections),
                         ("0.0.0.0", 3490, "store.txt", 100))
        self.assertIsNone(args.trace_jsonl)
        self.assertIsNone(args.inspect_port)
        self.assertEqual(args.inspect_host, "127.0.0.1")
        self.assertFalse(args.redact_values)
        with tempfile.TemporaryDirectory() as d:  # Server() reads store.txt from the working directory
            cwd = os.getcwd()
            os.chdir(d)
            try:
                s = server.Server()
            finally:
                os.chdir(cwd)
        self.assertEqual((s.host, s.port, s.kv_store.filepath, s.observer), ("0.0.0.0", 3490, "store.txt", None))
        self.assertIs(type(s.kv_store.lock), type(server.threading.RLock()))  # no wrapper when tracing is off

    def test_environment_then_flags(self):
        os.environ.update({"KVS_PORT": "4000", "KVS_HOST": "127.0.0.1", "KVS_INSPECT_PORT": "0",
                           "KVS_REDACT_VALUES": "1"})
        try:
            args = server.parse_args([])
            self.assertEqual((args.host, args.port, args.inspect_port, args.redact_values),
                             ("127.0.0.1", 4000, 0, True))
            self.assertEqual(server.parse_args(["--port", "5000"]).port, 5000)
        finally:
            for key in ("KVS_PORT", "KVS_HOST", "KVS_INSPECT_PORT", "KVS_REDACT_VALUES"):
                os.environ.pop(key, None)


if __name__ == "__main__":
    unittest.main()
