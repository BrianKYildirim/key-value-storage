import socket
import threading
import os

"""
socket: Provides low‐level networking functionality to create TCP connections.
threading: Allows creation and management of threads so that multiple clients can be handled concurrently.
os: Provides operating system interfaces (here, used to check file existence).
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
    def __init__(self, filepath="store.txt"):
        self.filepath = filepath  # The file where data will be saved.
        self.store = {}  # store: A dictionary holding the key–value pairs.
        self.lock = threading.RLock()
        """
        A reentrant lock (RLock) ensures that if a method (like set()) calls another
        method (like save()) that also needs a lock, the same thread can re-acquire it without deadlocking.
        """

        self.load()  # Called to populate the store with any pre-existing data.

    def load(self):
        """
        Load key-value pairs from the file into the store.
        Each line in the file should be formatted as 'key<TAB>value'.
        """
        if not os.path.exists(self.filepath):  # Checks if the persistence file exists.
            return  # Ensures thread-safe file reading and dictionary updates.
        with self.lock:
            try:
                with open(self.filepath, "r") as file:
                    for line in file:
                        line = line.strip()
                        if line:
                            # Split only on the first tab character
                            parts = line.split("\t", 1)
                            if len(parts) == 2:
                                key, value = parts
                                self.store[key] = value
                                """
                                Each non-empty line is stripped and split on the first tab character.
                                If two parts result, they are stored as key and value.
                                """

            except Exception as e:
                print(f"Error loading data: {e}")

    def save(self):
        try:
            with open(self.filepath, "w") as file:
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

    def set(self, key, value):
        with self.lock:  # Ensures that updating the store and saving to disk are atomic operations.
            print(f"[DEBUG] Setting key '{key}' to value '{value}'", flush=True)
            self.store[key] = value
            print("[DEBUG] Calling save()", flush=True)
            self.save()
            print("[DEBUG] Finished save()", flush=True)
        return f"Added key '{key}' with value '{value}'\n"  # Confirms that the key has been added.

    def get(self, key):  # Retrieve the value for the specified key.
        with self.lock:  # Uses the lock for safe reading from the dictionary.
            if key in self.store:
                return self.store[key]
            else:
                return f"Key '{key}' not found."  # Either the associated value or an error message returned.

    def remove(self, key):
        """Remove a key-value pair from the store."""
        with self.lock:
            if key in self.store:
                del self.store[key]
                self.save()
                return f"Removed key '{key}'.\n"
            else:
                return f"Key '{key}' not found."

    def print_store(self):
        """Return a formatted string of all key-value pairs."""
        with self.lock:
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

    def parse_and_execute(self, command):
        # Remove extra whitespace
        command = command.strip()
        if not command:
            return "ERROR: Empty command\n"
        tokens = command.split()
        if not tokens:
            return "ERROR: Invalid command\n"

        # Convert command to uppercase to allow case-insensitive commands.
        cmd = tokens[0].upper()

        # Checks the correct number of arguments for
        # each command and then calls the corresponding KeyValueStore method.
        if cmd == "SET":
            if len(tokens) != 3:
                return "ERROR: SET command requires 2 arguments: key and value\n"
            key, value = tokens[1], tokens[2]
            return self.kv_store.set(key, value)
        elif cmd == "GET":
            if len(tokens) != 2:
                return "ERROR: GET command requires 1 argument: key\n"
            key = tokens[1]
            return self.kv_store.get(key)
        elif cmd == "REMOVE":
            if len(tokens) != 2:
                return "ERROR: REMOVE command requires 1 argument: key\n"
            key = tokens[1]
            return self.kv_store.remove(key)
        elif cmd == "PRINT":
            return self.kv_store.print_store()
        else:
            return f"ERROR: Unknown command '{tokens[0]}'\n"


# The Server class is responsible for setting up the TCP socket, accepting
# incoming connections, and creating a new thread for each client.
# Encapsulates the server’s networking functionality.
class Server:
    def __init__(self, host="0.0.0.0", port=3490):
        self.host = host  # Where the server listens.
        self.port = port
        self.server_socket = None  # The socket used to accept incoming connections.
        self.kv_store = KeyValueStore()  # Shared store and command parser used by all clients.
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

            print(f"Server listening on {self.host}:{self.port}")
        except Exception as e:
            raise ConnectionException(f"Failed to setup server: {e}")

    def handle_client(self, client_socket, client_address):
        print(f"Connection from {client_address}")  # Prints the client’s address upon connection and disconnection.
        with client_socket:
            while True:
                try:
                    data = client_socket.recv(1024)  # Reads up to 1024 bytes from the client.
                    if not data:
                        print(f"Client {client_address} disconnected.")
                        break
                    message = data.decode().strip()
                    print(f"Received from {client_address}: {message}")
                    if message.lower() == "quit":
                        print(f"Client {client_address} requested to quit.")
                        break  # Decodes the received bytes, strips extra whitespace, and checks for a “quit” command.

                    response = self.parser.parse_and_execute(message)
                    client_socket.sendall(response.encode())
                    # Passes the message to CommandParser and sends the resulting response back to the client.

                    print(f"Response sent to {client_address}: {response.strip()}")
                except Exception as e:
                    print(f"Error handling client {client_address}: {e}")
                    break
                    # Catches any exceptions during client handling and closes the connection gracefully.

        print(f"Connection with {client_address} closed.")

    def start(self):
        """Start the server: set up the socket and continuously accept new client connections."""
        self.setup_server()
        self.is_running = True
        try:
            while self.is_running:  # After setting up, continuously accepts new client connections.
                client_socket, client_address = self.server_socket.accept()
                # Create a new thread for each client connection.
                client_thread = threading.Thread(target=self.handle_client, args=(client_socket, client_address))
                client_thread.daemon = True  # allows threads to close when main thread exits
                client_thread.start()
        except KeyboardInterrupt:
            print("Server is shutting down gracefully.")
            # Uses a try/except block to catch KeyboardInterrupt (Ctrl+C) and then calls stop().
        finally:
            self.stop()

    def stop(self):
        # Closes the server socket and updates the running flag to stop accepting new connections.
        self.is_running = False
        if self.server_socket:
            self.server_socket.close()
            print("Server socket closed.")


if __name__ == "__main__":
    server = Server()
    server.start()
