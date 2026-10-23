"""Tracing: every command type produces a complete, ordered, correctly labelled event sequence."""

import os
import tempfile
import unittest

from support import Client, RunningServer, event_problems, observability, parse_store_file

SET = ["conn.recv", "cmd.decode", "cmd.parse", "lock.wait_start", "lock.acquired", "store.mutation",
       "store.save_start", "store.save_end", "lock.released", "conn.send", "cmd.end"]
LOOKUP = ["conn.recv", "cmd.decode", "cmd.parse", "lock.wait_start", "lock.acquired", "store.lookup",
          "lock.released", "conn.send", "cmd.end"]
REJECTED = ["conn.recv", "cmd.decode", "cmd.parse", "conn.send", "cmd.end"]
QUIT = ["conn.recv", "cmd.decode", "conn.quit", "cmd.end"]

# payload, expected event types, verb, outcome
COMMANDS = [
    (b"SET k1 v1", SET, "SET", "ok"),
    (b"SET k1 v2", SET, "SET", "ok"),
    (b"GET k1", LOOKUP, "GET", "ok"),
    (b"get nope", LOOKUP, "GET", "not_found"),
    (b"PRINT", LOOKUP, "PRINT", "ok"),
    (b"REMOVE k1", SET, "REMOVE", "ok"),
    (b"REMOVE k1", LOOKUP, "REMOVE", "not_found"),
    (b"SET onlykey", REJECTED, "SET", "rejected"),
    (b"BOGUS x", REJECTED, "UNKNOWN", "rejected"),
    (b"   ", REJECTED, "EMPTY", "rejected"),
]


def durations(event):
    return {k: v for k, v in event["data"].items() if k.endswith("_ns") and k != "mtime_ns" and v is not None}


