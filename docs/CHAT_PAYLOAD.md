# Chat message payload (broadcast and direct)

Source of truth: `src/client/client.py` (`send_chat`), `src/server/session.py` (`_process_msg`, `route_broadcast`, `route_unicast`), `src/server/server.py` (`_broadcast`, `_unicast`). Wire summary also in `docs/API_CONTRACT.md` (rows `MSG`).

## 1. Where the payload sits

A chat message is a UTF-8 JSON object carried on **Stream 0** (control/chat stream).

```
UDP datagram
├─ Header (22 B, plaintext, used as AES-GCM AAD)
│    msg_type=PKT_DATA(0x03)  conn_id  seq  flags(ENCRYPTED|EOM)  stream_id=0  offset  payload_len
└─ Payload = AES-256-GCM( JSON bytes ) || 16 B tag
```

- JSON is produced with `json.dumps(msg, ensure_ascii=False).encode("utf-8")`.
- If the JSON is longer than 1362 B, `Stream.write` splits it into chunks; the last one carries `FLAG_EOM`. The receiver reassembles by `offset` before parsing.
- Each chunk is encrypted separately with the directional key (client->server uses the client key, server->client the server key).

## 2. Client -> Server (`MSG`)

Built by `ChatClient.send_chat(target, text)`:

```json
{ "type": "MSG", "target": "all", "text": "hello everyone" }
```

| Field    | Type   | Rules (checked by the server)                                                                      |
| -------- | ------ | -------------------------------------------------------------------------------------------------- |
| `type`   | string | Always `"MSG"`                                                                                     |
| `target` | string | `"all"` = broadcast, otherwise a username. Trimmed and lower-cased; empty -> `"all"`; max 32 chars |
| `text`   | string | Non-empty after `strip()`, max 10 000 characters                                                   |

The sender's identity is **not** in this payload. The server takes it from the authenticated session, so a client cannot spoof `from`.

### Broadcast example

```json
{ "type": "MSG", "target": "all", "text": "hello everyone" }
```

### Direct (unicast) example

```json
{ "type": "MSG", "target": "bob", "text": "hi bob, only you can read this" }
```

## 3. Server -> Client (`MSG`)

`ChatSession._process_msg` rebuilds the message and adds the sender and a timestamp:

```json
{
  "type": "MSG",
  "from": "alice",
  "text": "hello everyone",
  "target": "all",
  "timestamp": 1731312000.123
}
```

| Field       | Type   | Meaning                                                  |
| ----------- | ------ | -------------------------------------------------------- |
| `type`      | string | `"MSG"`                                                  |
| `from`      | string | Authenticated username of the sender (set by the server) |
| `text`      | string | Message text, unchanged                                  |
| `target`    | string | `"all"` or the recipient username (normalised)           |
| `timestamp` | float  | Server time in seconds since epoch (`time.time()`)       |

There is no `is_private` field. A client decides a message is private when `target != "all"`.

## 4. Routing differences

|                              | Broadcast (`target == "all"`)                                                                            | Direct (`target == "<user>"`)                                                          |
| ---------------------------- | -------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------- |
| Server call                  | `route_broadcast` -> `ChatServer._broadcast`                                                             | `route_unicast` -> `ChatServer._unicast`                                               |
| Recipients                   | Every authenticated session **except the sender** (`exclude=sender`)                                     | Only the session of `<user>`                                                           |
| Encryption                   | Server encrypts once **per recipient** with that recipient's key, so each copy is a different ciphertext | Encrypted with the recipient's key only                                                |
| Sender echo                  | None from the server. The UI must show the sender's own message locally                                  | None, except when `target == sender`: the server echoes it back                        |
| Recipient offline or unknown | n/a                                                                                                      | Sender gets `{"type":"ERROR","message":"User '<user>' is offline or does not exist."}` |
| Third parties                | Receive it                                                                                               | Never receive it (isolation)                                                           |

## 5. Error replies (server -> sender)

```json
{ "type": "ERROR", "message": "You are not logged in." }
{ "type": "ERROR", "message": "Chat text must be a non-empty string within the size limit." }
{ "type": "ERROR", "message": "Target must be a string." }
{ "type": "ERROR", "message": "Target is too long." }
```

## 6. Flow

```
alice UI -> ChatClient.send_chat -> _send_json -> Stream.write -> Connection._transmit_data -> encrypt()  ==UDP==>
server: Connection._handle_data -> decrypt() -> Stream reassemble -> ChatSession._process_msg (adds from/timestamp)
        ├─ broadcast: ChatServer._broadcast  -> for each other session: Stream(0).write -> encrypt() with that user's key
        └─ direct:    ChatServer._unicast    -> target session: Stream(0).write -> encrypt() with that user's key
bob client: decrypt() -> ChatClient event queue {"type":"MSG","from":"alice",...} -> SSE -> browser
```

## 7. Byte breakdown and the JSON wrapper (example: 400-char ASCII chat to `all`)

The "JSON wrapper" is everything in the JSON string except the user's text: the `type`/`target`/`text` keys, their values other than the text, and the JSON punctuation. `json.dumps` writes a space after each `:` and `,`, and those count.

```
{"type": "MSG", "target": "all", "text": "<400 chars>"}
└──────────── wrapper (42) ───────────┘└─ text ─┘└┘ wrapper (2)
```

Sizes (computed with the same `json.dumps` call as the client, not captured from a live run):

```
482 B  UDP datagram                      (the "bytes=" value in the [debug] line)
|- 22 B  Header (plaintext, used as AAD)
`- 460 B payload on the wire             (header payload_len = 460)
   |- 444 B ciphertext (same length as the plaintext JSON, GCM adds no padding)
   |   `- 444 B JSON plaintext
   |        |- 400 B  user text
   |        `-  44 B  JSON wrapper        ("target": "all")
   `-  16 B  GCM authentication tag
```

- Wrapper length depends on the `target` value: a username of N characters instead of `all` adds `N - 3` bytes.
- Non-ASCII text (for example Vietnamese) takes more than one byte per character in UTF-8, so the text part grows.
- Server -> client adds `from` and `timestamp`, so the same message is about 494 B of JSON and about 532 B as a datagram. The timestamp length varies.

Header fields (`!BIIBHIHI`, big-endian, 22 B):

| Bytes | Field | Value for this packet |
|---|---|---|
| 0 | `msg_type` | `0x03` (DATA) |
| 1-4 | `conn_id` | session id |
| 5-8 | `seq` | packet sequence number (also the AES nonce input) |
| 9 | `flags` | `0x09` = ENCRYPTED (0x08) + EOM (0x01) |
| 10-11 | `stream_id` | `0` (chat stream) |
| 12-15 | `offset` | `0` (first chunk) |
| 16-17 | `payload_len` | `460` |
| 18-21 | `reserved` | `0` |

## 8. Not covered here

- File messages (`FILE`, `FILE_READY`, raw chunks on stream N): see `docs/API_CONTRACT.md`.
- This describes the code at the current working-tree state. It was read from the source, not captured from a live run.
