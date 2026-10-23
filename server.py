import argparse
import locale
import socket
import sys
import threading
import os
import time

"""
socket: Provides low‐level networking functionality to create TCP connections.
threading: Allows creation and management of threads so that multiple clients can be handled concurrently.
os: Provides operating system interfaces (here, used to check file existence).
argparse, sys: Command-line options and the stderr notices of the opt-in observability features.
locale, time: Reading store files written by older versions, and bounding the wait at shutdown.
"""


class NetworkException(Exception):
    pass


class ConnectionException(NetworkException):
    pass


class MessageException(NetworkException):
    pass


class TimeoutException(NetworkException):
    pass


"""
These custom exceptions inherit from Python’s base Exception and allow the
program to differentiate and handle various network‐related errors
(connection issues, message errors, timeouts) more gracefully.
"""


class KeyValueStore:  # Manages an in-memory dictionary for key–value pairs and persists them to a file.
    def __init__(self, filepath="store.txt", observer=None):
        self.filepath = filepath  # The file where data will be saved.
        self.store = {}  # store: A dictionary holding the key–value pairs.
        # Optional observability.Observer. None (the default) disables every tracing hook below.
        self.observer = observer
        self.lock = threading.RLock() if observer is None else observer.instrument_lock("store")
        """
        A reentrant lock (RLock) ensures that if a method (like set()) calls another
        method (like save()) that also needs a lock, the same thread can re-acquire it without deadlocking.
        """

        self.load()  # Called to populate the store with any pre-existing data.

    def load(self):
        """
        Load key-value pairs from the file into the store.
        Each line in the file should be formatted as 'key<TAB>value'. Lines without a tab are skipped and reported.
        """
        obs = self.observer
        if not os.path.exists(self.filepath):  # Checks if the persistence file exists.
            if obs is not None:
                obs.load_skipped(self)
            return  # Ensures thread-safe file reading and dictionary updates.
        with self.lock:
            if obs is not None:
                token = obs.load_started(self)
            records = {}
            try:
                try:
                    try:
                        skipped = self._read_records("utf-8", records)
                    except UnicodeDecodeError:
                        # Older versions wrote the file in the platform's default encoding instead of UTF-8.
                        records.clear()
                        skipped = self._read_records(locale.getpreferredencoding(False), records)
                finally:
                    self.store.update(records)  # On an error, keep the lines read before it, as before.
                if skipped:
                    print(f"Skipped {skipped} malformed line(s) in {self.filepath}: expected 'key<TAB>value'.")
            except Exception as e:
                print(f"Error loading data: {e}")
                if obs is not None:
                    obs.load_finished(self, token, e)
            else:
                if obs is not None:
                    obs.load_finished(self, token, skipped=skipped)

    def _read_records(self, encoding, records):
        """Parse the file with the given encoding into records. Returns the number of malformed lines."""
        skipped = 0
        with open(self.filepath, "r", encoding=encoding) as file:
            for line in file:
                line = line.strip()
                if line:
                    # Split only on the first tab character
                    parts = line.split("\t", 1)
                    if len(parts) == 2:
                        key, value = parts
                        records[key] = value
                        """
                        Each non-empty line is stripped and split on the first tab character.
                        If two parts result, they are stored as key and value.
                        """
                    else:
                        skipped += 1
        return skipped

    def save(self):
        obs = self.observer
        if obs is not None:
            token = obs.save_started(self)
        try:
            # Always UTF-8 with "\n" line endings, so the file is the same on every platform.
            with open(self.filepath, "w", encoding="utf-8", newline="\n") as file:
                for key, value in self.store.items():
                    file.write(f"{key}\t{value}\n")
                    """
                    The file is opened (or created) for writing;
                    each key–value pair is written on a separate line, separated by a tab.
                    """

        except Exception as e:
            print(f"[ERROR] Error saving data: {e}", flush=True)
            """
            If any error occurs, it’s printed immediately.
            """
            if obs is not None:
                obs.save_finished(self, token, e)
        else:
            if obs is not None:
                obs.save_finished(self, token)

    def set(self, key, value):
        with self.lock:  # Ensures that updating the store and saving to disk are atomic operations.
            obs = self.observer
            if obs is not None:
                existed = key in self.store
            self.store[key] = value
            if obs is not None:
                obs.store_mutated("set", key, value, existed, len(self.store))
            self.save()
        return f"Added key '{key}' with value '{value}'\n"  # Confirms that the key has been added.

    def get(self, key):  # Retrieve the value for the specified key.
        with self.lock:  # Uses the lock for safe reading from the dictionary.
            if self.observer is not None:
                self.observer.store_lookup("get", key, key in self.store, len(self.store))
            if key in self.store:
                return self.store[key]
            else:
                return f"Key '{key}' not found."  # Either the associated value or an error message returned.

    def remove(self, key):
        """Remove a key-value pair from the store."""
        with self.lock:
            if key in self.store:
                del self.store[key]
                if self.observer is not None:
                    self.observer.store_mutated("remove", key, None, True, len(self.store))
                self.save()
                return f"Removed key '{key}'.\n"
            else:
                if self.observer is not None:
                    self.observer.store_lookup("remove", key, False, len(self.store))
                return f"Key '{key}' not found."

    def print_store(self):
        """Return a formatted string of all key-value pairs."""
        with self.lock:
            if self.observer is not None:
                self.observer.store_lookup("print", None, None, len(self.store))
            if not self.store:
                return "Store is empty.\n"
            response = ""
            for key, value in self.store.items():
                response += f"[KEY]: {key}\t[VALUE]: {value}\n"
            return response


