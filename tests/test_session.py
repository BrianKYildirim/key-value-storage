"""The socket-free per-message step (server.Session) that the browser demo calls. Standard library only."""

import contextlib
import io
import os
import tempfile
import unittest

from support import GOLDEN_SESSION, GOLDEN_STORE_TXT, event_problems, observability, server

# Everything the engine can emit without a connection: the store load at construction, and the command,
# lock and store events of a command. None of them belongs to a connection or a command id.
BROWSER_EVENT_TYPES = {"store.load", "cmd.parse", "lock.wait_start", "lock.acquired", "lock.released",
                       "store.lookup", "store.mutation", "store.save_start", "store.save_end"}


def new_parser(directory):
    return server.CommandParser(server.KeyValueStore(os.path.join(directory, "store.txt")))


class SessionTest(unittest.TestCase):
    def test_golden_session_replays_without_socket_or_output(self):
        with tempfile.TemporaryDirectory() as d:
            session = server.Session(new_parser(d))
            replies = []
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                for payload, _ in GOLDEN_SESSION:
                    self.assertTrue(session.handle(payload, replies.append))
            self.assertEqual(replies, [expected for _, expected in GOLDEN_SESSION])
            self.assertEqual(stdout.getvalue(), "")

    def test_quit_returns_false_and_sends_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            session = server.Session(new_parser(d))
            sent = []
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertFalse(session.handle(b"quit", sent.append))
                self.assertFalse(session.handle(b"  QuIt ", sent.append))
                self.assertTrue(session.handle(b"quit now", sent.append))
            self.assertEqual(sent, [b"ERROR: Unknown command 'quit'\n"])
            self.assertEqual(stdout.getvalue(), "")

    def test_client_address_prints_like_the_native_handler(self):
        with tempfile.TemporaryDirectory() as d:
            session = server.Session(new_parser(d), ("127.0.0.1", 5), None)
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertTrue(session.handle(b"SET a 1", lambda payload: None))
                self.assertFalse(session.handle(b"QUIT", lambda payload: None))
            self.assertEqual(stdout.getvalue(),
                             "Received from ('127.0.0.1', 5): SET a 1\n"
                             "Response sent to ('127.0.0.1', 5): Added key 'a' with value '1'\n"
                             "Received from ('127.0.0.1', 5): QUIT\n"
                             "Client ('127.0.0.1', 5) requested to quit.\n")

    def test_browser_shape_emits_only_connectionless_events(self):
        with tempfile.TemporaryDirectory() as d:
            observer = observability.Observer()
            srv = server.Server(store_path=os.path.join(d, "store.txt"), observer=observer)  # never started
            session = server.Session(srv.parser)
            replies = []
            with contextlib.redirect_stdout(io.StringIO()) as stdout:
                for payload, _ in GOLDEN_SESSION:
                    session.handle(payload, replies.append)
            self.assertEqual(replies, [expected for _, expected in GOLDEN_SESSION])
            self.assertEqual(stdout.getvalue(), "")
            events = observer.recent_events(0, 4096)["events"]
            self.assertTrue(events)
            for event in events:
                self.assertIn(event["type"], BROWSER_EVENT_TYPES)
                self.assertIsNone(event["conn"], event)
                self.assertIsNone(event["cmd"], event)
                self.assertEqual(event["thread"], "MainThread")
                self.assertEqual(event_problems(event), [])
            self.assertEqual({e["type"] for e in events} - {"store.load"}, BROWSER_EVENT_TYPES - {"store.load"})
            diagnostics = observer.diagnostics()
            self.assertEqual(diagnostics["trace"]["internal_errors"], 0)
            self.assertEqual(diagnostics["connections"], {"current": 0, "total": 0})
            self.assertFalse(diagnostics["server"]["running"])
            self.assertIsNone(diagnostics["server"]["bound_port"])
            keys = [entry["key"]["text"] for entry in observer.state()["store"]["entries"]]
            self.assertEqual(keys, ["a", "c", "ключ", "d"])
            observer.close()

    def test_restart_over_the_same_store_file_reloads_it(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "store.txt")
            first = observability.Observer()
            srv = server.Server(store_path=path, observer=first)
            session = server.Session(srv.parser)
            for payload, _ in GOLDEN_SESSION:
                session.handle(payload, lambda reply: None)
            with open(path, "rb") as f:
                self.assertEqual(f.read(), GOLDEN_STORE_TXT)
            second = observability.Observer()  # an Observer binds to one Server only
            server.Server(store_path=path, observer=second)
            loads = [e for e in second.recent_events(0, 4096)["events"] if e["type"] == "store.load"]
            self.assertEqual(len(loads), 1)
            self.assertTrue(loads[0]["data"]["exists"])
            self.assertEqual(loads[0]["data"]["keys"], 4)
            self.assertEqual(second.diagnostics()["trace"]["internal_errors"], 0)
            first.close()
            second.close()


if __name__ == "__main__":
    unittest.main()