class EventSequenceTest(unittest.TestCase):
    def setUp(self):
        self.rs = RunningServer(observe=True)
        self.rs.__enter__()
        self.addCleanup(self.rs.__exit__, None, None, None)
        self.log = self.rs.log

    def conn_events(self, conn):
        return [e for e in self.log.pump() if e["conn"] == conn]

    def test_every_command_type_in_order(self):
        responses = []
        with Client(self.rs.port) as c:
            for payload, _, _, _ in COMMANDS:
                responses.append(c.cmd(payload))
            c.send(b"quit")
            self.assertEqual(c.recv(), b"")
        close = self.log.wait_for(lambda e: e["type"] == "conn.close")
        events = self.conn_events(close["conn"])

        # Complete and ordered: accept, open, every command's sequence, close.
        expected = ["conn.accept", "conn.open"] + [t for _, seq, _, _ in COMMANDS for t in seq] + QUIT + ["conn.close"]
        self.assertEqual([e["type"] for e in events], expected)

        # Ordered by seq (contiguous: nothing else was happening) and by monotonic time.
        everything = self.log.events
        self.assertEqual([e["seq"] for e in everything], list(range(1, len(everything) + 1)))
        monos = [e["mono_ns"] for e in everything]
        self.assertEqual(monos, sorted(monos))

        # Ids: one connection; command ids 1..N assigned in order; accept on the start() thread.
        self.assertEqual(events[0]["thread"], "server-main")
        handler = {(e["thread"], e["thread_id"]) for e in events[1:]}
        self.assertEqual(len(handler), 1)
        self.assertNotEqual(events[0]["thread_id"], events[1]["thread_id"])
        for e in (events[0], events[1], events[-1]):
            self.assertIsNone(e["cmd"])
        per_cmd = {}
        for e in events[2:-1]:
            per_cmd.setdefault(e["cmd"], []).append(e)
        self.assertEqual(sorted(per_cmd), list(range(1, len(COMMANDS) + 2)))

        bytes_in = bytes_out = 0
        for cmd_id, (payload, seq, verb, outcome) in zip(sorted(per_cmd), COMMANDS + [(b"quit", QUIT, "QUIT", "ok")]):
            cmd_events = per_cmd[cmd_id]
            self.assertEqual([e["type"] for e in cmd_events], seq, payload)
            by_type = {e["type"]: e for e in cmd_events}
            self.assertEqual(by_type["conn.recv"]["data"]["bytes"], len(payload))
            self.assertEqual(by_type["cmd.decode"]["data"], {"ok": True, "chars": len(payload.decode().strip()),
                                                             "error_type": None})
            end = by_type["cmd.end"]["data"]
            self.assertEqual((end["verb"], end["outcome"]), (verb, outcome), payload)
            bytes_in += len(payload)
            if "conn.send" in by_type:
                response = responses[cmd_id - 1]
                send = by_type["conn.send"]["data"]
                self.assertEqual(send["bytes"], len(response))
                bytes_out += len(response)
                self.assertGreaterEqual(send["latency_ns"], send["send_ns"])
                self.assertEqual(end["duration_ns"], send["latency_ns"])
            for e in cmd_events:
                for name, value in durations(e).items():
                    self.assertGreaterEqual(value, 0, (e["type"], name))
            if "lock.acquired" in by_type:  # a single client never contends
                self.assertEqual(by_type["lock.wait_start"]["data"]["waiting"], 1)
                self.assertIsNone(by_type["lock.wait_start"]["data"]["owner_thread"])
                self.assertFalse(by_type["lock.acquired"]["data"]["contended"])

        parse = [e["data"] for e in events if e["type"] == "cmd.parse"]
        self.assertEqual([p["reason"] for p in parse], [None] * 7 + ["arity", "unknown", "empty"])
        self.assertEqual([p["argc"] for p in parse], [2, 2, 1, 1, 0, 1, 1, 1, 1, 0])
        self.assertEqual(parse[0]["key"]["text"], "k1")
        self.assertEqual(parse[0]["value"]["text"], "v1")
        self.assertEqual(parse[8]["verb_text"]["text"], "BOGUS")

        mutations = [e["data"] for e in events if e["type"] == "store.mutation"]
        self.assertEqual([(m["op"], m["existed"], m["keys"], m["generation"]) for m in mutations],
                         [("set", False, 1, 1), ("set", True, 1, 2), ("remove", True, 0, 3)])
        lookups = [e["data"] for e in events if e["type"] == "store.lookup"]
        self.assertEqual([(l["op"], l["found"]) for l in lookups],
                         [("get", True), ("get", False), ("print", None), ("remove", False)])

        closed = events[-1]["data"]
        self.assertEqual((closed["reason"], closed["commands"], closed["bytes_in"], closed["bytes_out"]),
                         ("quit", len(COMMANDS) + 1, bytes_in, bytes_out))
        diagnostics = self.rs.observer.diagnostics()
        self.assertEqual(diagnostics["bytes"], {"received": bytes_in, "sent": bytes_out})
        self.assertEqual(diagnostics["commands"], {
            "SET": {"ok": 2, "rejected": 1}, "GET": {"ok": 1, "not_found": 1}, "PRINT": {"ok": 1},
            "REMOVE": {"ok": 1, "not_found": 1}, "UNKNOWN": {"rejected": 1}, "EMPTY": {"rejected": 1},
            "QUIT": {"ok": 1}})
        self.assertEqual(diagnostics["trace"]["internal_errors"], 0)

    def test_undecodable_bytes_and_peer_disconnect(self):
        with Client(self.rs.port) as c:
            c.send(b"SET k \xff")
            try:
                self.assertEqual(c.recv(), b"")
            except ConnectionResetError:
                pass
        first = self.log.wait_for(lambda e: e["type"] == "conn.close")
        with Client(self.rs.port):
            pass
        second = self.log.wait_for(lambda e: e["type"] == "conn.close" and e["conn"] != first["conn"])

        events = self.conn_events(first["conn"])
        self.assertEqual([e["type"] for e in events], ["conn.accept", "conn.open", "conn.recv", "cmd.decode",
                                                       "error", "cmd.end", "conn.close"])
        self.assertEqual(events[3]["data"], {"ok": False, "chars": None, "error_type": "UnicodeDecodeError"})
        self.assertEqual((events[4]["data"]["where"], events[4]["data"]["step"], events[4]["data"]["error_type"]),
                         ("handler", "decode", "UnicodeDecodeError"))
        self.assertEqual((events[5]["data"]["verb"], events[5]["data"]["outcome"]), ("UNDECODED", "decode_error"))
        self.assertEqual(events[6]["data"]["reason"], "error")

        events = self.conn_events(second["conn"])
        self.assertEqual([e["type"] for e in events], ["conn.accept", "conn.open", "conn.disconnect", "conn.close"])
        self.assertEqual((events[-1]["data"]["reason"], events[-1]["data"]["commands"]), ("disconnect", 0))
        diagnostics = self.rs.observer.diagnostics()
        self.assertEqual(diagnostics["errors"], {"UnicodeDecodeError": 1})
        self.assertEqual(diagnostics["commands"], {"UNDECODED": {"decode_error": 1}})
        self.assertEqual(diagnostics["connections"], {"current": 0, "total": 2})

    def test_mutation_and_save_events_match_store_txt(self):
        replay = {}
        with Client(self.rs.port) as c:
            for i in range(30):
                c.cmd(b"SET key%d value%d" % (i % 7, i))
                if i % 4 == 3:
                    c.cmd(b"REMOVE key%d" % (i % 5))
            c.send(b"quit")
        self.log.wait_for(lambda e: e["type"] == "conn.close")
        events = self.log.pump()
        generation = 0
        for e in events:
            if e["type"] == "store.mutation":
                d = e["data"]
                generation += 1
                self.assertEqual(d["generation"], generation)
                self.assertEqual(d["existed"], d["key"]["text"] in replay)
                if d["op"] == "set":
                    replay[d["key"]["text"]] = d["value"]["text"]
                else:
                    del replay[d["key"]["text"]]
                self.assertEqual(d["keys"], len(replay))
            elif e["type"] == "store.save_end":
                self.assertTrue(e["data"]["ok"])
                self.assertEqual(e["data"]["generation"], generation)  # each save follows its mutation
        self.assertEqual(replay, parse_store_file(self.rs.store_path))
        last_save = [e for e in events if e["type"] == "store.save_end"][-1]["data"]
        st = os.stat(self.rs.store_path)
        self.assertEqual((last_save["bytes"], last_save["mtime_ns"]), (st.st_size, st.st_mtime_ns))
        self.assertEqual(len([e for e in events if e["type"] == "store.save_start"]), generation)
        state = self.rs.observer.state()
        self.assertEqual(state["file"]["synced_generation"], generation)
        self.assertTrue(state["derived"]["file_reflects_latest_change"]["value"])
        self.assertTrue(state["derived"]["file_matches_last_save_stat"]["value"])

    def test_every_event_matches_the_schema(self):
        with Client(self.rs.port) as c:
            for payload, _, _, _ in COMMANDS:
                c.cmd(payload)
            c.send(b"quit")
        with Client(self.rs.port) as c:
            c.send(b"\xff")
        self.log.wait_for(lambda e: e["type"] == "conn.close" and e["conn"] == 2)
        schema = observability.event_schema()
        seen = set()
        for e in self.log.pump():
            self.assertEqual(event_problems(e, schema), [], e)
            seen.add(e["type"])
        for etype, spec in schema["events"].items():
            for name, field in spec["data"].items():
                self.assertIn(field["kind"], ("actual", "measured", "derived", "id", "config"), (etype, name))
        self.assertGreaterEqual(len(seen), 20)

    def test_readme_documents_every_event_field(self):
        with open(os.path.join(os.path.dirname(observability.__file__), "README.md"), encoding="utf-8") as f:
            readme = f.read()
        schema = observability.event_schema()
        for etype, spec in schema["events"].items():
            self.assertIn("**`%s`**" % etype, readme)
            for name, field in spec["data"].items():
                self.assertIn("| `%s` | %s |" % (name, field["type"].replace("|", "\\|")), readme, (etype, name))