# This class parses incoming text commands and maps them to the corresponding
# operations on the key-value store.
# Takes a KeyValueStore instance to execute commands on it.
class CommandParser:
    def __init__(self, kv_store):
        self.kv_store = kv_store
        self.observer = getattr(kv_store, "observer", None)  # Tracing follows the store's observer.

    def parse(self, command):
        """
        Validate a command without executing it. Returns (cmd, tokens, rejection, error_response):
        rejection is None for an executable command, otherwise "empty", "arity" or "unknown",
        and error_response is then the exact reply parse_and_execute() sends back.
        """
        # Remove extra whitespace
        command = command.strip()
        if not command:
            return None, [], "empty", "ERROR: Empty command\n"
        # A non-empty stripped string always splits into at least one token: strip() and split() use the
        # same definition of whitespace.
        tokens = command.split()

        # Convert command to uppercase to allow case-insensitive commands.
        cmd = tokens[0].upper()

        # Checks the correct number of arguments for each command.
        if cmd == "SET":
            if len(tokens) != 3:
                return cmd, tokens, "arity", "ERROR: SET command requires 2 arguments: key and value\n"
        elif cmd == "GET":
            if len(tokens) != 2:
                return cmd, tokens, "arity", "ERROR: GET command requires 1 argument: key\n"
        elif cmd == "REMOVE":
            if len(tokens) != 2:
                return cmd, tokens, "arity", "ERROR: REMOVE command requires 1 argument: key\n"
        elif cmd != "PRINT":
            return cmd, tokens, "unknown", f"ERROR: Unknown command '{tokens[0]}'\n"
        return cmd, tokens, None, None

    def parse_and_execute(self, command):
        cmd, tokens, rejection, error_response = self.parse(command)
        if self.observer is not None:
            self.observer.command_parsed(cmd, tokens, rejection)
        if rejection is not None:
            return error_response

        # Calls the KeyValueStore method that corresponds to the validated command.
        if cmd == "SET":
            key, value = tokens[1], tokens[2]
            return self.kv_store.set(key, value)
        elif cmd == "GET":
            return self.kv_store.get(tokens[1])
        elif cmd == "REMOVE":
            return self.kv_store.remove(tokens[1])
        else:
            return self.kv_store.print_store()


