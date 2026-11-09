# STREAMCHAT Sequence Diagrams (with function and file locations)

Every arrow in Part 1 names the real function that runs, as `file:line Class.function()`, plus the owner (M1..M4). Line numbers are for branch `feat/Jaden`, commit `d869b8c`; if they drift, search the function name. Public signatures are in [`API_CONTRACT.md`](API_CONTRACT.md); the design is in [`ARCHITECTURE.md`](ARCHITECTURE.md).

Part 2 is the **proposed** secure design (T1..T11). Its functions **do not exist yet**: each is marked `NEW` with the file that should own it and the existing function it hooks into. Names there are suggestions, not agreed API.

## 0. How to read this

Participants are named by file so you can open the right one:

| Alias     | File                                                                        | Owner |
| :-------- | :-------------------------------------------------------------------------- | :---- |
| `http_ui` | `src/client/http_ui.py` `HttpUIServer`                                      | M4    |
| `client`  | `src/client/client.py` `ChatClient`                                         | M4    |
| `cconn`   | `src/transport/connection.py` `Connection` (client side, `is_server=False`) | M1    |
| `server`  | `src/server/server.py` `ChatServer`                                         | M3    |
| `session` | `src/server/session.py` `Session`                                           | M3    |
| `sconn`   | `src/transport/connection.py` `Connection` (server side, `is_server=True`)  | M1    |
| `stream`  | `src/transport/stream.py` `Stream`                                          | M2    |
| `rel`     | `src/transport/reliability.py` `ReliabilityManager`                         | M2    |
| `crypto`  | `src/common/crypto.py`                                                      | M2    |
| `db`      | `src/server/database.py` `db.verify_user`                                   | M3    |

`cconn` and `sconn` are the same class. Where the code is identical on both sides the diagram says "either side".

### Quick index: "I need to do X, which function do I call?"

| I want to                                  | Call                                                       | File:line                           | Owner |
| :----------------------------------------- | :--------------------------------------------------------- | :---------------------------------- | :---- |
| Start a connection and handshake (client)  | `Connection.handle_handshake()`                            | `connection.py:465`                 | M1    |
| Feed a received datagram into a connection | `Connection.receive_packet(raw)`                           | `connection.py:292`                 | M1    |
| Run timers (heartbeat, retransmit, idle)   | `Connection.tick()` every 0.05 s                           | `connection.py:613`                 | M1    |
| Send a JSON message                        | `Stream(0).write(bytes)` via `get_or_create_stream(0)`     | `stream.py:76`, `connection.py:144` | M2/M1 |
| Send a file                                | `Connection.allocate_stream()` then `Stream.write()`       | `connection.py:162`, `stream.py:76` | M1/M2 |
| Receive a complete message                 | set `on_app_message(stream_id, data)` in `Connection(...)` | `connection.py:70`                  | M1    |
| Encrypt / decrypt one packet               | `crypto.encrypt()` / `crypto.decrypt()`                    | `crypto.py:164`, `:219`             | M2    |
| Ask "may I send another packet?"           | `ReliabilityManager.can_send()`                            | `reliability.py:340`                | M2    |
| Send a chat message from the UI layer      | `ChatClient.send_chat(target, text)`                       | `client.py:121`                     | M4    |
| Start a file transfer from the UI layer    | `ChatClient.start_file_transfer(path, target)`             | `client.py:137`                     | M4    |
| Log a user in / out (client)               | `ChatClient.connect()` / `.logout()`                       | `client.py:80`, `:329`              | M4    |
| Send a server message to one client        | `Session._send_app_message(dict)`                          | `session.py:568`                    | M3    |
| Route to everyone / to one user            | `Session.route_broadcast()` / `route_unicast()`            | `session.py:494`, `:516`            | M3    |
| Check credentials                          | `db.verify_user(username, hash)`                           | `database.py:94`                    | M3    |

---

## Part 1: Current implementation

### 1.1 Handshake and login

