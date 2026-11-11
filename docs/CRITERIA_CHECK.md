# Does the project meet the five functional criteria?

Method: static reading of the current working tree (branch `test`, including uncommitted edits to `client.py`, `http_ui.py`, `connection.py`) plus the unit-test suite (150 tests passed with `.winenv`). **No full live run** (server + browser clients) was done for this document, so "Met" below means "the code and tests support it", not "demonstrated end to end".

Legend: **Met** = code present and covered by tests. **Partly** = code present but a gap or untested path remains. **Gap** = missing.

| #   | Criterion                                       | Verdict                           |
| --- | ----------------------------------------------- | --------------------------------- |
| 1   | User authentication and discovery               | **Met** (not run live)            |
| 2   | Streaming-based messaging (broadcast + unicast) | **Met** (not run live)            |
| 3   | Reliable file transfer                          | **Partly**                        |
| 4   | Connection resilience                           | **Partly**                        |
| 5   | Secure data transmission                        | **Met**, with one design weakness |

---

## 1. User authentication and discovery: Met

Why:

- **Login:** the client sends `AUTH` (username + SHA-256 password hash) on stream 0 after the handshake (`ChatClient.connect`). The server checks it in `ChatSession` with `db.verify_user`, then calls `_on_auth_success` (`server.py:321`).
- **Presence:** after a successful login the server calls `broadcast_user_list()`, which sends `{"type":"USER_LIST","users":[...]}` to every authenticated session (`server.py:379-386`).
- **Leaving:** logout (`_on_auth_logout`) and closed sessions (the server `_tick_loop`, `server.py:479-531`) both broadcast a `SYSTEM` "has left" message and a fresh `USER_LIST`.
- **Duplicate login:** a second login with the same username kicks the old session (`KICKED`), so the user list has no duplicates.

Caveats:

- Passwords are hashed with plain unsalted SHA-256 (`client.py:117`). Fine for a course demo, weak in general.
- Auth only works after the handshake; the client now refuses to send AUTH without a session key.

## 2. Streaming-based messaging: Met

Why:

- **Broadcast:** `target == "all"` goes through `route_broadcast` -> `ChatServer._broadcast`, to every other authenticated session.
- **Unicast:** `target == "<user>"` goes through `route_unicast` -> `ChatServer._unicast`, to that user only; an offline target returns an `ERROR` to the sender.
- **Message-oriented stream design (QUIC/GNS style):** each message is an application message on a stream (`stream.py`), split into chunks by `offset` and reassembled with an EOM flag. Stream 0 carries chat/control JSON; each file gets its own stream. Client streams are odd, server streams even. Details: `docs/CHAT_PAYLOAD.md`.
- It is **not** the real QUIC or GameNetworkingSockets. The project reimplements the ideas (streams, connection ID, per-packet encryption, SACK, CUBIC) over raw UDP.

Caveat: the server does not echo a broadcast back to the sender, so the UI has to draw the sender's own message locally.

## 3. Reliable file transfer: Partly

What supports it:

- **Sender:** reads the file, computes SHA-256, announces `FILE` (name, size, sha256, target), then sends raw chunks on a dedicated stream (`start_file_transfer`).
- **Receiver:** `_handle_incoming_file_chunk` accumulates chunks, and when `received >= size` it recomputes SHA-256 and compares it with the announced hash. A mismatch raises `FILE_ERROR` and the file is not saved. The saved name uses `os.path.basename`, so a hostile filename cannot escape the downloads folder.
- **Transport reliability:** `reliability.py` provides SACK, RFC 6298 RTO and CUBIC, with in-order reassembly in `stream.py`. Tests cover a 10 MB transfer with reproducible loss (`test_integration.py:293`), big files with 5 % loss plus reordering and duplication (`test_connection_loopback.py:429`), and single-packet loss at 10 % (`:435`). These tests run at the `Connection` level.

Gaps and risks:

