# STREAMCHAT API Contract

What each module exposes and what it expects, so you can call it without reading its code. Signatures are copied from `feat/Jaden` (`d869b8c`). Anything not listed here is private (leading `_`) and may change without notice.

Rules for everyone (from `AGENTS.md`):

- A signature, header layout, flag value or message field listed here may not change silently. To change one: keep a backward-compatible default if possible, add an ADR in `docs/project/DECISIONS.md`, and tell the owners of the callers first.
- Imports assume `src/` is on `sys.path`: `from common.crypto import ...`, `from transport.stream import ...`.
- "Raises" lists what a caller must be ready to catch. A function that says "never raises" swallows bad input itself.

Layer map and owners:

| Module                                                                | Owner | Called by                              |
| :-------------------------------------------------------------------- | :---- | :------------------------------------- |
| `common/constants.py`, `common/packet.py`                             | M1    | everyone                               |
| `transport/connection.py`                                             | M1    | `client.py`, `server.py`, `session.py` |
| `common/crypto.py`, `transport/reliability.py`, `transport/stream.py` | M2    | `connection.py` only                   |
| `server/server.py`, `server/session.py`, `server/database.py`         | M3    | `run_server.py`                        |
| `client/client.py`, `client/http_ui.py`                               | M4    | `run_client.py`, browser               |

---

## 1. `common/constants.py` (M1)

Values only. Use the names, never the literals.

| Group        | Names                                                                                                                                                                                                  |
| :----------- | :----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Packet types | `PKT_HELLO=0x01`, `PKT_HELLO_ACK=0x02`, `PKT_DATA=0x03`, `PKT_ACK=0x04`, `PKT_HEARTBEAT=0x05`, `PKT_HEARTBEAT_ACK=0x06`, `PKT_CLOSE=0x07`, `PKT_AUTH_REQ=0x08` (unused), `PKT_AUTH_RESP=0x09` (unused) |
| Flags        | `FLAG_NONE=0x00`, `FLAG_EOM=0x01`, `FLAG_FIN=0x02`, `FLAG_SYN=0x04`, `FLAG_ENCRYPTED=0x08`                                                                                                             |
| Sizes        | `HEADER_SIZE=22`, `MAX_UDP_PAYLOAD=1400`, `RECV_BUFFER_SIZE=65535`, `GCM_NONCE_SIZE=12`, `GCM_TAG_SIZE=16`, `AES_KEY_SIZE=32`                                                                          |
| Timers       | `HEARTBEAT_INTERVAL=5.0`, `IDLE_TIMEOUT=30.0`, `TICK_INTERVAL=0.05`, `INITIAL_RTO=1.0`, `MIN_RTO=0.2`, `MAX_RTO=10.0`                                                                                  |
| CUBIC        | `CUBIC_C`, `CUBIC_BETA`, `INITIAL_CWND`, `MIN_CWND`, `MAX_CWND`, `SSTHRESH_INITIAL`                                                                                                                    |
| Defaults     | `DEFAULT_SERVER_HOST="127.0.0.1"`, `DEFAULT_SERVER_PORT=9000`, `DEFAULT_HTTP_PORT=8080`                                                                                                                |

Known mismatch: `constants.MAX_APP_PAYLOAD` is 1378, but the real per-chunk limit is `transport.stream.SAFE_MAX_APP_PAYLOAD = 1362` (1400 - 22 - 16). Never size a chunk from `MAX_APP_PAYLOAD`; let `Stream.write` do the chunking.

---

## 2. `common/packet.py` (M1)

```python
class Header(NamedTuple):
    msg_type: int      # PKT_*
    conn_id: int       # uint32
    seq_num: int       # uint32
    flags: int         # FLAG_* bitmask
    stream_id: int     # uint16
    offset: int        # uint32
    payload_len: int   # uint16
    # plus one reserved field, ignore it

def pack(msg_type: int, conn_id: int, seq_num: int, flags: int,
         stream_id: int, offset: int, payload: bytes) -> bytes
    # returns 22-byte header + payload. payload_len is filled for you.

def unpack(raw_bytes: bytes) -> tuple[Header, bytes]
    # raises ValueError if the datagram is shorter than 22 bytes or malformed.
```

Contract: the first 22 bytes of a datagram are the header and are also the AEAD associated data. For encrypted DATA, `payload_len` equals `len(plaintext) + 16`.

---

## 3. `transport/connection.py` (M1). The main API for client and server code