```mermaid
sequenceDiagram
    autonumber
    participant B as Browser
    participant UI as http_ui.py
    participant CL as client.py
    participant CC as connection.py (client)
    participant SV as server.py
    participant SS as session.py
    participant SC as connection.py (server)
    participant DB as database.py

    B->>UI: POST /api/login {username, password}
    Note over UI: http_ui.py:110 handle_http_request routes to<br/>http_ui.py:335 _handle_login
    UI->>CL: client.py:80 ChatClient.connect(username, password)
    CL->>CC: connection.py:465 Connection.handle_handshake()
    Note over CC: crypto.py:91 generate_keypair()
    CC->>SV: HELLO {public_key, conn_id} (clear) via _raw_send
    Note over SV: server.py:105 _read_datagrams to<br/>server.py:126 handle_datagram(data, addr)
    SV->>SV: server.py:221 _create_session(conn_id, addr)<br/>builds Connection(is_server=True) and Session(...)
    SV->>SC: connection.py:292 receive_packet(raw)
    SC->>SC: connection.py:500 _handle_hello()
    Note over SC: crypto.py:91 generate_keypair()<br/>connection.py:491 _install_keys()<br/>which calls crypto.py:145 derive_session_keys()
    SC-->>CC: HELLO_ACK {public_key} (clear), cached in _hello_ack_raw
    Note over CC: client.py:237 poll_network to<br/>connection.py:292 receive_packet to<br/>connection.py:550 _handle_hello_ack()<br/>then _install_keys() and handshake_complete.set()
    opt HELLO_ACK lost
        Note over CC: connection.py:613 tick() resends HELLO<br/>(max 5, every 1 s). Server replays cached HELLO_ACK.
    end
    CL->>CL: handshake_complete.wait(5.0)
    CL->>CL: client.py:302 _send_json({type: AUTH, username, password_hash})
    CL->>CC: stream.py:76 Stream(0).write(bytes)
    CC->>CC: connection.py:178 _stream_send_callback to :193 send_packet<br/>to :230 _flush_send_queue (asks rel.can_send())<br/>to :241 _transmit_data
    Note over CC: crypto.py:164 encrypt(payload, send_key, seq, associated_data=header)<br/>reliability.py:84 mark_sent(seq, raw)<br/>connection.py:279 _raw_send
    CC->>SV: DATA stream 0 (AES-GCM)
    SV->>SC: handle_datagram to receive_packet
    SC->>SC: connection.py:387 _handle_data()
    Note over SC: crypto.py:219 decrypt() must succeed first<br/>stream.py:118 read_incoming()<br/>connection.py:443 _send_ack(seq)<br/>stream.py:266 get_complete_messages()
    SC->>SV: on_app_message(0, bytes)
    Note over SV: server.py:259 _on_app_message routes<br/>stream 0 to session.process_payload
    SV->>SS: session.py:81 Session.process_payload(bytes)
    SS->>SS: session.py:141 _process_auth(msg) validates fields
    SS->>DB: session.py:196 verify_auth to database.py:94 db.verify_user()
    DB-->>SS: True / False
    SS->>SC: session.py:568 _send_app_message(AUTH_RESP)<br/>to Stream(0).write to encrypted DATA
    opt success and was not authenticated before
        SS->>SV: server.py:321 _on_auth_success(conn_id, username)
        Note over SV: kicks older session of same user via<br/>session.py:559 send_kicked()
        SV->>SV: server.py:375 broadcast_user_list()<br/>to server.py:386 _broadcast("SYSTEM", {type: USER_LIST, users})
    end
    SC-->>CC: DATA AUTH_RESP
    Note over CL: client.py:266 _on_app_message sets _authenticated<br/>and puts the dict on event_queue
    CL-->>UI: event_queue, then http_ui.py:288 _sse_pump to :302 _broadcast_sse
    UI-->>B: SSE event
```

### 1.2 One reliable encrypted DATA packet (either side)

This is the path every message and file chunk takes. Written once; sender and receiver are both `connection.py`.

```mermaid
sequenceDiagram
    autonumber
    participant ST as stream.py (sender)
    participant S as connection.py (sender)
    participant R as reliability.py (sender)
    participant K as crypto.py
    participant N as UDP
    participant P as connection.py (receiver)
    participant RS as stream.py (receiver)

    ST->>S: Stream.write(data) chunks to _stream_send_callback<br/>to connection.py:193 send_packet(PKT_DATA, ...)
    S->>S: appends to _tx_queue, then connection.py:230 _flush_send_queue()
    S->>R: reliability.py:340 can_send()
    R-->>S: True (cwnd has room)
    S->>S: connection.py:241 _transmit_data(): _alloc_seq(), build header
    S->>K: crypto.py:164 encrypt(payload, send_key, seq, associated_data=22 B header, is_server=self.is_server)
    S->>R: reliability.py:84 mark_sent(seq, raw)
    S->>N: connection.py:279 _raw_send(raw)
    N->>P: datagram
    P->>P: connection.py:292 receive_packet() to :387 _handle_data()
    P->>K: crypto.py:219 decrypt(ct, recv_key, seq, associated_data=header, is_server=not self.is_server)
    alt InvalidTag, or unencrypted DATA after handshake
        P->>P: drop, no ACK
    else authentic
        P->>RS: stream.py:118 read_incoming(offset, data, is_eom)
        alt ValueError (overflow or overlap)
            P->>P: drop, no ACK, sender will retransmit
        else accepted
            P->>N: connection.py:443 _send_ack(seq) (PKT_ACK, clear)
            P->>RS: stream.py:266 get_complete_messages()
            P->>P: on_app_message(stream_id, msg) for each message
        end
    end
    N->>S: PKT_ACK
    S->>S: connection.py:364 _handle_ack(payload)
    S->>R: reliability.py:108 process_sack(ranges)
    Note over R: updates RTT (:227), cwnd (:257), may fast-retransmit
    S->>S: _flush_send_queue()
    loop every 0.05 s from the owner's tick thread
        S->>R: connection.py:613 tick() calls reliability.py:173 get_retransmits()
        S->>N: resend packets past RTO
        S->>R: reliability.py:349 take_failed(), if non-empty then closed = True
    end
```

