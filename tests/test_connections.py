"""Connection limit and graceful shutdown."""

import os
import socket
import tempfile
import threading
import time
import unittest

from support import TIMEOUT, CliServer, Client, RunningServer, parse_store_file, server


class ConnectionLimitTest(unittest.TestCase):
    def test_clients_beyond_the_limit_wait_for_a_free_slot(self):
        with RunningServer(observe=True, server_kwargs={"max_connections": 2}) as rs:
            first, second = Client(rs.port), Client(rs.port)
            self.assertEqual(first.cmd(b"SET a 1"), b"Added key 'a' with value '1'\n")
            self.assertEqual(second.cmd(b"GET a"), b"1")
            third = Client(rs.port)  # TCP connects (listen backlog), but no thread serves it yet
            third.send(b"GET a")
            third.sock.settimeout(0.7)
            with self.assertRaises(socket.timeout):
                third.recv()
            diagnostics = rs.observer.diagnostics()
            self.assertEqual(diagnostics["connections"]["current"], 2)
            self.assertEqual(diagnostics["server"]["max_connections"], 2)
            first.send(b"quit")
            first.close()
            third.sock.settimeout(TIMEOUT)
            self.assertEqual(third.recv(), b"1")  # served as soon as a slot was free
            for c in (second, third):
                c.send(b"quit")
                c.close()

    def test_limit_must_be_positive(self):
        with self.assertRaises(ValueError):
            server.Server(port=0, store_path=os.devnull, max_connections=0)


class GracefulShutdownTest(unittest.TestCase):
    def test_open_connections_are_closed_cleanly(self):
        rs = RunningServer(observe=True)
        rs.__enter__()
        try:
            client = Client(rs.port)
            self.assertEqual(client.cmd(b"SET a 1"), b"Added key 'a' with value '1'\n")
            address = client.address
            rs.server.shutdown()
            rs.thread.join(TIMEOUT)
            self.assertFalse(rs.thread.is_alive())
            self.assertEqual(client.recv(), b"")  # the server closed the idle connection
            client.close()
        finally:
            rs.__exit__(None, None, None)
        peer = str(tuple(address))
        self.assertTrue(rs.stdout.getvalue().endswith(
            "Server socket closed.\nClosing connection with %s: server is shutting down.\n"
            "Connection with %s closed.\n" % (peer, peer)))
        events = rs.log.events
        disconnect = [e for e in events if e["type"] == "conn.disconnect"][0]
        close = [e for e in events if e["type"] == "conn.close"][0]
        stop = [e for e in events if e["type"] == "server.stop"][0]
        self.assertEqual(disconnect["data"], {"by": "server"})
        self.assertEqual(close["data"]["reason"], "shutdown")
        self.assertEqual(stop["data"]["connections_open"], 0)
        self.assertLess(close["seq"], stop["seq"])

    def test_a_command_in_progress_finishes_before_its_connection_closes(self):
        rs = RunningServer(observe=True)
        rs.__enter__()
        try:
            client = Client(rs.port)
            lock = rs.server.kv_store.lock
            lock.acquire()  # the next SET will wait inside the command, holding no reply yet
            try:
                client.send(b"SET late 1")
                rs.log.wait_for(lambda e: e["type"] == "lock.wait_start" and e["cmd"] is not None)
                stopper = threading.Thread(target=rs.server.shutdown)
                stopper.start()
                stopper.join(TIMEOUT)
                time.sleep(0.3)  # stop() is now waiting for the handler
            finally:
                lock.release()
            self.assertEqual(client.recv(), b"Added key 'late' with value '1'\n")
            self.assertEqual(client.recv(), b"")
            client.close()
            rs.thread.join(TIMEOUT)
            on_disk = parse_store_file(rs.store_path)
        finally:
            rs.__exit__(None, None, None)
        self.assertEqual(on_disk, {"late": "1"})
        self.assertEqual([e["data"]["reason"] for e in rs.log.events if e["type"] == "conn.close"], ["shutdown"])

    @unittest.skipUnless(os.name == "posix", "uses SIGINT")
    def test_ctrl_c_with_a_connected_client(self):
        with tempfile.TemporaryDirectory() as d:
            cli = CliServer(["--host", "127.0.0.1", "--port", "0"], cwd=d)
            client = Client(cli.port)
            try:
                self.assertEqual(client.cmd(b"SET a 1"), b"Added key 'a' with value '1'\n")
                peer = str(tuple(client.address))
                time.sleep(0.3)
                started = time.monotonic()
                code, stdout, _ = cli.interrupt()
                elapsed = time.monotonic() - started
                self.assertEqual(client.recv(), b"")
            finally:
                client.close()
        self.assertEqual(code, 0)
        self.assertLess(elapsed, server.Server.SHUTDOWN_GRACE_SECONDS)
        self.assertTrue(stdout.endswith(
            "Server is shutting down gracefully.\nServer socket closed.\n"
            "Closing connection with %s: server is shutting down.\nConnection with %s closed.\n" % (peer, peer)))


if __name__ == "__main__":
    unittest.main()