- **Server relay uses the sender's stream id.** `_route_file_chunk` writes to `get_or_create_stream(stream_id)` on each recipient's connection using the _sender's_ id. Client ids are odd and start at 1, so two senders transferring at once to one recipient can collide on the same stream id. This is the "G6" issue from plan 01 and is unfixed in this tree.
- **No end-to-end test.** No test drives `ChatClient` -> server -> `ChatClient` with a file. The loss tests above exercise only the transport, so "files of various formats and sizes without corruption" is supported by the pieces but **not demonstrated through the application path**.
- **Whole file in memory** on both sides (`b"".join(chunks)`), so very large files are limited by RAM.
- **Ordering:** if a file chunk reaches the receiver before the `FILE_READY` message that registers its stream, the chunk is not treated as file data (it falls to the JSON path). Behaviour in that race is untested.
- The 0-byte file case is handled specially (`client.py` saves it immediately).

## 4. Connection resilience: Partly

What supports it:

- **Heartbeat and idle timeout:** a heartbeat every 5 s and `IDLE_TIMEOUT = 30 s`; `Connection.tick()` marks the connection closed when nothing is received.
- **Retransmission limits:** a packet that exceeds max retransmits marks the connection closed (`take_failed`, `tick`).
- **Handshake loss:** HELLO is resent every 1 s, up to 5 times (`test_lost_hello_is_retransmitted`).
- **Client disappears abruptly:** the server's `_tick_loop` sees `closed`, removes the session and its address/user mappings, and tells everyone `@user has left` plus a new `USER_LIST`.
- **Explicit disconnect:** a `PKT_CLOSE` packet closes the session.
- **NAT rebinding:** the server accepts a new source address only after an encrypted packet from it verifies.
- **Corrupt packets** are dropped and not ACKed, so they are retransmitted.

Gaps:

- **No automatic reconnect.** When the client's connection closes it emits `{"type":"DISCONNECTED"}` (`client.py:281`), but nothing in `src/` handles that event, including `app.js`, so the user is not told and the UI does not recover on its own. The user must log in again.
- **Server crash is not handled gracefully** on the client beyond the 30 s idle timeout.
- No test kills a client mid-transfer or mid-chat and checks the server's cleanup and relay state.

## 5. Secure data transmission: Met, with one weakness

Why:

- **Key exchange:** X25519 in HELLO / HELLO_ACK, then HKDF, giving two directional AES-256-GCM keys (`crypto.py`, `Connection._install_keys`).
- **Payloads:** every DATA payload is AES-256-GCM encrypted in `Connection._transmit_data`. The 22-byte header is the AAD, so tampering with the header (stream id, offset, flags) is detected. Tests cover tampered payload, tag and header (`test_crypto.py`, `test_integration.py`).
- **Nonces:** derived from the sequence number and direction, with a reuse guard (`_used_nonces`).
- **No plaintext after the handshake:** the receiver drops unencrypted DATA once a key exists, and (after the fix made during this session) drops any DATA before a key exists. The sender refuses to send application messages before the handshake completes.
- **ACK only after decryption:** a forged or corrupted packet is not acknowledged.

Weakness:

- **The server's public key is not authenticated** (no certificate, no pinning), so an active man-in-the-middle could substitute keys during the handshake. This protects against passive sniffing, which is what the criterion asks for ("prevent packet-sniffing"), but not against an active attacker.
- Headers (type, conn_id, seq, stream_id, offset) are visible; only payloads are encrypted.

---

## Summary of what to do before claiming "all met"

1. Write an end-to-end test: server + 2-3 `ChatClient`s, covering login, broadcast, unicast, a 1 MB+ file with hash check, and two simultaneous file senders to one recipient (criterion 3, G6).
2. Fix the relay stream-id collision (M3, `server.py`).
3. Handle `DISCONNECTED` in the UI and decide whether to auto-reconnect (criterion 4).
4. Do a manual run with `run_all.py` and several browser clients, then record the result honestly in `PROGRESS.md`.