Who calls `tick()`: the client from `client.py:254 ChatClient._tick_loop`; the server from `server.py:475 ChatServer._tick_loop`, which also removes sessions whose `connection.closed` is set.

### 1.3 Chat message (broadcast and unicast)

```mermaid
sequenceDiagram
    autonumber
    participant B1 as Browser (Alice)
    participant UI1 as http_ui.py
    participant CL1 as client.py
    participant SS1 as session.py (Alice)
    participant SV as server.py
    participant SS2 as session.py (Bob)

    B1->>UI1: POST /api/send {target, text}
    Note over UI1: http_ui.py:365 _handle_send
    UI1->>CL1: client.py:121 ChatClient.send_chat(target, text)
    CL1->>CL1: client.py:302 _send_json({type: MSG, target, text})<br/>to Stream(0).write (see 1.2)
    Note over SS1: server receives via 1.2, then<br/>server.py:259 _on_app_message to session.py:81 process_payload
    SS1->>SS1: session.py:254 _process_msg(msg)<br/>requires authenticated, validates target (:471) and text length
    Note over SS1: builds {type: MSG, from, text, target, timestamp}
    alt target == all
        SS1->>SV: session.py:494 route_broadcast(msg)<br/>calls server.py:386 ChatServer._broadcast(sender, msg, exclude)
    else target == a username
        SS1->>SV: session.py:516 route_unicast(target, msg)<br/>calls server.py:427 ChatServer._unicast(sender, target, msg)
        Note over SV: looks up _user_to_conn[target], returns False if offline
    end
    SV->>SS2: Session._send_app_message(msg) at session.py:568
    SS2->>SS2: Stream(0).write(json) to encrypted DATA (see 1.2)
    Note over SS2: Bob's client.py:266 _on_app_message to event_queue to SSE
```

### 1.4 File transfer

```mermaid
sequenceDiagram
    autonumber
    participant B1 as Browser (Alice)
    participant UI1 as http_ui.py
    participant CL1 as client.py
    participant CC1 as connection.py (Alice)
    participant SS1 as session.py (Alice)
    participant SV as server.py
    participant SS2 as session.py (Bob)
    participant CL2 as client.py (Bob)

    B1->>UI1: POST /api/file (binary)
    Note over UI1: http_ui.py:430 _handle_file_stream_upload<br/>saves to temp file, then calls client
    UI1->>CL1: client.py:137 ChatClient.start_file_transfer(path, target)
    CL1->>CL1: sha256 of file
    CL1->>CC1: connection.py:162 allocate_stream() returns odd stream id N
    CL1->>CL1: client.py:302 _send_json({type: FILE, filename, size, sha256, stream_id: N, target})
    CL1->>CL1: starts thread client.py:195 _transfer_file_data
    Note over SS1: session.py:81 process_payload to :290 _process_file<br/>validates filename, size, sha256, stream id
    SS1->>SV: route_broadcast / route_unicast with {type: FILE_READY, ...}
    SV-->>CL2: FILE_READY (Bob's client.py:266 _on_app_message)
    Note over SS1: stores _file_transfers[N] = (target, size, 0)
    loop until file end
        CL1->>CC1: Stream(N).write(chunk) (4096 B reads, split to 1362 B) and can_send() back-pressure
        CC1->>SS1: DATA on stream N (see 1.2)
        Note over SS1: server.py:259 _on_app_message routes stream N to<br/>session.py:420 process_file_chunk(N, data)<br/>checks running total against declared size
        SS1->>SV: file_chunk_fn is server.py:278 _route_file_chunk(sender, target, N, data)
        SV->>SS2: session.connection.get_or_create_stream(N).write(data)
        SS2-->>CL2: DATA on stream N (own seq, own ACKs)
    end
    CL2->>CL2: reassembly in stream.py:266 get_complete_messages()
    Note over CL2: receiver compares SHA-256 with the FILE_READY value
```