```python
class Connection:
    INTEGRATION_CONTRACT_VERSION: int = 1

    def __init__(self, sock: socket.socket, remote_addr: tuple[str, int],
                 conn_id: int = 0, is_server: bool = False,
                 on_app_message: Optional[Callable[[int, bytes], None]] = None) -> None
```

| Member                                                                    | Signature                       | Use it to                                                                                      |
| :------------------------------------------------------------------------ | :------------------------------ | :--------------------------------------------------------------------------------------------- |
| `conn_id`                                                                 | `int` attr                      | read the connection id (random if `0` passed)                                                  |
| `remote_addr`                                                             | `tuple[str,int]` attr, writable | where packets are sent; server updates it only after NAT-rebinding check                       |
| `closed`                                                                  | `bool` attr                     | `True` once idle timeout, too many retransmits, or `PKT_CLOSE`. Caller removes the connection. |
| `handshake_complete`                                                      | `threading.Event`               | `.wait(timeout)` before sending app data                                                       |
| `reliability`                                                             | `ReliabilityManager` attr       | read-only use: `can_send()`, `in_flight`                                                       |
| `handle_handshake()`                                                      | `-> None`                       | **client only.** Sends `HELLO`; completion is signalled by `handshake_complete`                |
| `receive_packet(raw_bytes: bytes)`                                        | `-> None`                       | feed every datagram that belongs to this connection. Never raises on bad input; drops it.      |
| `verify_migration_datagram(raw_bytes: bytes)`                             | `-> bool`                       | server: `True` only for a valid AEAD DATA packet; use before accepting a new source address    |
| `tick()`                                                                  | `-> None`                       | call every `TICK_INTERVAL` (0.05 s): heartbeat, HELLO retry, retransmits, idle check           |
| `get_or_create_stream(stream_id: int)`                                    | `-> Stream`                     | get the stream; use `0` for JSON chat/control                                                  |
| `allocate_stream()`                                                       | `-> Stream`                     | new stream id (odd on client, even on server)                                                  |
| `send_packet(packet_type, stream_id, payload, flags=FLAG_NONE, offset=0)` | `-> None`                       | low level; prefer `Stream.write` for app data. `PKT_DATA` is queued, never blocks              |

Callback: `on_app_message(stream_id: int, data: bytes)` fires once per complete message, possibly several times per datagram. It runs on the receive thread; do not block in it.

Usage rules:

1. Client: `handle_handshake()`, then `handshake_complete.wait(5.0)`. If it times out, do not send credentials (see ARCHITECTURE.md gap).
2. Send app data only with `connection.get_or_create_stream(sid).write(bytes)`.
3. Threading: `receive_packet` from one thread, `tick` from another is supported. `write` is thread-safe via the tx queue.
4. After `closed` is `True`, stop calling it and release the session.

---

## 4. `transport/stream.py` (M2)

```python
SAFE_MAX_APP_PAYLOAD = 1362
DEFAULT_MAX_STREAM_BUFFER_SIZE = 16 * 1024 * 1024
DEFAULT_MAX_BUFFERED_CHUNKS = 65536

class Stream:
    def __init__(self, stream_id: int,
                 send_callback: Optional[Callable[[int, int, bytes, int], None]] = None,
                 chunk_size: int = SAFE_MAX_APP_PAYLOAD,
                 max_buffer_size: int = DEFAULT_MAX_STREAM_BUFFER_SIZE,
                 max_buffered_chunks: int = DEFAULT_MAX_BUFFERED_CHUNKS) -> None

    stream_id: int                                   # attr

    def write(self, data: bytes) -> int
    # Splits data into <=1362 B chunks, assigns offsets, sets FLAG_EOM on the last,
    # calls send_callback(stream_id, offset, chunk, flags) per chunk. Returns bytes accepted.
    # Raises TypeError if data is not bytes/bytearray.

    def read_incoming(self, offset: int, data: bytes, is_eom: bool) -> Optional[bytes]
    # Called by Connection only. Raises TypeError (wrong types), ValueError (negative offset,
    # buffer cap, overlap conflict). A raise means: do not ACK that packet.

    def get_complete_messages(self) -> list[bytes]
    # Drains and returns every fully reassembled message, in order. Empty list if none.
```

Contract: one `write()` call = one message = one `on_app_message` call at the peer. Messages on one stream arrive in order; different streams are independent.

---

## 5. `transport/reliability.py` (M2). Used by `Connection` only