# The Server class is responsible for setting up the TCP socket, accepting
# incoming connections, and creating a new thread for each client.
# Encapsulates the server’s networking functionality.
class Server:
    SHUTDOWN_GRACE_SECONDS = 5  # How long stop() waits in total for open connections to finish.
    SHUTDOWN_SETTLE_SECONDS = 1  # How long running commands get before remaining connections are closed.

    def __init__(self, host="0.0.0.0", port=3490, store_path="store.txt", observer=None, max_connections=100):
        if max_connections < 1:
            raise ValueError("max_connections must be at least 1")
        self.host = host  # Where the server listens.
        self.port = port  # 0 asks the OS for a free port; see bound_port.
        self.server_socket = None  # The socket used to accept incoming connections.
        self.bound_port = None  # The port actually bound, known once setup_server() has run.
        self.ready = threading.Event()  # Set once start() is accepting connections.
        # At most max_connections client threads exist at once; further clients wait in the listen backlog.
        self.max_connections = max_connections
        self._slots = threading.BoundedSemaphore(max_connections)
        self._connections = {}  # Handler thread -> its client socket, so stop() can close open connections.
        self._connections_lock = threading.Lock()
        self._closing = False  # True while stop() closes the remaining connections.
        # Optional observability.Observer, owned by this server only. None keeps every tracing hook off.
        self.observer = observer
        if observer is not None:
            observer.bind(self)
        # Shared store and command parser used by all clients.
        self.kv_store = KeyValueStore(store_path, observer=observer)
        self.parser = CommandParser(self.kv_store)
        self.is_running = False  # A flag to control the server loop.

    def setup_server(self):  # Uses IPv4 and TCP.
        """Initialize the server socket, bind it to the host and port, and begin listening."""
        try:
            self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            # Set socket options to allow immediate reuse of the address

            self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # SO_REUSEADDR allows the server to restart quickly without waiting for the OS to release the port.

            self.server_socket.bind((self.host, self.port))
            self.server_socket.listen(10)
            # Binds the socket and begins listening for up to 10 queued connections.
            self.bound_port = self.server_socket.getsockname()[1]  # Differs from self.port when port is 0.

            print(f"Server listening on {self.host}:{self.bound_port}")
        except Exception as e:
            if self.observer is not None:
                self.observer.error("setup", e)
            raise ConnectionException(f"Failed to setup server: {e}")
        if self.observer is not None:
            self.observer.server_listening(self)

    def handle_client(self, client_socket, client_address, conn_id=None):
        obs = self.observer
        print(f"Connection from {client_address}")  # Prints the client’s address upon connection and disconnection.
        if obs is not None:
            obs.connection_opened(conn_id, client_address)
        with client_socket:
            while True:
                receiving = True  # While waiting for data, an error during shutdown is not the client's fault.
                try:
                    data = client_socket.recv(1024)  # Reads up to 1024 bytes from the client.
                    receiving = False
                    if not data:
                        if self._closing:  # stop() shut the connection down, not the client.
                            self._report_shutdown_close(client_address)
                        else:
                            print(f"Client {client_address} disconnected.")
                            if obs is not None:
                                obs.peer_disconnected()
                        break
                    if obs is not None:
                        obs.bytes_received(len(data))
                    message = data.decode().strip()
                    if obs is not None:
                        obs.decoded(message)
                    print(f"Received from {client_address}: {message}")
                    if message.lower() == "quit":
                        print(f"Client {client_address} requested to quit.")
                        if obs is not None:
                            obs.quit_requested()
                        break  # Decodes the received bytes, strips extra whitespace, and checks for a “quit” command.

                    response = self.parser.parse_and_execute(message)
                    payload = response.encode()
                    if obs is not None:
                        obs.response_ready(len(payload))
                    client_socket.sendall(payload)
                    if obs is not None:
                        obs.response_sent()
                    # Passes the message to CommandParser and sends the resulting response back to the client.

                    print(f"Response sent to {client_address}: {response.strip()}")
                except Exception as e:
                    if receiving and self._closing and isinstance(e, OSError):
                        # On Windows, recv() raises instead of returning b"" once stop() closes the connection.
                        self._report_shutdown_close(client_address)
                    else:
                        print(f"Error handling client {client_address}: {e}")
                        if obs is not None:
                            obs.handler_error(e)
                    break
                    # Catches any exceptions during client handling and closes the connection gracefully.

        print(f"Connection with {client_address} closed.")
        if obs is not None:
            obs.connection_closed()

    def _report_shutdown_close(self, client_address):
        print(f"Closing connection with {client_address}: server is shutting down.")
        if self.observer is not None:
            self.observer.peer_disconnected(by_server=True)

    def start(self):
        """Start the server: set up the socket and continuously accept new client connections."""
        obs = self.observer
        if obs is not None:
            obs.server_starting(self)
        self.setup_server()
        self.is_running = True
        self._closing = False
        self.ready.set()
        try:
            while self.is_running:  # After setting up, continuously accepts new client connections.
                # Wait for a free slot first, so the number of client threads stays bounded. The timeout
                # lets the loop notice shutdown() while every slot is taken.
                if not self._slots.acquire(timeout=0.5):
                    continue
                try:
                    client_socket, client_address = self.server_socket.accept()
                except OSError as e:
                    self._slots.release()
                    if not self.is_running:
                        break  # shutdown() was called from another thread.
                    if obs is not None:
                        obs.error("accept", e)
                    raise
                # Create a new thread for each client connection.
                args = (client_socket, client_address)
                if obs is not None:
                    args += (obs.connection_accepted(client_address),)
                client_thread = threading.Thread(target=self._serve_client, args=args,
                                                 name="client %s:%s" % client_address[:2])
                client_thread.daemon = True  # allows threads to close when main thread exits
                with self._connections_lock:
                    self._connections[client_thread] = client_socket
                client_thread.start()
        except KeyboardInterrupt:
            print("Server is shutting down gracefully.")
            # Uses a try/except block to catch KeyboardInterrupt (Ctrl+C) and then calls stop().
        finally:
            self.stop()

    def _serve_client(self, client_socket, client_address, conn_id=None):
        """Thread body: handle one client, then give its slot back."""
        try:
            self.handle_client(client_socket, client_address, conn_id)
        finally:
            with self._connections_lock:
                self._connections.pop(threading.current_thread(), None)
            self._slots.release()

    def stop(self):
        # Closes the server socket and updates the running flag to stop accepting new connections.
        self.is_running = False
        self.ready.clear()
        if self.server_socket:
            self.server_socket.close()
            print("Server socket closed.")
        self._close_connections()
        if self.observer is not None:
            self.observer.server_stopped(self)

    def _close_connections(self):
        """
        Close the connections that are still open and wait (up to SHUTDOWN_GRACE_SECONDS) for their threads.

        1. Shut down only the receiving side. A command that is already running finishes, including its save
           and its reply. On Linux this also wakes handlers waiting in recv(), which then close themselves.
        2. After up to SHUTDOWN_SETTLE_SECONDS, close whatever is still open. This is needed on Windows, where
           shutdown() does not wake a blocked recv() but close() does, and for a handler stuck sending to a
           client that no longer reads.
        """
        with self._connections_lock:
            connections = list(self._connections.items())
        if not connections:
            return
        self._closing = True
        deadline = time.monotonic() + self.SHUTDOWN_GRACE_SECONDS
        for _, client_socket in connections:
            try:
                client_socket.shutdown(socket.SHUT_RD)
            except OSError:
                pass
        self._join(connections, min(deadline, time.monotonic() + self.SHUTDOWN_SETTLE_SECONDS))
        for thread, client_socket in connections:
            if thread.is_alive():
                try:
                    client_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                client_socket.close()
        self._join(connections, deadline)

    @staticmethod
    def _join(connections, until):
        for thread, _ in connections:
            thread.join(max(0.0, until - time.monotonic()))

    def shutdown(self):
        """
        Make a running start() return, from any thread (for tests and embedding). Closing the socket
        from another thread does not wake a blocked accept() on Linux, but shutting it down does.
        Call it after ready is set; start() then runs stop() itself.
        """
        self.is_running = False
        if self.server_socket:
            try:
                self.server_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                self.server_socket.close()


