"""Inspection endpoint, snapshots, redaction, truncation and bounded buffers."""

import json
import os
import tempfile
import threading
import time
import unittest

from support import (TIMEOUT, Client, RunningServer, event_problems, http_get, http_json, observability)


class InspectedServer(RunningServer):
    """RunningServer plus an InspectionServer on 127.0.0.1:0."""

    def __enter__(self):
        super().__enter__()
        self.inspector = observability.InspectionServer(self.observer)
        self.url = self.inspector.url
        return self

    def __exit__(self, *exc_info):
        self.inspector.close()
        super().__exit__(*exc_info)


def fill(port, commands):
    with Client(port) as c:
        replies = [c.cmd(p) for p in commands]
        c.send(b"quit")
    return replies


class SnapshotTest(unittest.TestCase):
    def test_binds_to_localhost_by_default(self):
        observer = observability.Observer()
        inspector = observability.InspectionServer(observer)
        try:
            self.assertEqual(inspector.address[0], "127.0.0.1")
        finally:
            inspector.close()

    def test_snapshot_equals_get_and_print(self):
        with InspectedServer(observe=True) as rs:
            empty = http_json(rs.url + "state")
            empty_print = fill(rs.port, [b"PRINT"])[0]
            fill(rs.port, [b"SET b 2", b"SET a 1", "SET ключ значение".encode(), b"SET b 3", b"REMOVE a",
                           b"SET z 26"])
            state = http_json(rs.url + "state")
            with Client(rs.port) as c:
                printed = c.cmd(b"PRINT").decode()
                gets = {e["key"]["text"]: c.cmd(b"GET " + e["key"]["text"].encode()).decode()
                        for e in state["store"]["entries"]}
                c.send(b"quit")
        self.assertEqual((empty["store"]["entries"], empty["store"]["keys"]), ([], 0))
        self.assertEqual(empty_print, b"Store is empty.\n")
        entries = [(e["key"]["text"], e["value"]["text"]) for e in state["store"]["entries"]]
        self.assertEqual("".join("[KEY]: %s\t[VALUE]: %s\n" % kv for kv in entries), printed)
        self.assertEqual(gets, dict(entries))
        self.assertEqual(state["store"]["keys"], 3)
        self.assertEqual(state["store"]["payload_bytes"]["value"],
                         sum(len(k.encode()) + len(v.encode()) for k, v in entries))
        self.assertEqual(state["store"]["payload_bytes"]["kind"], "exact")
        self.assertEqual(state["store"]["memory_bytes"]["kind"], "estimate")

    def test_file_view(self):
        with InspectedServer(observe=True) as rs:
            fill(rs.port, [b"SET a 1", b"SET b 2"])
            state = http_json(rs.url + "state")
            st = os.stat(rs.store_path)
            with open(rs.store_path, "ab") as f:  # an external edit the server does not know about
                f.write(b"c\t3\n")
            edited = http_json(rs.url + "state")
        file = state["file"]
        self.assertEqual((file["exists"], file["size_bytes"], file["mtime_ns"]), (True, st.st_size, st.st_mtime_ns))
        self.assertEqual(file["synced_generation"], state["store"]["generation"])
        self.assertEqual(file["last_save"]["bytes"], st.st_size)
        derived = state["derived"]
        self.assertEqual((derived["file_reflects_latest_change"]["value"], derived["unsaved_mutations"]["value"],
                          derived["file_matches_last_save_stat"]["value"]), (True, 0, True))
        self.assertTrue(all("from" in v for v in derived.values()))
        self.assertFalse(edited["derived"]["file_matches_last_save_stat"]["value"])
        self.assertEqual(edited["file"]["size_bytes"], st.st_size + 4)

    def test_no_file_yet(self):
        with InspectedServer(observe=True) as rs:
            state = http_json(rs.url + "state")
        self.assertEqual((state["file"]["exists"], state["file"]["size_bytes"]), (False, None))
        self.assertTrue(state["derived"]["file_reflects_latest_change"]["value"])