```python
class ReliabilityManager:
    def __init__(self) -> None
    def mark_sent(self, packet_num: int, packet_data: bytes) -> None   # TypeError/ValueError on bad args
    def process_sack(self, ack_ranges: list[tuple[int, int]]) -> None # inclusive (start,end); TypeError/ValueError on malformed, caller must catch
    def get_retransmits(self) -> list[bytes]                           # packets past RTO, ready to resend
    def update_rtt(self, sample_rtt: float) -> None                    # seconds, finite, >= 0
    def update_cwnd(self, is_loss: bool, acked_packets: int = 1) -> None
    def can_send(self) -> bool                                         # sender must check before every DATA send
    def take_failed(self) -> set[int]                                  # packet numbers that exceeded max retransmits
    @property
    def in_flight(self) -> int
```

Contract: SACK input is untrusted. At most one cwnd reduction per loss epoch. Consecutive timeouts double the RTO up to `MAX_RTO`.

---

## 6. `common/crypto.py` (M2). Used by `Connection` only

```python
def generate_keypair() -> tuple[X25519PrivateKey, bytes]              # (private, 32-byte public)
def derive_session_keys(private_key: X25519PrivateKey,
                        peer_public_bytes: bytes) -> tuple[bytes, bytes]
    # returns (client_write_key, server_write_key), each 32 bytes. ValueError on a bad public key.
def derive_shared_secret(private_key, peer_public_bytes,
                         salt: Optional[bytes] = None,
                         info: bytes = b"streaming-protocol-session-key") -> bytes   # legacy, non-directional
def encrypt(payload: bytes, session_key: bytes, packet_num: int,
            associated_data: Optional[bytes] = None,
            is_server: Optional[bool] = None) -> bytes                 # ciphertext + 16 B tag
def decrypt(ciphertext: bytes, session_key: bytes, packet_num: int,
            associated_data: Optional[bytes] = None,
            is_server: Optional[bool] = None) -> bytes                 # raises InvalidTag / ValueError / TypeError
def reset_nonce_tracker() -> None                                      # tests only
```

Contract: `is_server` is the role of the **sender**. When decrypting, pass the peer's role (`not self.is_server`). `associated_data` is the exact 22 header bytes. `encrypt` refuses to reuse a (key, packet_num, role) nonce. Never ACK before `decrypt` succeeds.

---

## 7. `server/server.py` (M3)

```python
class ChatServer:
    def __init__(self) -> None
    def start_server(self, host: str = DEFAULT_SERVER_HOST,
                     port: int = DEFAULT_SERVER_PORT) -> None   # blocks; runs the loop and tick thread
    def handle_datagram(self, data: bytes, addr: tuple[str, int]) -> None   # routes by conn_id; never raises to caller
    def get_online_users(self) -> list[str]                      # sorted, unique
    def broadcast_user_list(self) -> None
```

Callbacks given to `Session` (signatures `Session` relies on):

```python
_broadcast(sender: str, msg: dict[str, Any], exclude: Optional[str] = None) -> None
_unicast(sender: str, target_username: str, msg: dict[str, Any]) -> bool    # False if target offline
_route_file_chunk(sender: str, target_username: str, stream_id: int, data: bytes) -> bool
_on_auth_success(conn_id: int, username: str) -> None
_on_auth_logout(conn_id: int, username: str) -> None
```

Entry point: `python run_server.py [--host H] [--port P]`.

---

## 8. `server/session.py` (M3)

```python
class Session:
    def __init__(self, conn_id: int, client_addr: tuple[str, int], connection: Connection,
                 broadcast_fn=None, unicast_fn=None, on_auth_success=None,
                 on_auth_logout=None, file_chunk_fn=None) -> None
    conn_id: int
    client_addr: tuple[str, int]      # writable by ChatServer on verified NAT rebinding
    connection: Connection
    username: Optional[str]
    authenticated: bool

    def process_payload(self, app_msg: bytes) -> None            # stream 0 JSON; never raises
    def process_file_chunk(self, stream_id: int, data: bytes) -> None   # stream N raw bytes
    def verify_auth(self, username: str, password_hash: str) -> bool
    def send_kicked(self, reason: str = ...) -> None
    def route_broadcast(self, msg: dict) -> bool
    def route_unicast(self, target_username: str, msg: dict) -> bool
```

Limits enforced (reject with `ERROR` message): username 32 chars, chat text 10 000 chars, filename 255 chars, file chunk 48 KiB, app message 64 KiB, stream id 1..65535, password hash exactly 64 hex chars.

