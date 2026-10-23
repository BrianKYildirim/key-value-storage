"""store.txt: always UTF-8 with "\\n", older files still readable, malformed lines reported."""

import locale
import os
import subprocess
import sys
import tempfile
import unittest

from support import CliServer, Client, RunningServer, server

SET_UNICODE = "SET ключ значение".encode()


class StoreFileFormatTest(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "uses SIGINT and a C locale")
    def test_utf8_and_newlines_do_not_depend_on_the_locale(self):
        ascii_locale = {"LC_ALL": "C", "LANG": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0"}
        encoding = subprocess.run([sys.executable, "-c", "import locale; print(locale.getpreferredencoding(False))"],
                                  env=dict(os.environ, **ascii_locale), capture_output=True, text=True).stdout.strip()
        # stdout stays UTF-8 here: the server logs every command to stdout, and an ASCII stdout cannot print
        # this command at all (a separate, known limitation documented in the README).
        ascii_locale["PYTHONIOENCODING"] = "utf-8"
        if encoding.lower().replace("-", "") in ("utf8",):
            self.skipTest("this system keeps UTF-8 even in the C locale")
        with tempfile.TemporaryDirectory() as d:
            cli = CliServer(["--host", "127.0.0.1", "--port", "0"], cwd=d, env=ascii_locale)
            try:
                with Client(cli.port) as c:
                    # Before the fix, save() raised UnicodeEncodeError here (the file used the locale's
                    # %s encoding), the client still got "Added key" and the file was left truncated.
                    self.assertEqual(c.cmd(SET_UNICODE), "Added key 'ключ' with value 'значение'\n".encode())
                    self.assertEqual(c.cmd(b"SET plain value"), b"Added key 'plain' with value 'value'\n")
                    c.send(b"quit")
            finally:
                code, stdout, _ = cli.interrupt()
            with open(os.path.join(d, "store.txt"), "rb") as f:
                self.assertEqual(f.read(), "ключ\tзначение\nplain\tvalue\n".encode("utf-8"))
        self.assertEqual(code, 0)
        self.assertNotIn("Error saving data", stdout)

    def test_files_from_older_versions_are_still_read(self):
        # The old save() used the platform's default encoding, e.g. cp1252 on Windows.
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "store.txt"), "wb") as f:
                f.write("café\tcrème\r\nplain\tvalue\r\n".encode("cp1252"))
            original = locale.getpreferredencoding
            locale.getpreferredencoding = lambda do_setlocale=True: "cp1252"
            try:
                with RunningServer(store_dir=d) as rs:
                    loaded = dict(rs.server.kv_store.store)
                    with Client(rs.port) as c:
                        c.cmd(b"SET x 1")  # the next save rewrites the file as UTF-8
                        c.send(b"quit")
                    rewritten = rs.read_store()
            finally:
                locale.getpreferredencoding = original
        self.assertEqual(loaded, {"café": "crème", "plain": "value"})
        self.assertEqual(rewritten, "café\tcrème\nplain\tvalue\nx\t1\n".encode("utf-8"))
        self.assertNotIn("Error loading data", rs.stdout.getvalue())


class MalformedLinesTest(unittest.TestCase):
    def load(self, raw):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "store.txt")
            with open(path, "wb") as f:
                f.write(raw)
            with RunningServer(store_dir=d, observe=True) as rs:
                store = dict(rs.server.kv_store.store)
                load = rs.log.of_type("store.load")[0]["data"]
        return store, load, rs.stdout.getvalue(), path

    def test_malformed_lines_are_skipped_and_reported(self):
        store, load, stdout, path = self.load(b"a\t1\nno tab here\n\nb\t2\nkey only\t\n")
        self.assertEqual(store, {"a": "1", "b": "2"})
        self.assertIn("Skipped 2 malformed line(s) in %s: expected 'key<TAB>value'.\n" % path, stdout)
        self.assertEqual((load["ok"], load["keys"], load["lines_skipped"]), (True, 2, 2))

    def test_a_well_formed_file_prints_nothing_extra(self):
        store, load, stdout, _ = self.load(b"a\t1\n\nb\t2\n")
        self.assertEqual(store, {"a": "1", "b": "2"})
        self.assertNotIn("Skipped", stdout)
        self.assertEqual(load["lines_skipped"], 0)

    def test_parse_has_no_unreachable_branch(self):
        parser = server.CommandParser(object())
        for blank in ("", "   ", "\t\r\n", "  "):
            self.assertEqual(parser.parse(blank), (None, [], "empty", "ERROR: Empty command\n"))


if __name__ == "__main__":
    unittest.main()