class EndpointTest(unittest.TestCase):
    def setUp(self):
        self.rs = InspectedServer(observe=True)
        self.rs.__enter__()
        self.addCleanup(self.rs.__exit__, None, None, None)
        fill(self.rs.port, [b"SET a 1", b"GET a", b"NOPE"])
        self.rs.log.wait_for(lambda e: e["type"] == "conn.close")

    def test_read_only(self):
        before = self.rs.read_store(), os.stat(self.rs.store_path).st_mtime_ns, http_json(self.rs.url + "state")
        seq_before = self.rs.observer.diagnostics()["trace"]["emitted"]
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            status, headers, body = http_get(self.rs.url + "state", method=method)
            self.assertEqual(status, 405, method)
            self.assertEqual(headers["Allow"], "GET, HEAD")
        for path in ("SET%20a%202", "state/SET", "?cmd=SET+a+2", "../store.txt"):
            self.assertIn(http_get(self.rs.url + path)[0], (200, 404))
        self.assertEqual(http_get(self.rs.url + "state?limit=abc")[0], 400)
        after = self.rs.read_store(), os.stat(self.rs.store_path).st_mtime_ns, http_json(self.rs.url + "state")
        self.assertEqual(before[:2], after[:2])
        self.assertEqual(before[2]["store"], after[2]["store"])
        new_events = [e for e in self.rs.log.pump() if e["seq"] > seq_before]
        # Inspection only ever takes the store lock to read; it never mutates, saves or parses.
        self.assertEqual({e["type"] for e in new_events}, {"lock.wait_start", "lock.acquired", "lock.released"})
        self.assertEqual({e["thread"] for e in new_events}, {"inspect-http"})

    def test_valid_json_matching_the_schema(self):
        schema = observability.event_schema()
        for path in ("", "state", "diagnostics", "events", "schema"):
            status, headers, body = http_get(self.rs.url + path)
            self.assertEqual(status, 200, path)
            self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertEqual(int(headers["Content-Length"]), len(body))
            json.loads(body.decode("utf-8"))
        status, headers, body = http_get(self.rs.url + "state", method="HEAD")
        self.assertEqual((status, body), (200, b""))
        self.assertEqual(http_get(self.rs.url + "nope")[0], 404)

        index = http_json(self.rs.url)
        self.assertEqual((index["v"], index["read_only"]), (1, True))
        self.assertEqual(set(index["endpoints"]), {"/state", "/diagnostics", "/events", "/schema"})

        state = http_json(self.rs.url + "state")
        self.assertEqual(set(state), {"v", "taken", "store", "file", "redaction", "derived"})
        self.assertEqual(set(state["store"]), {"keys", "generation", "entries", "entries_returned",
                                               "entries_truncated", "payload_bytes", "memory_bytes"})
        self.assertEqual(set(state["file"]), {"path", "exists", "size_bytes", "mtime_ns", "stat_error",
                                              "synced_generation", "last_save"})
        for entry in state["store"]["entries"]:
            self.assertEqual(set(entry), {"key", "value"})

        diagnostics = http_json(self.rs.url + "diagnostics")
        self.assertEqual(set(diagnostics), {"v", "taken", "server", "connections", "threads", "commands", "bytes",
                                            "locks", "persistence", "store", "errors", "trace", "derived"})
        self.assertEqual(diagnostics["commands"], {"SET": {"ok": 1}, "GET": {"ok": 1}, "UNKNOWN": {"rejected": 1},
                                                   "QUIT": {"ok": 1}})
        self.assertEqual(diagnostics["server"]["bound_port"], self.rs.port)
        self.assertGreaterEqual(diagnostics["derived"]["uptime_ns"]["value"], 0)
        for value in diagnostics["derived"].values():
            self.assertEqual(set(value), {"value", "from"})

        events = http_json(self.rs.url + "events?since=0&limit=1000")
        self.assertEqual(set(events), {"v", "since", "events", "next_since", "oldest_seq", "latest_seq", "derived"})
        for e in events["events"]:
            self.assertEqual(event_problems(e, schema), [], e)
        self.assertEqual(events["next_since"], events["events"][-1]["seq"])
        later = http_json(self.rs.url + "events?since=%d" % events["next_since"])
        self.assertTrue(all(e["seq"] > events["next_since"] for e in later["events"]))
        self.assertEqual(http_json(self.rs.url + "schema"), schema)

    def test_host_header_must_name_the_loopback_endpoint(self):
        port = self.rs.inspector.address[1]
        self.assertEqual(http_get(self.rs.url + "state", headers={"Host": "attacker.example"})[0], 403)
        self.assertEqual(http_get(self.rs.url + "state", headers={"Host": "localhost:%d" % port})[0], 200)


