<h1>Key-Value Storage</h1>
This project implements a multi-threaded TCP server in Python that provides a persistent key-value store with a custom command protocol.

It supports multiple concurrent clients, ensures thread-safe operations using reentrant locks, and persists data to disk.

<H1>Functionalities</H1>

- Persistent Storage:
  - Key-value pairs are saved to a file (store.txt) so that data remains available even after the server restarts.

- Multi-threaded Client Handling:
  - Each client connection is handled in its own thread, allowing the server to manage multiple connections concurrently.

- Thread-safe Operations:
  - Uses threading.RLock to ensure safe access to the shared key-value store, even when nested operations occur.

- Custom Command Protocol:
  - Supports the following commands:

    - `SET <key> <value>`: Adds or updates a key-value pair.

    - `GET <key>`: Retrieves the value for a specified key.

    - `REMOVE <key>`: Deletes the key-value pair.

    - `PRINT`: Displays all key-value pairs.

    - `quit`: Disconnects the client gracefully.

- Error Handling and Shutdown:
  - The server includes robust error handling and allows for a clean shutdown when clients disconnect or when the server is interrupted.


<h1>User Guide</h1>
<h3>Prerequisites</h3>
- Python 3.x is required. The project uses only Python’s standard libraries (no additional dependencies).

<h3>Setup</h3>
- Clone the Repository:

- Open your terminal and run:
  ```
  git clone https://github.com/yourusername/your-repo-name.git
  cd your-repo-name
  ```

<h3>Running the application</h3>

1. Open a terminal in the project directory.

2. Run the server.
   ```
   python server.py
   ```

3. The server will start listening on port 3490 and display:
   ```
   Server listening on 0.0.0.0:3490
   ```

4. Open another terminal in the same project directory.
5. Run the client:
   ```
   python client.py
   ```

   The client will connect to the server and prompt:
   ```
   Connected to server at localhost:3490
   Enter command (or 'quit' to exit):
   ```

<h3>Using the application</h3>

Once the client is running, you can type commands into the console:

- `SET` Command:
  - Store a key-value pair.

  - Example:
    ```
    SET mykey myvalue
    ```
      - Expected Response:
        ```
        Added key 'mykey' with value 'myvalue'
        ```

- `GET` Command:
  - Retrieve the value for a specific key.

  - Example:
    ```
    GET mykey
    ```
      - Expected Response:
        ```
        myvalue
        ```

- `REMOVE` Command:
  - Delete a key-value pair.

  - Example:
    ```
    REMOVE mykey
    ```
      - Expected Response:
        ```
        Removed key 'mykey'.
        ```

- `PRINT` Command:
  - Display all stored key-value pairs.

  - Example:
    ```
    PRINT
    ```
      - Expected Response:
        A list of all key-value pairs or "Store is empty." if no data exists.

- `quit` Command:
  - Disconnect the client.

  - Example:
    ```
    quit
    ```
      - Expected Response:
        ```
        Exiting client.
        Connection closed.
        ```

<h3>Troubleshooting</h3>

**No Response on Client:**
Ensure that the server is running. Check the server’s output for debug messages and confirm that the command is being processed.

**Port Issues:**
Verify that port 3490 is available and not blocked by any firewall or security settings.

**Data Persistence:**
The data is stored in store.txt in the project directory. If you want a fresh start, you can delete this file.

<br/>
<h3>Additional Information</h3>

**Shutdown:**
The server can be stopped by pressing Ctrl+C in the terminal running the server.

<br/>
Happy coding!
