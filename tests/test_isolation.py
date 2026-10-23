"""Two servers in one process never share observability state."""

import collections
import os
import threading
import unittest

from support import Client, RunningServer, observability, server


class IsolationTest(unittest.TestCase):
    def test_two_observed_servers_side_by_side(self):
        with RunningServer(observe=True) as a, RunningServer(observe=True) as b:
            self.assertNotEqual(a.port, b.port)
            self.assertNotEqual(a.store_path, b.store_path)
            with Client(a.port) as ca, Client(b.port) as cb:
                for i in range(5):  # interleaved, so both handler threads are active at once
                    ca.cmd(b"SET a%d %d" % (i, i))
                    cb.cmd(b"GET missing%d" % i)
                ca_port, cb_port = ca.address[1], cb.address[1]
                ca.send(b"quit")
                cb.send(b"quit")
            a.log.wait_for(lambda e: e["type"] == "conn.close")
            b.log.wait_for(lambda e: e["type"] == "conn.close")
            da, db = a.observer.diagnostics(), b.observer.diagnostics()
            sa, sb = a.observer.state(), b.observer.state()
            ea, eb = a.log.pump(), b.log.pump()
            b_file_exists = os.path.exists(b.store_path)
        self.assertEqual({e["data"]["peer_port"] for e in ea if e["type"] == "conn.accept"}, {ca_port})
        self.assertEqual({e["data"]["peer_port"] for e in eb if e["type"] == "conn.accept"}, {cb_port})
        self.assertEqual((da["commands"], db["commands"]), ({"SET": {"ok": 5}, "QUIT": {"ok": 1}},
                                                             {"GET": {"not_found": 5}, "QUIT": {"ok": 1}}))
        self.assertEqual([e["seq"] for e in ea], list(range(1, len(ea) + 1)))
        self.assertEqual([e["seq"] for e in eb], list(range(1, len(eb) + 1)))
        self.assertEqual({e["cmd"] for e in eb if e["cmd"]}, set(range(1, 7)))
        self.assertEqual(sa["store"]["keys"], 5)
        self.assertEqual((sb["store"]["keys"], sb["store"]["generation"]), (0, 0))
        self.assertFalse(b_file_exists)
        self.assertNotIn("store.mutation", {e["type"] for e in eb})
        handler_threads_a = {e["thread_id"] for e in ea if e["conn"] and e["type"] != "conn.accept"}
        handler_threads_b = {e["thread_id"] for e in eb if e["conn"] and e["type"] != "conn.accept"}
        self.assertFalse(handler_threads_a & handler_threads_b)

    def test_unobserved_server_next_to_an_observed_one(self):
        with RunningServer(observe=True) as a, RunningServer() as b:
            before = a.observer.diagnostics()["trace"]["emitted"]
            with Client(b.port) as c:
                c.cmd(b"SET k v")
                c.send(b"quit")
            self.assertEqual(a.observer.diagnostics()["trace"]["emitted"], before)
            self.assertIsNone(b.server.observer)
            self.assertIs(type(b.server.kv_store.lock), type(threading.RLock()))

    def test_an_observer_belongs_to_one_server(self):
        observer = observability.Observer()
        with RunningServer(observer=observer):
            with self.assertRaises(ValueError):
                server.Server(port=0, store_path=os.devnull + "-unused", observer=observer)

    def test_no_module_level_mutable_state(self):
        mutable = (list, dict, set, bytearray, collections.deque, type(threading.local()))
        for module in (observability, server):
            offenders = [name for name, value in vars(module).items()
                         if not name.startswith("__") and isinstance(value, mutable)]
            self.assertEqual(offenders, [], module.__name__)


if __name__ == "__main__":
    unittest.main()