### `server/database.py`

```python
class UserDatabase:
    def __init__(self, db_path: Optional[str | Path] = None) -> None
    def get_password_hash(self, username: str) -> Optional[str]
    def verify_user(self, username: str, password_hash: str) -> bool
    def add_user(self, username: str, raw_password: str) -> bool   # False if exists
db: UserDatabase   # module-level instance used by Session
```

---

## 9. `client/client.py` (M4)

```python
class ChatClient:
    def __init__(self, server_ip: str = DEFAULT_SERVER_HOST,
                 server_port: int = DEFAULT_SERVER_PORT) -> None
    event_queue: queue.Queue[dict]        # UI reads events from here

    def connect(self, username: str, password: str) -> bool     # starts threads, sends AUTH; result arrives as AUTH_RESP event
    def send_chat(self, target: str, text: str) -> None         # target "all" or username
    def start_file_transfer(self, file_path: str, target: str = "all") -> Optional[int]   # stream id, None if file missing
    def get_file_progress(self) -> dict[int, dict]              # sid -> {filename,size,sent,status}
    def logout(self) -> None
    def disconnect(self) -> None
    @property is_authenticated -> bool
    @property username -> Optional[str]
```

### `client/http_ui.py`

```python
class HttpUIServer:
    def __init__(self, port: int = DEFAULT_HTTP_PORT, client: Optional[ChatClient] = None) -> None
    def set_client(self, client: ChatClient) -> None
    def start(self) -> None
    def stop(self) -> None
```

Entry point: `python run_client.py [--server-ip IP --server-port P --http-port P]`.

Browser-facing HTTP API (on `127.0.0.1:<http-port>`):

| Method and path     | Body                   | Result                            |
| :------------------ | :--------------------- | :-------------------------------- |
| `GET /events`       | none                   | SSE stream of `event_queue` items |
| `POST /api/login`   | `{username, password}` | starts `connect()`                |
| `POST /api/logout`  | none                   | `logout()`                        |
| `POST /api/send`    | `{target, text}`       | `send_chat()`                     |
| `POST /api/file`    | file bytes or metadata | `start_file_transfer()`           |
| `GET /api/status`   | none                   | connection/auth status            |
| `GET /api/progress` | none                   | `get_file_progress()`             |

---

## 10. Application message schema (JSON, UTF-8, stream 0)

All messages have a `type`. Server fills `from` and `timestamp`; a client must not set them.

| `type`                                      | Direction       | Fields                                                                                |
| :------------------------------------------ | :-------------- | :------------------------------------------------------------------------------------ |
| `AUTH`                                      | C to S          | `username: str`, `password_hash: str` (64 hex)                                        |
| `AUTH_RESP`                                 | S to C          | `success: bool`, `username: str`, `message: str`                                      |
| `LOGOUT` / `LOGOUT_RESP`                    | C to S / S to C | `LOGOUT_RESP`: `success: bool`                                                        |
| `MSG`                                       | C to S          | `target: str`, `text: str`                                                            |
| `MSG`                                       | S to C          | `from: str`, `target: str`, `text: str`, `timestamp: float`                           |
| `FILE`                                      | C to S          | `filename: str`, `size: int`, `sha256: str` (64 hex), `stream_id: int`, `target: str` |
| `FILE_READY`                                | S to C          | the `FILE` fields plus `from: str`, `download_url: str`, `timestamp: float`           |
| `SYSTEM`                                    | S to C          | `message: str`                                                                        |
| `ERROR`                                     | S to C          | `message: str`                                                                        |
| `KICKED`                                    | S to C          | `reason` text; the client is logged out                                               |
| `USER_LIST`  | S to C | `users: list[str]` (sorted online usernames), sent after any login or logout |
| `DISCONNECTED`, `FILE_COMPLETE`, `RAW_DATA` | local to UI     | produced by `ChatClient` on `event_queue` only; never on the wire                     |

File bytes are not JSON: after `FILE`, the sender writes raw bytes to `stream_id`; the server forwards them on the same stream id to the recipient. The receiver checks `sha256` itself.

---

## 11. Known gaps to be aware of when using the API

- `PKT_AUTH_REQ` and `PKT_AUTH_RESP` are defined but unused.
- `HttpUIServer` request/response body shapes for `/api/file` were not verified here.
- `src/server/web_server.py` is a legacy monolithic server that bypasses this whole stack and is not part of this contract.