def parse_args(argv=None):
    """Command-line options. Each falls back to a KVS_* environment variable, then to the original default."""
    env = os.environ.get
    parser = argparse.ArgumentParser(
        description="Multithreaded TCP key-value server. All observability features are opt-in.")
    parser.add_argument("--host", default=env("KVS_HOST", "0.0.0.0"),
                        help="address to listen on (default 0.0.0.0; env KVS_HOST)")
    parser.add_argument("--port", type=int, default=env("KVS_PORT", "3490"),
                        help="TCP port; 0 picks a free port, also reported on stderr (default 3490; env KVS_PORT)")
    parser.add_argument("--store", default=env("KVS_STORE", "store.txt"),
                        help="persistence file (default store.txt in the working directory; env KVS_STORE)")
    parser.add_argument("--max-connections", metavar="N", type=int, default=env("KVS_MAX_CONNECTIONS", "100"),
                        help="clients served at once; more wait until one leaves (default 100; "
                             "env KVS_MAX_CONNECTIONS)")
    group = parser.add_argument_group("observability (all off by default)")
    group.add_argument("--trace-jsonl", metavar="PATH", default=env("KVS_TRACE_JSONL"),
                       help="append one JSON event per line to PATH, '-' for stderr (env KVS_TRACE_JSONL)")
    group.add_argument("--inspect-port", metavar="PORT", type=int, default=env("KVS_INSPECT_PORT"),
                       help="serve the read-only inspection endpoint on PORT, 0 for a free port "
                            "(env KVS_INSPECT_PORT)")
    group.add_argument("--inspect-host", default=env("KVS_INSPECT_HOST", "127.0.0.1"),
                       help="address of the inspection endpoint (default 127.0.0.1; env KVS_INSPECT_HOST)")
    group.add_argument("--redact-values", action="store_true", default=env("KVS_REDACT_VALUES", "") not in ("", "0"),
                       help="hide values (not keys) in traces and inspection output (env KVS_REDACT_VALUES=1)")
    group.add_argument("--trace-max-chars", metavar="N", type=int, default=256,
                       help="cut keys, values and messages in observability output to N characters (default 256)")
    group.add_argument("--trace-buffer", metavar="N", type=int, default=4096,
                       help="number of recent events kept for the inspection endpoint (default 4096)")
    return parser.parse_args(argv)


def _announce_port(server):
    if server.ready.wait():
        print(f"Listening on {server.host}:{server.bound_port}", file=sys.stderr, flush=True)


def main(argv=None):
    args = parse_args(argv)
    observer = sink = inspector = None
    if args.trace_jsonl or args.inspect_port is not None:
        import observability  # Only loaded when an observability option is used.

        observer = observability.Observer(redact_values=args.redact_values, max_chars=args.trace_max_chars,
                                          buffer_size=args.trace_buffer)
        if args.trace_jsonl:
            sink = observability.JsonlSink(observer, args.trace_jsonl)  # Subscribed before the store loads.
    server = Server(host=args.host, port=args.port, store_path=args.store, observer=observer,
                    max_connections=args.max_connections)
    if args.inspect_port is not None:
        inspector = observability.InspectionServer(observer, args.inspect_host, args.inspect_port)
        print(f"Inspection endpoint: {inspector.url}", file=sys.stderr, flush=True)
    if args.port == 0:
        threading.Thread(target=_announce_port, args=(server,), daemon=True).start()
    try:
        server.start()
    finally:
        if inspector is not None:
            inspector.close()
        if sink is not None:
            sink.close()


if __name__ == "__main__":
    main()
