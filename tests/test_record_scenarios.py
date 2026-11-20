"""The capture script's assertions (bench/record_scenarios.py) as pure functions over synthetic events.

No server, socket or thread is started here: a trace without the target behavior must be rejected, and one
with it accepted, so the script can never write a recording that lacks what it claims to show.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "bench"))
import record_scenarios as rec  # noqa: E402


def trace(*specs):
    """Events with seq 1..N from (type, data, extras) triples; extras may set thread and conn."""
    out = []
    for i, (etype, data, extra) in enumerate(specs, start=1):
        out.append({"v": 1, "seq": i, "type": etype, "thread": extra.get("thread", "MainThread"),
                    "conn": extra.get("conn"), "cmd": None, "data": data})
    return out


def ev(etype, data=None, **extra):
    return (etype, data or {}, extra)


def client(n):
    return "client 127.0.0.1:%d" % (40000 + n)


def contended_trace(threads=4, wait_ns=5, contended=True):
    specs = [ev("conn.open", conn=i, thread=client(i)) for i in range(1, threads + 1)]
    specs.append(ev("lock.acquired", {"lock": "store", "wait_ns": wait_ns, "contended": contended}, conn=1))
    return trace(*specs)


class SeqTest(unittest.TestCase):
    def test_accepts_one_to_n_and_rejects_gaps_repeats_wrong_starts_and_empty(self):
        ok = trace(ev("server.start"), ev("server.listen"), ev("server.stop"))
        self.assertEqual(rec.check_seq(ok), [])
        gap = [dict(e) for e in ok]
        gap[2]["seq"] = 4
        repeat = [dict(e) for e in ok]
        repeat[2]["seq"] = 2
        late = [dict(e, seq=e["seq"] + 1) for e in ok]
        for bad in (gap, repeat, late, []):
            self.assertNotEqual(rec.check_seq(bad), [], bad)

    def test_a_trace_must_end_with_server_stop(self):
        self.assertEqual(rec.check_stop(trace(ev("server.listen"), ev("server.stop"))), [])
        self.assertNotEqual(rec.check_stop(trace(ev("server.stop"), ev("server.listen"))), [])
        self.assertNotEqual(rec.check_stop([]), [])


class ContentionTest(unittest.TestCase):
    def test_accepts_a_contended_acquisition_and_four_client_threads(self):
        self.assertEqual(rec.check_contention(contended_trace(), 4), [])

    def test_rejects_no_contended_acquisition(self):
        self.assertNotEqual(rec.check_contention(contended_trace(contended=False), 4), [])
        self.assertNotEqual(rec.check_contention(contended_trace(wait_ns=0), 4), [])

    def test_rejects_fewer_client_threads_than_connections(self):
        self.assertNotEqual(rec.check_contention(contended_trace(threads=3), 4), [])
        shared = contended_trace()
        shared[3]["thread"] = shared[2]["thread"]
        self.assertNotEqual(rec.check_contention(shared, 4), [])


def run_a(by="server", reason="shutdown", stop=True, connections=2):
    specs = [ev("conn.open", conn=i, thread=client(i)) for i in range(1, connections + 1)]
    specs += [ev("conn.disconnect", {"by": by}, conn=i) for i in range(1, connections + 1)]
    specs += [ev("conn.close", {"reason": reason}, conn=i) for i in range(1, connections + 1)]
    if stop:
        specs.append(ev("server.stop"))
    return trace(*specs)


def run_b(exists=True):
    return trace(ev("store.load", {"exists": exists, "keys": 1}), ev("server.start"), ev("server.stop"))


class ShutdownTest(unittest.TestCase):
    def test_accepts_a_server_side_shutdown_and_a_reload(self):
        self.assertEqual(rec.check_shutdown(run_a(), run_b()), [])

    def test_rejects_run_a_without_each_part_of_the_shutdown(self):
        self.assertNotEqual(rec.check_shutdown(run_a(by="peer"), run_b()), [])
        self.assertNotEqual(rec.check_shutdown(run_a(reason="disconnect"), run_b()), [])
        self.assertNotEqual(rec.check_shutdown(run_a(stop=False), run_b()), [])
        self.assertNotEqual(rec.check_shutdown(run_a(connections=1), run_b()), [])

    def test_rejects_run_b_that_did_not_find_the_store_file(self):
        self.assertNotEqual(rec.check_shutdown(run_a(), run_b(exists=False)), [])


def framing_events(first=1024, second=84, send_bytes=1500):
    return trace(ev("conn.recv", {"bytes": first}), ev("conn.recv", {"bytes": second}),
                 ev("conn.send", {"bytes": send_bytes}))


def log_rec(op, nbytes=None, text="", client_name="c", **extra):
    return dict({"client": client_name, "op": op, "bytes": nbytes, "text": text}, **extra)


def framing_log(print_chunks=(1024, 600), oversize=1108):
    out = [log_rec("send", oversize, "SET big " + "x" * (oversize - 8)), log_rec("recv", 40, "Added"),
           log_rec("send", 5, "PRINT")]
    out += [log_rec("recv", n, "y" * n) for n in print_chunks]
    return out


class FramingTest(unittest.TestCase):
    def test_accepts_a_split_oversize_send_and_a_split_print_reply(self):
        self.assertEqual(rec.check_framing(framing_events(), framing_log()), [])

    def test_rejects_an_oversize_send_read_as_a_single_recv(self):
        single = trace(ev("conn.recv", {"bytes": 1108}), ev("conn.send", {"bytes": 1500}))
        self.assertNotEqual(rec.check_framing(single, framing_log()), [])

    def test_rejects_a_first_recv_of_other_than_1024_bytes(self):
        self.assertNotEqual(rec.check_framing(framing_events(first=1000, second=108), framing_log()), [])

    def test_rejects_a_client_log_without_a_split_print_reply(self):
        self.assertNotEqual(rec.check_framing(framing_events(), framing_log(print_chunks=(600,))), [])
        self.assertNotEqual(rec.check_framing(framing_events(), framing_log(print_chunks=(900, 700))), [])
        self.assertNotEqual(rec.check_framing(framing_events(send_bytes=900), framing_log()), [])

    def test_merged_sends_are_detected_but_their_absence_is_not_a_problem(self):
        merged = trace(ev("conn.recv", {"bytes": 16}))
        apart = trace(ev("conn.recv", {"bytes": 8}), ev("conn.recv", {"bytes": 8}))
        self.assertTrue(rec.merged_recv(merged, [8, 8]))
        self.assertFalse(rec.merged_recv(apart, [8, 8]))


class BacklogTest(unittest.TestCase):
    def test_accepts_a_second_accept_after_the_first_close(self):
        ok = trace(ev("conn.accept", conn=1), ev("conn.close", conn=1), ev("conn.accept", conn=2))
        self.assertEqual(rec.check_backlog(ok), [])

    def test_rejects_a_second_accept_before_the_first_close(self):
        early = trace(ev("conn.accept", conn=1), ev("conn.accept", conn=2), ev("conn.close", conn=1))
        self.assertNotEqual(rec.check_backlog(early), [])

    def test_rejects_traces_without_two_accepts_or_without_the_first_close(self):
        self.assertNotEqual(rec.check_backlog([]), [])
        self.assertNotEqual(rec.check_backlog(trace(ev("conn.accept", conn=1))), [])
        self.assertNotEqual(rec.check_backlog(trace(ev("conn.accept", conn=1), ev("conn.accept", conn=2))), [])


class PathLeakTest(unittest.TestCase):
    def test_flags_the_repo_home_and_temporary_paths_and_passes_a_relative_one(self):
        repo, home = os.path.abspath(os.path.join(os.sep, "work", "key-value-storage")), os.path.abspath(
            os.path.join(os.sep, "home", "someone"))
        tmp = tempfile.gettempdir()
        paths = [repo, home, tmp]
        self.assertEqual(rec.path_leaks('{"path":"store.txt"}', paths), [])
        for path in paths:
            self.assertNotEqual(rec.path_leaks('{"path":"%s"}' % path.replace("\\", "\\\\"), paths), [], path)
            self.assertNotEqual(rec.path_leaks("see %s/store.txt" % path.replace("\\", "/"), paths), [], path)


if __name__ == "__main__":
    unittest.main()