class RedactionAndTruncationTest(unittest.TestCase):
    def test_values_are_redacted_everywhere(self):
        with tempfile.TemporaryDirectory() as d:
            trace_path = os.path.join(d, "trace.jsonl")
            observer = observability.Observer(redact_values=True)
            sink = observability.JsonlSink(observer, trace_path)
            with InspectedServer(observer=observer) as rs:
                replies = fill(rs.port, [b"SET visiblekey s3cr3t-value", b"GET visiblekey"])
                rs.log.wait_for(lambda e: e["type"] == "conn.close")
                bodies = [http_get(rs.url + p)[2] for p in ("state", "events?since=0&limit=4096", "diagnostics")]
                state = http_json(rs.url + "state")
            sink.close()
            with open(trace_path, "rb") as f:
                bodies.append(f.read())
        self.assertEqual(replies, [b"Added key 'visiblekey' with value 's3cr3t-value'\n", b"s3cr3t-value"])
        for body in bodies:
            self.assertNotIn(b"s3cr3t", body)
        self.assertIn(b"visiblekey", bodies[0])
        self.assertEqual(state["store"]["entries"][0]["value"],
                         {"text": None, "chars": 12, "truncated": False, "redacted": True})
        self.assertTrue(state["redaction"]["values_redacted"])
        # The server's own stdout is not part of observability and is unchanged.
        self.assertIn("s3cr3t-value", rs.stdout.getvalue())

    def test_truncation_bounds_every_event(self):
        key, value = "k" * 300, "v" * 600
        with InspectedServer(observe=True, max_chars=8) as rs:
            fill(rs.port, [("SET %s %s" % (key, value)).encode(), b"SET x 1", b"SET y 2", b"BOGUS" * 100])
            rs.log.wait_for(lambda e: e["type"] == "conn.close")
            state = http_json(rs.url + "state")
            limited = http_json(rs.url + "state?limit=2")
            events = rs.log.pump()
        entry = state["store"]["entries"][0]
        self.assertEqual(entry["key"], {"text": "k" * 8, "chars": 300, "truncated": True, "redacted": False})
        self.assertEqual(entry["value"], {"text": "v" * 8, "chars": 600, "truncated": True, "redacted": False})
        self.assertEqual((limited["store"]["entries_returned"], limited["store"]["entries_truncated"],
                          limited["store"]["keys"]), (2, True, 3))
        parse = [e for e in events if e["type"] == "cmd.parse"]
        self.assertEqual(parse[0]["data"]["value"]["text"], "v" * 8)
        self.assertEqual(parse[-1]["data"]["verb_text"]["chars"], 500)
        largest = max(len(json.dumps(e)) for e in events)
        self.assertLess(largest, 800)  # a 1 KB command never produces a large event


class BoundedBuffersTest(unittest.TestCase):
    def test_ring_buffer_keeps_only_the_latest_events(self):
        with InspectedServer(observe=True, buffer_size=20) as rs:
            fill(rs.port, [b"SET a %d" % i for i in range(10)])
            rs.log.wait_for(lambda e: e["type"] == "conn.close")
            events = http_json(rs.url + "events?since=0&limit=20")
            diagnostics = http_json(rs.url + "diagnostics")["trace"]
        self.assertEqual(len(events["events"]), 20)
        self.assertEqual(diagnostics["ring_size"], 20)
        self.assertEqual(events["events"][0]["seq"], events["oldest_seq"])
        self.assertEqual(diagnostics["ring_evicted"], diagnostics["emitted"] - 20)
        self.assertEqual(events["derived"]["evicted_since"]["value"], events["oldest_seq"] - 1)

    def test_full_subscription_drops_and_counts(self):
        with RunningServer(observe=True) as rs:
            stuck = rs.observer.subscribe(maxsize=5)
            start = rs.observer.diagnostics()["trace"]["emitted"]
            fill(rs.port, [b"SET a 1", b"GET a", b"PRINT"])
            rs.log.wait_for(lambda e: e["type"] == "conn.close")
            emitted = rs.observer.diagnostics()["trace"]["emitted"] - start
            received = stuck.drain()
            self.assertEqual(len(received), 5)
            self.assertEqual(stuck.dropped, emitted - 5)
            self.assertEqual(rs.observer.diagnostics()["trace"]["dropped"], emitted - 5)
            stuck.close()
            self.assertEqual(rs.observer.diagnostics()["trace"]["dropped"], emitted - 5)  # kept after close

    @unittest.skipUnless(hasattr(os, "mkfifo"), "needs a FIFO")
    def test_stalled_consumers_never_block_serving(self):
        with tempfile.TemporaryDirectory() as d:
            fifo = os.path.join(d, "stalled.jsonl")
            os.mkfifo(fifo)
            reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)  # exists, but never reads
            release = threading.Event()
            observer = observability.Observer()
            sink = observability.JsonlSink(observer, fifo, maxsize=100)
            stalled = observer.add_listener(lambda event: release.wait(), maxsize=10)
            failing = observer.add_listener(lambda event: 1 / 0, maxsize=10)
            try:
                with RunningServer(observer=observer) as rs:
                    started = time.monotonic()
                    replies = fill(rs.port, [b"SET k%d %d" % (i % 20, i) for i in range(300)])
                    elapsed = time.monotonic() - started
                    rs.log.wait_for(lambda e: e["type"] == "conn.close")
                    diagnostics = observer.diagnostics()["trace"]
            finally:
                release.set()
                os.close(reader)  # the blocked sink write now fails with a broken pipe
                stalled.close()
                failing.close()
                sink.close()
        self.assertEqual(replies, [b"Added key 'k%d' with value '%d'\n" % (i % 20, i) for i in range(300)])
        self.assertLess(elapsed, TIMEOUT)
        self.assertGreater(sink.subscription.dropped, 0)
        self.assertGreater(stalled.subscription.dropped, 0)
        self.assertGreater(diagnostics["dropped"], 0)
        self.assertGreater(diagnostics["listener_errors"], 0)
        self.assertEqual(diagnostics["internal_errors"], 0)


if __name__ == "__main__":
    unittest.main()
