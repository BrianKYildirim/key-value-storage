# Encapsulates the client logic for connecting to the
# server, sending commands, and receiving responses.

import socket  # provides networking capabilities for the client.


class Client:
    def __init__(self, host="localhost", port=3490):
        self.host = host  # The server’s address and port the client will connect to.
        self.port = port
        self.sock = None  # Will hold the socket object once connected.

    def connect(self):
        """Establish a connection to the server."""
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)  # Creates a TCP socket using IPv4.
            self.sock.connect((self.host, self.port))
            print(f"Connected to server at {self.host}:{self.port}", flush=True)
            # Attempts to connect to the server; if successful,
            # prints a confirmation; otherwise, prints an error message

        except Exception as e:
            print(f"Failed to connect to server: {e}", flush=True)
            self.sock = None

    def send(self, message):
        """
        Encodes the message as bytes and sends it over the
        socket using sendall(), ensuring the entire message is sent.
        """

        if self.sock:
            try:
                self.sock.sendall(message.encode())
            except Exception as e:
                print(f"Error sending message: {e}", flush=True)

    def receive(self):
        """
        Waits for up to 1024 bytes from the server. If data is received,
        decodes and returns it; otherwise, returns None if the connection
        is closed or an error occurs
        """

        if self.sock:
            try:
                data = self.sock.recv(1024)
                if not data:
                    return None
                return data.decode()
            except Exception as e:
                print(f"Error receiving data: {e}", flush=True)
                return None

    def run(self):
        self.connect()  # Calls connect() to establish a connection.
        if not self.sock:
            return
        try:
            while True:  # Prompts the user for a command, sends it to the server, and then waits for a response.
                message = input("Enter command (or 'quit' to exit): ")
                self.send(message)
                if message.lower() == "quit":
                    print("Exiting client.", flush=True)
                    break
                    # If the command is “quit” (or if the server disconnects), it
                    # breaks out of the loop and then closes the socket.

                response = self.receive()
                if response is None:
                    print("Server disconnected.", flush=True)
                    break
                print(f"Response: {response}", flush=True)
        except KeyboardInterrupt:  # Catches Ctrl+C for a graceful shutdown.
            print("\nClient shutting down.", flush=True)
        finally:
            if self.sock:
                self.sock.close()


if __name__ == "__main__":
    client = Client()
    client.run()
