# Multithreaded TCP Key-Value Storage Server

A lightweight key-value storage service written in Python using TCP sockets and the standard library.

The server accepts multiple client connections concurrently, processes commands through a simple text-based protocol, and persists stored data to disk so it survives server restarts.

## Demo

The video below demonstrates how to start the server, connect with a client, and use the supported commands.

https://github.com/user-attachments/assets/721b5ab5-2df6-498e-96f0-17ff72d300ff

## Features

- TCP client-server communication using Python sockets
- A dedicated thread for each connected client
- Thread-safe access to the shared key-value store
- Persistent storage using a local `store.txt` file
- Custom command-line protocol
- Graceful client disconnects and server shutdown
- No third-party dependencies
- Compatible with Python 3

## How It Works

The project contains two main components:

### Server

`server.py`:

- Creates and binds a TCP socket on port `3490`
- Listens for incoming client connections
- Starts a daemon thread for each client
- Parses and executes client commands
- Stores key-value pairs in memory
- Persists updates to `store.txt`
- Uses a reentrant lock to protect shared state

### Client

`client.py`:

- Connects to the server over TCP
- Reads commands from the terminal
- Sends commands to the server
- Displays server responses
- Closes the connection when the user enters `quit`

## Project Structure

```text
key-value-storage/
├── client.py       # Interactive TCP client
├── server.py       # Multithreaded TCP server and key-value store
├── store.txt       # Persistent data file created at runtime
└── README.md
```

## Requirements

- Python 3.8 or newer
- No external packages are required

The project uses only Python standard-library modules, including:

- `socket`
- `threading`
- `os`

## Getting Started

### 1. Clone the repository

```bash
git clone https://github.com/BrianKYildirim/key-value-storage.git
cd key-value-storage
```

### 2. Start the server

Open a terminal in the project directory and run:

```bash
python server.py
```

The server listens on:

```text
0.0.0.0:3490
```

You should see:

```text
Server listening on 0.0.0.0:3490
```

### 3. Start a client

Open a second terminal in the same directory and run:

```bash
python client.py
```

The client connects to the local server:

```text
Connected to server at localhost:3490
```

You can then enter commands interactively.

## Command Protocol

Commands are case-insensitive. Each command should be entered on its own line.

### `SET`

Stores or updates a key-value pair.

```text
SET username brian
```

Example response:

```text
Added key 'username' with value 'brian'
```

> Values currently support a single whitespace-delimited token. For example, `SET first_name Brian` works, but values containing spaces are not supported by the current parser.

### `GET`

Retrieves the value associated with a key.

```text
GET username
```

Example response:

```text
brian
```

If the key does not exist:

```text
Key 'username' not found.
```

### `REMOVE`

Deletes a key-value pair.

```text
REMOVE username
```

Example response:

```text
Removed key 'username'.
```

### `PRINT`

Displays every key-value pair currently stored.

```text
PRINT
```

Example response:

```text
[KEY]: username    [VALUE]: brian
```

If the store is empty:

```text
Store is empty.
```

### `quit`

Disconnects the client from the server.

```text
quit
```

The server closes the client connection while remaining available for other clients.

## Persistence

The key-value store is persisted in `store.txt`.

Each record is saved as:

```text
key<TAB>value
```

For example:

```text
username    brian
language    Python
```

The file is loaded automatically when the server starts. Changes made through `SET` and `REMOVE` are written to disk immediately.

To reset the database, stop the server and delete `store.txt`:

```bash
rm store.txt
```

On Windows PowerShell:

```powershell
Remove-Item store.txt
```

The file will be recreated when new data is stored.

## Concurrency Model

The server uses a thread-per-client model:

1. The main server thread listens for incoming connections.
2. Each client connection is accepted by the server.
3. A new daemon thread handles that client.
4. All client threads share the same `KeyValueStore`.
5. A `threading.RLock` protects reads, updates, removals, and persistence operations.

This allows multiple clients to connect and issue commands concurrently while preventing unsafe simultaneous access to the shared dictionary and persistence file.

## Error Handling

The server reports errors for:

- Empty commands
- Unknown commands
- Missing command arguments
- Attempts to retrieve nonexistent keys
- Attempts to remove nonexistent keys
- Connection and socket failures
- Malformed persistence records

The server can be stopped gracefully with:

```text
Ctrl+C
```

## Troubleshooting

### The client cannot connect

Make sure the server is running first:

```bash
python server.py
```

Also verify that port `3490` is available and not blocked by a firewall.

### The port is already in use

Another process may already be using port `3490`. Stop that process or change the port in both `server.py` and `client.py`.

### Stored data disappeared

The server stores data in `store.txt` in the project directory. Make sure you are running the server from the repository directory and that the file has not been deleted or moved.

### The server does not respond

Check the server terminal for connection, command-processing, or socket errors. Also confirm that commands follow the documented format.

## Possible Future Improvements

- Support values containing spaces or quoted strings
- Add commands for clearing the entire store
- Add configurable host and port arguments
- Replace the text file with SQLite or another durable database
- Add request framing for larger messages
- Add automated unit and integration tests
- Add authentication and encrypted connections
- Use a thread pool instead of creating an unbounded thread per client
- Add structured logging

## License

This project is available for educational and personal use. Add a formal license if you intend to distribute or reuse it publicly.

---

Built with Python sockets, threads, and the standard library.
