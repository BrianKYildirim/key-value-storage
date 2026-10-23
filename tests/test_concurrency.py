"""Many clients at once: every write lands, store and file agree, lock events are well formed."""

import threading
import time
import unittest

from support import TIMEOUT, Client, RunningServer, parse_store_file

CLIENTS = 16
WRITES = 30


def run_clients(port, work):
    """Run work(client_index, Client) on CLIENTS threads that start together; re-raise failures."""
    barrier = threading.Barrier(CLIENTS)
    errors = []

    def body(i):
        try:
            with Client(port) as c:
                barrier.wait(TIMEOUT)
                work(i, c)
                c.send(b"quit")
        except BaseException as e:  # reported by the test thread
            errors.append(e)

    threads = [threading.Thread(target=body, args=(i,)) for i in range(CLIENTS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    if any(t.is_alive() for t in threads):
        raise AssertionError("client threads did not finish: deadlock?")
    if errors:
        raise errors[0]


def wait_for_idle(rs):
    deadline = time.monotonic() + TIMEOUT
    while True:
        d = rs.observer.diagnostics()
        if d["connections"]["current"] == 0 and d["threads"]["live_handlers"] == 0:
            return d
        if time.monotonic() > deadline:
            raise AssertionError("handlers still alive: %r" % d["threads"])
        time.sleep(0.01)


class ConcurrencyTest(unittest.TestCase):
    def assert_lock_events_well_formed(self, events):
        """Per thread: wait_start -> acquired -> released, strictly alternating, with sane numbers."""
        per_thread = {}
        releases = {}
        for e in events:
            if e["type"].startswith("lock."):
                per_thread.setdefault(e["thread_id"], []).append(e)
            if e["type"] == "lock.released":
                releases.setdefault(e["thread_id"], []).append(e["seq"])
        for thread_id, thread_events in per_thread.items():
            types = [e["type"] for e in thread_events]
            self.assertEqual(types, ["lock.wait_start", "lock.acquired", "lock.released"] * (len(types) // 3))
            for wait_start, acquired, released in zip(*[iter(thread_events)] * 3):
                self.assertTrue(wait_start["seq"] < acquired["seq"] < released["seq"])
                self.assertEqual(wait_start["cmd"], acquired["cmd"])
                self.assertGreaterEqual(wait_start["data"]["waiting"], 1)
                self.assertGreaterEqual(acquired["data"]["wait_ns"], 0)
                self.assertGreaterEqual(acquired["data"]["waiting"], 0)
                self.assertGreaterEqual(released["data"]["hold_ns"], 0)
                owner = wait_start["data"]["owner_thread_id"]
                if owner is not None:
                    # A non-null owner really held the lock at that instant: it is another thread,
                    # and its release is reported after this snapshot.
                    self.assertNotEqual(owner, thread_id)
                    self.assertTrue(any(seq > wait_start["seq"] for seq in releases[owner]))
        # Mutual exclusion as seen in the trace: acquired/released events alternate globally.
        holders = [e for e in events if e["type"] in ("lock.acquired", "lock.released")]
        current = None
        for e in holders:
            if e["type"] == "lock.acquired":
                self.assertIsNone(current, "two holders at once")
                current = e["thread_id"]
            else:
                self.assertEqual(current, e["thread_id"])
                current = None

    def test_distinct_keys(self):
        with RunningServer(observe=True) as rs:
            baseline_threads = rs.observer.diagnostics()["threads"]["live_handlers"]

            def work(i, c):
                for j in range(WRITES):
                    self_check = c.cmd(b"SET c%dk%d v%d_%d" % (i, j % 10, i, j))
                    assert self_check == b"Added key 'c%dk%d' with value 'v%d_%d'\n" % (i, j % 10, i, j), self_check

            run_clients(rs.port, work)
            diagnostics = wait_for_idle(rs)
            expected = {"c%dk%d" % (i, k): "v%d_%d" % (i, WRITES - 10 + k) for i in range(CLIENTS) for k in range(10)}
            with Client(rs.port) as c:
                for key, value in expected.items():
                    self.assertEqual(c.cmd(b"GET " + key.encode()), value.encode())
                c.send(b"quit")
            memory = dict(rs.server.kv_store.store)
            on_disk = parse_store_file(rs.store_path)
            events = rs.log.pump()
        self.assertEqual(memory, expected)
        self.assertEqual(on_disk, expected)
        mutations = [e for e in events if e["type"] == "store.mutation"]
        self.assertEqual(len(mutations), CLIENTS * WRITES)
        self.assertEqual([m["data"]["generation"] for m in mutations], list(range(1, CLIENTS * WRITES + 1)))
        self.assertEqual(diagnostics["persistence"]["saves_ok"], CLIENTS * WRITES)
        self.assertEqual(diagnostics["threads"]["live_handlers"], baseline_threads)
        self.assert_lock_events_well_formed(events)
        stats = diagnostics["locks"]["store"]
        self.assertGreater(stats["contended"], 0, "16 concurrent writers never contended")
        self.assertGreater(stats["wait_ns_max"], 0)
        self.assertEqual(diagnostics["trace"]["internal_errors"], 0)

    def test_same_key(self):
        with RunningServer(observe=True) as rs:
            def work(i, c):
                for j in range(WRITES):
                    c.cmd(b"SET shared c%dn%d" % (i, j))

            run_clients(rs.port, work)
            diagnostics = wait_for_idle(rs)
            with Client(rs.port) as c:
                final = c.cmd(b"GET shared").decode()
                c.send(b"quit")
            on_disk = parse_store_file(rs.store_path)
            events = rs.log.pump()
        mutations = [e["data"] for e in events if e["type"] == "store.mutation"]
        self.assertEqual(len(mutations), CLIENTS * WRITES)
        self.assertEqual([m["existed"] for m in mutations], [False] + [True] * (CLIENTS * WRITES - 1))
        # The trace order of mutations is the real order: the last one is what the store holds.
        self.assertEqual(mutations[-1]["value"]["text"], final)
        self.assertEqual(on_disk, {"shared": final})
        # Each client's own writes landed in the order it sent them.
        for i in range(CLIENTS):
            prefix = "c%dn" % i
            own = [int(m["value"]["text"][len(prefix):]) for m in mutations if m["value"]["text"].startswith(prefix)]
            self.assertEqual(own, list(range(WRITES)))
        self.assert_lock_events_well_formed(events)
        self.assertEqual(diagnostics["connections"]["total"], CLIENTS)
        self.assertEqual(diagnostics["commands"]["SET"], {"ok": CLIENTS * WRITES})


if __name__ == "__main__":
    unittest.main()