### 1.5 Logout, heartbeat, idle and close

```mermaid
sequenceDiagram
    autonumber
    participant CL as client.py
    participant CC as connection.py (client)
    participant SC as connection.py (server)
    participant SS as session.py
    participant SV as server.py

    CL->>CL: client.py:329 ChatClient.logout() sends {type: LOGOUT}
    SS->>SS: session.py:237 _process_logout() clears username
    SS-->>CL: LOGOUT_RESP via _send_app_message
    SS->>SV: server.py:352 _on_auth_logout(conn_id, username)
    SV->>SV: _broadcast SYSTEM "left" then broadcast_user_list()
    Note over CC,SC: transport and keys stay open after logout
    loop every 5 s
        CC->>SC: connection.py:584 _send_heartbeat() (PKT_HEARTBEAT, clear)
        SC-->>CC: connection.py:598 _send_heartbeat_ack()
    end
    alt no packet for 30 s, or a packet exceeds max retransmits
        Note over CC,SC: connection.py:613 tick() sets closed = True
        Note over CL: client.py:254 _tick_loop emits DISCONNECTED
        Note over SV: server.py:475 _tick_loop removes the session
    else PKT_CLOSE received
        Note over SV: server.py:212 in handle_datagram sets connection.closed = True<br/>(currently unauthenticated, see ARCHITECTURE.md section 5)
    end
```

---

## Part 2: Target flow with full security (PROPOSED)

`NEW` = does not exist. `HOOK` = existing function that would call it. Names are placeholders; agree them in an ADR before coding. T-numbers match `ARCHITECTURE.md` section 6.

### 2.1 Proposed function map

| T   | New or changed function                                                                                                                 | File                            | Hooks into                                                      | Owner  |
| :-- | :-------------------------------------------------------------------------------------------------------------------------------------- | :------------------------------ | :-------------------------------------------------------------- | :----- |
| T1  | `NEW crypto.sign_transcript(identity_priv, transcript) -> bytes`, `NEW crypto.verify_transcript(identity_pub, transcript, sig) -> bool` | `crypto.py`                     | `Connection._handle_hello` (sign), `_handle_hello_ack` (verify) | M2     |
| T1  | `NEW` server identity key load, and pinned key in client config                                                                         | `server.py`, `client.py`        | `ChatServer.__init__`, `ChatClient.__init__`                    | M3, M4 |
| T2  | `NEW crypto.derive_session_keys(priv, peer_pub, transcript_hash)`: add optional `transcript_hash: bytes = b""` (backward compatible)    | `crypto.py`                     | `Connection._install_keys`                                      | M2     |
| T2  | `NEW Connection._send_finished()`, `_handle_finished()`                                                                                 | `connection.py`                 | after `_install_keys`                                           | M1     |
| T3  | `NEW ChatServer._make_cookie(addr, conn_id)`, `_check_cookie(...)`; reply `PKT_RETRY` before `_create_session`                          | `server.py`, `constants.py`     | `ChatServer.handle_datagram`                                    | M3, M1 |
| T4  | `NEW Connection._send_authenticated_control(msg_type)` and verification in `receive_packet` for `PKT_ACK/HEARTBEAT*/CLOSE`              | `connection.py`                 | `_send_ack`, `_send_heartbeat*`, `receive_packet`               | M1     |
| T5  | `ChatClient.connect`: replace the unencrypted fallback with an error event                                                              | `client.py`                     | `client.py:106`                                                 | M4     |
| T5  | `Session._process_auth`: reject unless `connection.handshake_complete` and keys installed                                               | `session.py`                    | `session.py:141`                                                | M3     |
| T6  | `NEW UserDatabase.get_auth_params(username)`, `verify_proof(username, nonce, proof)`                                                    | `database.py`                   | `Session.verify_auth`                                           | M3     |
| T6  | `NEW Session._process_auth_begin()`, `_process_auth_proof()`; client side `ChatClient._answer_challenge()`                              | `session.py`, `client.py`       | `process_payload` dispatch, `_on_app_message`                   | M3, M4 |
| T7  | `NEW ReplayWindow.check_and_update(seq) -> bool`                                                                                        | `reliability.py` or `crypto.py` | `Connection._handle_data` after decrypt                         | M2     |
| T8  | `NEW Connection._start_rekey()`, `_handle_rekey()`                                                                                      | `connection.py`                 | `tick()`, `_handle_data`                                        | M1     |
| T9  | `NEW RateLimiter.allow(key) -> bool`                                                                                                    | `server.py`                     | `handle_datagram`, `_process_auth`                              | M3     |
| T10 | token and Origin check in `HttpUIServer.handle_http_request`                                                                            | `http_ui.py`                    | `http_ui.py:110`                                                | M4     |
| T11 | surface SHA-256 mismatch as an `ERROR` event                                                                                            | `client.py`                     | file receive path                                               | M4     |

