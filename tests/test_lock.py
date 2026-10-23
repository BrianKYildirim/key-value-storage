"""InstrumentedRLock keeps RLock semantics and reports real waits and holds."""

import threading
import time
import unittest

from support import TIMEOUT, Client, EventLog, RunningServer, observability


class InstrumentedRLockSemanticsTest(unittest.TestCase):
    def setUp(self):
        self.observer = observability.Observer()
        self.log = EventLog(self.observer.subscribe(maxsize=100000))
        self.lock = self.observer.instrument_lock("store")

    def in_thread(self, fn):
        """Run fn on another thread; return its result or re-raise its exception here."""
        outcome = []

        def body():
            try:
                outcome.append((True, fn()))
            except BaseException as e:
                outcome.append((False, e))

        t = threading.Thread(target=body)
        t.start()
        t.join(TIMEOUT)
        self.assertFalse(t.is_alive())
        ok, value = outcome[0]
        if not ok:
            raise value
        return value

    def test_reentrancy_and_context_manager(self):
        with self.lock:
            with self.lock:
                self.assertTrue(self.lock.acquire())
                self.assertTrue(self.lock._is_owned())
                self.lock.release()
            self.assertTrue(self.lock._is_owned())
        self.assertFalse(self.lock._is_owned())
        # Only the outermost acquire/release is reported; nested ones are counted.
        self.assertEqual([e["type"] for e in self.log.pump()], ["lock.wait_start", "lock.acquired", "lock.released"])
        stats = self.observer.diagnostics()["locks"]["store"]
        self.assertEqual((stats["acquisitions"], stats["reentrant"]), (1, 2))

    def test_errors_match_rlock(self):
        plain = threading.RLock()
        for lock in (plain, self.lock):
            with self.assertRaises(RuntimeError):
                lock.release()
            with self.assertRaises(ValueError):
                lock.acquire(False, 1)
            lock.acquire()
            with self.assertRaises(RuntimeError):  # released by a thread that does not own it
                self.in_thread(lock.release)
            lock.release()

    def test_non_blocking_and_timeout_acquire_while_held(self):
        self.lock.acquire()
        try:
            self.assertFalse(self.in_thread(lambda: self.lock.acquire(False)))
            started = time.monotonic()
            self.assertFalse(self.in_thread(lambda: self.lock.acquire(timeout=0.2)))
            self.assertGreaterEqual(time.monotonic() - started, 0.19)
        finally:
            self.lock.release()
        failed = [e["data"] for e in self.log.of_type("lock.acquire_failed")]
        self.assertEqual(len(failed), 2)
        self.assertGreaterEqual(failed[1]["wait_ns"], 190000000)
        self.assertEqual(self.observer.diagnostics()["locks"]["store"]["failed"], 2)

    def test_measured_wait_and_hold_under_real_contention(self):
        holding = threading.Event()

        def holder():
            with self.lock:
                holding.set()
                time.sleep(0.3)

        t = threading.Thread(target=holder, name="holder")
        t.start()
        holding.wait(TIMEOUT)
        with self.lock:
            pass
        t.join(TIMEOUT)
        events = self.log.pump()
        mine = [e for e in events if e["thread"] == threading.current_thread().name]
        wait_start, acquired, released = (e["data"] for e in mine)
        self.assertEqual((wait_start["owner_thread"], wait_start["waiting"]), ("holder", 1))
        self.assertTrue(acquired["contended"])
        self.assertGreater(acquired["wait_ns"], 100000000)
        holder_release = [e for e in events if e["thread"] == "holder" and e["type"] == "lock.released"][0]
        self.assertGreaterEqual(holder_release["data"]["hold_ns"], 290000000)
        # The holder's release happened before our acquisition completed.
        self.assertLess(holder_release["seq"], mine[1]["seq"])

    def test_waiting_counts_threads_inside_acquire(self):
        self.lock.acquire()
        threads = [threading.Thread(target=lambda: (self.lock.acquire(), self.lock.release())) for _ in range(3)]
        for t in threads:
            t.start()
        deadline = time.monotonic() + TIMEOUT
        while self.observer.diagnostics()["locks"]["store"]["waiting_now"] < 3:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)
        diagnostics = self.observer.diagnostics()["locks"]["store"]
        self.assertEqual(diagnostics["owner_thread"], threading.current_thread().name)
        self.lock.release()
        for t in threads:
            t.join(TIMEOUT)
        self.assertEqual(self.observer.diagnostics()["locks"]["store"]["waiting_now"], 0)
        self.assertEqual(sorted(e["data"]["waiting"] for e in self.log.of_type("lock.wait_start"))[-1], 3)


class ServerLockContentionTest(unittest.TestCase):
    def test_a_command_waiting_for_the_store_lock(self):
        with RunningServer(observe=True) as rs:
            lock = rs.server.kv_store.lock
            with Client(rs.port) as c:
                lock.acquire()
                try:
                    c.send(b"GET x")
                    rs.log.wait_for(lambda e: e["type"] == "lock.wait_start" and e["cmd"] is not None)
                    self.assertEqual(rs.observer.diagnostics()["locks"]["store"]["waiting_now"], 1)
                    time.sleep(0.1)
                finally:
                    lock.release()
                self.assertEqual(c.recv(), b"Key 'x' not found.")
                c.send(b"quit")
            acquired = rs.log.wait_for(lambda e: e["type"] == "lock.acquired" and e["cmd"] is not None)
            wait_start = [e for e in rs.log.events if e["type"] == "lock.wait_start" and e["cmd"] is not None][0]
        self.assertEqual(wait_start["data"]["owner_thread"], threading.current_thread().name)
        self.assertTrue(acquired["data"]["contended"])
        self.assertGreaterEqual(acquired["data"]["wait_ns"], 90000000)


if __name__ == "__main__":
    unittest.main()