class LoadAndSaveEventsTest(unittest.TestCase):
    def test_load_of_existing_file(self):
        with tempfile.TemporaryDirectory() as d:
            raw = b"a\t1\nmalformed line\nb\t2\n"
            with open(os.path.join(d, "store.txt"), "wb") as f:
                f.write(raw)
            with RunningServer(store_dir=d, observe=True) as rs:
                load = rs.log.of_type("store.load")[0]["data"]
                state = rs.observer.state()
        self.assertEqual((load["exists"], load["ok"], load["keys"], load["bytes"], load["lines_skipped"]),
                         (True, True, 2, len(raw), 1))
        self.assertGreaterEqual(load["duration_ns"], 0)
        self.assertTrue(state["derived"]["file_reflects_latest_change"]["value"])

    def test_failed_load_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "store.txt"), "wb") as f:
                f.write(b"a\t1\nb\t\x81\n")  # 0x81 is invalid in UTF-8 and undefined in cp1252 (the Windows fallback)
            with RunningServer(store_dir=d, observe=True) as rs:
                load = rs.log.of_type("store.load")[0]["data"]
                error = rs.log.of_type("error")[0]["data"]
                state = rs.observer.state()
        self.assertIn("Error loading data:", rs.stdout.getvalue())
        # Like the original server, only what was decoded before the error is kept. The file is decoded in
        # blocks of several kilobytes, so for a file this small that is nothing.
        self.assertEqual((load["ok"], load["error_type"], load["keys"]), (False, "UnicodeDecodeError", 0))
        self.assertEqual((error["where"], error["error_type"]), ("load", "UnicodeDecodeError"))
        self.assertIsNone(state["derived"]["file_reflects_latest_change"]["value"])

    @unittest.skipIf(os.name != "posix" or os.geteuid() == 0, "needs a non-root POSIX user for chmod")
    def test_failed_save_is_reported_while_the_reply_is_unchanged(self):
        with RunningServer(observe=True) as rs:
            with Client(rs.port) as c:
                self.assertEqual(c.cmd(b"SET a 1"), b"Added key 'a' with value '1'\n")
                os.chmod(rs.store_path, 0o444)
                try:
                    self.assertEqual(c.cmd(b"SET b 2"), b"Added key 'b' with value '2'\n")  # original behaviour
                    failed = rs.observer.state()
                finally:
                    os.chmod(rs.store_path, 0o644)
                c.cmd(b"SET c 3")
                recovered = rs.observer.state()
                c.send(b"quit")
            saves = [e["data"] for e in rs.log.of_type("store.save_end")]
            errors = [e["data"] for e in rs.log.of_type("error")]
            diagnostics = rs.observer.diagnostics()
        self.assertEqual([s["ok"] for s in saves], [True, False, True])
        self.assertEqual((saves[1]["error_type"], saves[1]["bytes"]), ("PermissionError", None))
        self.assertEqual([(e["where"], e["error_type"]) for e in errors], [("save", "PermissionError")])
        self.assertFalse(failed["derived"]["file_reflects_latest_change"]["value"])
        self.assertEqual(failed["derived"]["unsaved_mutations"]["value"], 1)
        self.assertTrue(recovered["derived"]["file_reflects_latest_change"]["value"])
        self.assertEqual(diagnostics["persistence"]["saves_failed"], 1)
        self.assertEqual(diagnostics["errors"], {"PermissionError": 1})
        self.assertIn("[ERROR] Error saving data:", rs.stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