### 2.2 Authenticated handshake with cookie and key confirmation

```mermaid
sequenceDiagram
    autonumber
    participant CC as connection.py (client)
    participant SV as server.py
    participant SC as connection.py (server)
    participant K as crypto.py

    Note over CC: pinned server_identity_pub from client config (T1)
    CC->>SV: HELLO {client_eph_pub, conn_id} via handle_handshake() :465
    SV->>SV: handle_datagram :126, NEW _check_cookie fails, no Session created (T3)
    SV-->>CC: NEW PKT_RETRY {cookie = HMAC(secret, addr, conn_id, time)} (T3)
    CC->>SV: HELLO {client_eph_pub, conn_id, cookie}
    SV->>SV: NEW RateLimiter.allow(addr) (T9), cookie ok, _create_session :221
    SV->>SC: receive_packet :292 to _handle_hello :500
    SC->>K: transcript = H(HELLO fields, server_eph_pub), NEW sign_transcript (T1)
    SC->>K: derive_session_keys(priv, peer_pub, transcript_hash) (T2)
    SC-->>CC: HELLO_ACK {server_eph_pub, signature}
    CC->>K: NEW verify_transcript(pinned_pub, transcript, sig) in _handle_hello_ack :550 (T1)
    alt signature invalid
        CC->>CC: abort, DISCONNECTED event, never send AUTH (T1, T5)
    else valid
        CC->>SC: NEW _send_finished(): AEAD DATA {MAC(transcript)} (T2)
        SC->>SC: NEW _handle_finished(), verify, handshake_complete.set()
        SC-->>CC: NEW Finished (AEAD)
    end
```

### 2.3 Login with challenge and salted storage

```mermaid
sequenceDiagram
    autonumber
    participant B as Browser
    participant UI as http_ui.py
    participant CL as client.py
    participant SS as session.py
    participant DB as database.py

    B->>UI: POST /api/login + local UI token (T10)
    UI->>UI: handle_http_request :110 checks Origin and token (T10)
    UI->>CL: ChatClient.connect() :80, handshake must be complete else error (T5)
    CL->>SS: AEAD DATA {type: AUTH_BEGIN, username}
    SS->>SS: NEW _process_auth_begin(), RateLimiter.allow (T9)
    SS->>DB: NEW get_auth_params(username), dummy salt if unknown (T6)
    SS-->>CL: AEAD DATA {server_nonce, salt, kdf_params}
    CL->>CL: NEW _answer_challenge(): k = scrypt(password, salt), proof = HMAC(k, nonce + transcript) (T6)
    CL->>SS: AEAD DATA {type: AUTH_PROOF, proof}
    SS->>SS: NEW _process_auth_proof() to DB.verify_proof(), constant-time compare
    SS-->>CL: AEAD DATA AUTH_RESP (existing _send_app_message :568)
    Note over SS: a captured proof is bound to this nonce and session, so it cannot be replayed
```

### 2.4 Protected data and control path

```mermaid
sequenceDiagram
    autonumber
    participant A as connection.py (sender)
    participant P as connection.py (receiver)

    A->>P: DATA via _transmit_data :241 (AEAD, header as AAD)
    P->>P: _handle_data :387, decrypt(), then NEW ReplayWindow.check_and_update(seq) (T7)
    P-->>A: ACK via NEW _send_authenticated_control (AEAD, header as AAD) (T4)
    loop heartbeat from tick() :613
        A->>P: HEARTBEAT as AEAD(empty) (T4)
        P-->>A: HEARTBEAT_ACK as AEAD(empty) (T4)
    end
    A->>P: CLOSE as AEAD(empty) (T4)
    P->>P: receive_packet verifies tag before setting closed (T4)
    Note over A,P: forged ACK, HEARTBEAT or CLOSE fails the tag check and is dropped
    opt rekey limit reached, checked in tick() (T8)
        A->>P: NEW _start_rekey(): AEAD {new_eph_pub}
        P-->>A: NEW _handle_rekey(): AEAD {new_eph_pub}
        Note over A,P: new keys = HKDF(old key, new DH), nonce counters restart
    end
    Note over A,P: T11: the receiver of a file compares SHA-256 and shows an ERROR on mismatch
```
