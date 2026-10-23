"""Golden protocol tests: responses, stdout and store.txt must match the original server byte for byte."""

import contextlib
import os
import tempfile
import threading
import unittest

from support import (GOLDEN_RESPONSES, GOLDEN_RESTART_PRINT, GOLDEN_STORE_TXT, Client, RunningServer,
                     expected_stdout_tail, http_get, observability, run_golden_connections)


@contextlib.contextmanager
def fully_observed_server(store_dir, poll_statuses):
    """Observer with a JSONL sink, a listener and the inspection endpoint, polled during the session."""
    observer = observability.Observer()
    sink = observability.JsonlSink(observer, os.path.join(store_dir, "trace.jsonl"))
    listener = observer.add_listener(lambda event: None)
    with RunningServer(store_dir=store_dir, observer=observer) as rs:
        inspector = observability.InspectionServer(observer)
        stop = threading.Event()

        def poll():
            while not stop.is_set():
                for path in ("state", "diagnostics", "events?since=0"):
                    poll_statuses.append(http_get(inspector.url + path)[0])

        poller = threading.Thread(target=poll, daemon=True)
        poller.start()
        try:
            yield rs
        finally:
            stop.set()
            poller.join(10)
            inspector.close()
    listener.close()
    sink.close()
    observer.close()


class GoldenProtocolTest(unittest.TestCase):
    def run_golden(self, make_server):
        with tempfile.TemporaryDirectory() as d:
            with make_server(d) as rs:
                responses, addresses = run_golden_connections(rs.port)
                store_txt = rs.read_store()
            stdout = rs.stdout.getvalue()
            with make_server(d) as rs2:  # a restarted server must reload the same data
                with Client(rs2.port) as c:
                    restart_print = c.cmd(b"PRINT")
                    c.send(b"quit")
        return rs, responses, addresses, store_txt, stdout, restart_print

    def assert_golden(self, result):
        rs, responses, addresses, store_txt, stdout, restart_print = result
        self.assertEqual(len(responses), len(GOLDEN_RESPONSES))
        for i, (got, expected) in enumerate(zip(responses, GOLDEN_RESPONSES)):
            self.assertEqual(got, expected, "response %d" % i)
        self.assertEqual(store_txt, GOLDEN_STORE_TXT)
        self.assertEqual(restart_print, GOLDEN_RESTART_PRINT)
        self.assertEqual(stdout, "Server listening on 127.0.0.1:%d\n" % rs.port + expected_stdout_tail(addresses)
                         + "Server socket closed.\n")

    def test_observability_off(self):
        self.assert_golden(self.run_golden(lambda d: RunningServer(store_dir=d)))

    def test_observability_on_with_every_channel_active(self):
        statuses = []
        result = self.run_golden(lambda d: fully_observed_server(d, statuses))
        self.assert_golden(result)
        self.assertTrue(statuses)
        self.assertEqual(set(statuses), {200})
        self.assertEqual(result[0].observer.diagnostics()["trace"]["internal_errors"], 0)

    def test_observability_on_with_redaction_and_tiny_limits(self):
        self.assert_golden(self.run_golden(
            lambda d: RunningServer(store_dir=d, observe=True, redact_values=True, max_chars=1, buffer_size=1)))

    def test_hook_failures_never_change_responses(self):
        class FailingObserver(observability.Observer):
            """Every 3rd event fails inside the hooks, and even-numbered connections lose their context."""

            def _append(self, etype, data, conn=None, cmd=None):
                if self._seq % 3 == 0:
                    self._seq += 1
                    raise RuntimeError("injected failure")
                super()._append(etype, data, conn, cmd)

            @observability._contained
            def connection_opened(self, conn_id, address):
                if conn_id is not None and conn_id % 2 == 0:
                    raise RuntimeError("injected failure")
                return observability.Observer.connection_opened(self, conn_id, address)

        result = self.run_golden(lambda d: RunningServer(store_dir=d, observer=FailingObserver()))
        self.assert_golden(result)
        self.assertGreater(result[0].observer.diagnostics()["trace"]["internal_errors"], 0)


class ShutdownTest(unittest.TestCase):
    def test_shutdown_from_another_thread_makes_start_return(self):
        with RunningServer(observe=True) as rs:
            pass
        self.assertFalse(rs.thread.is_alive())
        self.assertEqual(rs.stdout.getvalue(), "Server listening on 127.0.0.1:%d\nServer socket closed.\n" % rs.port)
        self.assertEqual([e["type"] for e in rs.log.events][-1], "server.stop")
        self.assertEqual(len(rs.log.of_type("server.stop")), 1)
        self.assertFalse(rs.server.ready.is_set())


if __name__ == "__main__":
    unittest.main()
