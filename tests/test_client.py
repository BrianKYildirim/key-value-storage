"""client.py: blank lines are not sent, and quit is recognised the same way the server recognises it."""

import threading
import unittest

from support import TIMEOUT, RunningServer

import client


class InteractiveClientTest(unittest.TestCase):
    def test_blank_lines_and_padded_quit(self):
        lines = iter(["", "   ", "SET a 1", "  quit  "])
        client.input = lambda prompt="": next(lines)  # shadows the built-in input() inside client.py only
        self.addCleanup(delattr, client, "input")
        with RunningServer(observe=True) as rs:
            c = client.Client(host="127.0.0.1", port=rs.port)
            t = threading.Thread(target=c.run)
            t.start()
            t.join(TIMEOUT)  # before the fix, the first blank line made the client wait forever
            self.assertFalse(t.is_alive())
            rs.log.wait_for(lambda e: e["type"] == "conn.close")
            commands = rs.observer.diagnostics()["commands"]
        output = rs.stdout.getvalue()
        self.assertIn("Response: Added key 'a' with value '1'\n", output)
        self.assertIn("Exiting client.\n", output)
        self.assertNotIn("Server disconnected.", output)
        self.assertEqual(commands, {"SET": {"ok": 1}, "QUIT": {"ok": 1}})  # nothing else was sent


if __name__ == "__main__":
    unittest.main()
