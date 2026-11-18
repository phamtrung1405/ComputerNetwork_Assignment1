# StreamChat — Sequence Diagram mức hàm (function-level)

Tài liệu này mô tả luồng gọi **giữa các hàm do nhóm tự viết** (không vẽ hàm built-in của Python như `json.dumps`, `struct.pack`, `socket.sendto`...). Mỗi mũi tên ghi **tên hàm(đầu vào)**, mũi tên trả về ghi **đầu ra**; các `Note` mô tả **cấu trúc dữ liệu**.

> Xem bằng VS Code (extension _Markdown Preview Mermaid Support_), GitHub, hoặc dán từng khối vào <https://mermaid.live>.
> Số dòng tham chiếu theo code ở thời điểm viết. File nguồn: `src/common`, `src/transport`, `src/server`, `src/client`.

## Mục lục

0. [Từ điển cấu trúc dữ liệu](#0-từ-điển-cấu-trúc-dữ-liệu)
1. [Bắt tay mã hóa (HELLO / HELLO_ACK, X25519 + HKDF)](#1-bắt-tay-mã-hóa)
2. [Đăng nhập (Browser → HttpUI → ChatClient → Server → SQLite)](#2-đăng-nhập)
3. [Tầng truyền tải — GỬI một gói DATA](#3-gửi-một-gói-data)
4. [Tầng truyền tải — NHẬN một gói DATA](#4-nhận-một-gói-data)
5. [ACK / SACK / RTT / CUBIC](#5-ack--sack--rtt--cubic)
6. [Phát hiện mất gói & truyền lại (tick)](#6-phát-hiện-mất-gói--truyền-lại)
7. [Chat phòng chung & chat riêng](#7-chat-phòng-chung--chat-riêng)
8. [Gửi file qua server](#8-gửi-file-qua-server)
9. [Heartbeat, timeout, dọn session, NAT rebinding](#9-heartbeat-timeout-dọn-session-nat-rebinding)
10. [Logout & Kick (đăng nhập nơi khác)](#10-logout--kick)
11. [Ghi chú về file không nằm trong luồng](#11-ghi-chú)

---

## 0. Từ điển cấu trúc dữ liệu

### 0.1 Header gói tin 22 byte — `common/packet.py` (`!BIIBHIHI`, big-endian)

| Trường        | Kích thước | Ý nghĩa                                                                          |
| ------------- | ---------- | -------------------------------------------------------------------------------- |
| `msg_type`    | 1B         | `PKT_HELLO=1, HELLO_ACK=2, DATA=3, ACK=4, HEARTBEAT=5, HEARTBEAT_ACK=6, CLOSE=7` |
| `conn_id`     | 4B         | ID kết nối (client random 1..2³²-1; server dùng lại đúng ID của client)          |
| `seq_num`     | 4B         | Số thứ tự gói (chỉ DATA mới tăng, cấp bởi `_alloc_seq`)                          |
| `flags`       | 1B         | `EOM=0x01` (chunk cuối của 1 message), `ENCRYPTED=0x08`                          |
| `stream_id`   | 2B         | 0 = kênh điều khiển/JSON. Client cấp ID **lẻ** (1,3,5…), server **chẵn**         |
| `offset`      | 4B         | Vị trí byte của chunk trong stream                                               |
| `payload_len` | 2B         | Độ dài payload (với gói mã hóa = plaintext + 16B tag)                            |
| `reserved`    | 4B         | = 0                                                                              |

`Header` = `NamedTuple` cùng 8 trường trên. `unpack()` trả `(Header, payload_bytes)`.

### 0.2 Các payload quan trọng

| Tên                               | Cấu trúc                                                                                                                                         | Nơi tạo                                              |
| --------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------- |
| HELLO / HELLO_ACK                 | JSON `{"public_key": "<hex 64 ký tự>", "conn_id": int}`                                                                                          | `handle_handshake`, `_handle_hello`                  |
| ACK (SACK)                        | JSON `{"ranges": [[seq, seq]]}` (hiện mỗi ACK chỉ 1 dải, `start==end`)                                                                           | `_send_ack`                                          |
| Tin nhắn ứng dụng                 | JSON UTF-8 có trường `type`: `AUTH`, `AUTH_RESP`, `MSG`, `FILE`, `FILE_READY`, `USER_LIST`, `SYSTEM`, `ERROR`, `LOGOUT`, `LOGOUT_RESP`, `KICKED` | `ChatClient._send_json`, `Session._send_app_message` |
| `AUTH`                            | `{"type":"AUTH","username":str,"password_hash":sha256_hex(64)}`                                                                                  | `ChatClient.connect`                                 |
| `MSG` (client→server)             | `{"type":"MSG","target":"all"\|username,"text":str}`                                                                                             | `ChatClient.send_chat`                               |
| `MSG` (server→client)             | thêm `"from":str,"timestamp":float`                                                                                                              | `Session._process_msg`                               |
| `FILE` (client→server)            | `{"type":"FILE","filename","size":int,"sha256","stream_id":int,"target"}`                                                                        | `ChatClient.start_file_transfer`                     |
| `FILE_READY` (server→người nhận)  | `FILE` + `"from"`, `"download_url"`, `"timestamp"`                                                                                               | `Session._process_file`                              |
| Chunk file                        | **bytes thô** (không JSON) trên `stream_id ≠ 0`, mỗi message ≤ 4096B                                                                             | `ChatClient._transfer_file_data`                     |
| Hàng đợi gửi `_tx_queue`          | `deque[(stream_id:int, payload:bytes, flags:int, offset:int)]`                                                                                   | `Connection.send_packet`                             |
| `_InFlightPacket`                 | `dataclass(packet_num, raw_data:bytes, sent_time:float, retransmit_count:int, fr_mark:int)`                                                      | `ReliabilityManager.mark_sent`                       |
| `ReliabilityManager._unacked`     | `dict[seq:int → _InFlightPacket]` (các gói đang bay)                                                                                             |                                                      |
| `Stream._recv_buffer`             | `dict[offset:int → chunk:bytes]` + `_sorted_offsets:list[int]` + `_eom_offsets:set[int]`                                                         | `Stream.read_incoming`                               |
| `ChatServer._sessions`            | `dict[conn_id → Session]`; `_addr_to_conn: dict[(ip,port) → conn_id]`; `_user_to_conn: dict[username → conn_id]`                                 |                                                      |
| `Session._file_transfers`         | `dict[stream_id → (target:str, expected_size:int, received_size:int)]`                                                                           | `_process_file`                                      |
| `ChatClient._incoming_files[sid]` | `{filename,size,sha256,from,target,received,chunks:list[bytes],file_path,status,download_url}`                                                   |                                                      |
| `ChatClient.event_queue`          | `queue.Queue[dict]` – mỗi phần tử là 1 event JSON gửi cho UI qua SSE                                                                             |                                                      |

### 0.3 Khóa mật mã (sau handshake)

`derive_session_keys()` trả `(client_key, server_key)` 32B mỗi khóa. Client gửi bằng `client_key`, nhận bằng `server_key`; server làm ngược lại (`_install_keys`). **Nonce AES-GCM 12B** = `[1 byte vai trò: 0x00 client / 0x01 server] + [11 byte seq_num big-endian]`. **AAD** = 22 byte header.

---

## 1. Bắt tay mã hóa

Hàm: `Connection.handle_handshake`, `_handle_hello`, `_handle_hello_ack`, `_install_keys`, `crypto.generate_keypair`, `derive_session_keys`, `derive_shared_secret`, `tick` (gửi lại HELLO).

```mermaid
sequenceDiagram
    autonumber
    participant C as ChatClient
    participant CC as Connection (client)
    participant CR as crypto.py
    participant PK as packet.py
    participant SRV as ChatServer
    participant SC as Connection (server)

    C->>CC: handle_handshake()
    Note right of CC: IN: không có<br/>OUT: không trả, gửi 1 gói HELLO
    CC->>CR: generate_keypair()
    CR-->>CC: (X25519PrivateKey, public_bytes 32B)
    CC->>PK: pack(PKT_HELLO, conn_id, seq=0, FLAG_NONE, stream=0, offset=0, payload)
    Note right of PK: payload = JSON {public_key: hex, conn_id}
    PK-->>CC: raw = header 22B + payload
    CC->>CC: lưu _hello_raw (để gửi lại), _raw_send(raw)
    CC-)SRV: UDP datagram HELLO

    SRV->>SRV: _read_datagrams() → handle_datagram(data, addr)
    Note right of SRV: IN: data=bytes, addr=(ip,port)<br/>conn_id chưa có trong _sessions
    SRV->>SRV: _create_session(conn_id, addr)
    Note right of SRV: tạo Connection(is_server=True) + Session<br/>OUT: ghi vào _sessions[conn_id]
    SRV->>SC: receive_packet(data)
    SC->>PK: unpack(raw) → (Header, payload)
    SC->>SC: _handle_hello(header, payload)
    Note right of SC: IN: header (msg_type=HELLO), payload JSON chứa public_key client
    alt đã có _hello_ack_raw (HELLO trùng do HELLO_ACK bị mất)
        SC-)CC: _raw_send(_hello_ack_raw) gửi lại y nguyên
    else HELLO đầu tiên
        SC->>CR: generate_keypair()
        CR-->>SC: (priv_server, pub_server 32B)
        SC->>SC: _install_keys(peer_pub_bytes)
        SC->>CR: derive_session_keys(priv_server, peer_pub_bytes)
        CR->>CR: derive_shared_secret(priv, peer_pub, info=directional)
        Note right of CR: ECDH X25519 → HKDF-SHA256 (32B shared)<br/>rồi HKDF expand 64B
        CR-->>SC: (client_key 32B, server_key 32B)
        SC->>CR: derive_shared_secret(priv, peer_pub)
        CR-->>SC: _session_key 32B (chỉ để tương thích/chẩn đoán)
        Note right of SC: server: _send_key = server_key, _recv_key = client_key
        SC->>PK: pack(PKT_HELLO_ACK, ..., payload = JSON {public_key: pub_server})
        SC-)CC: _raw_send(HELLO_ACK) và handshake_complete.set()
    end

    CC->>CC: receive_packet → unpack → _handle_hello_ack(header, payload)
    Note right of CC: IN: payload JSON chứa public_key server<br/>bỏ qua nếu chưa gửi HELLO hoặc đã handshake xong
    CC->>CC: _install_keys(peer_pub_bytes)
    CC->>CR: derive_session_keys(private_key, peer_pub_bytes)
    CR-->>CC: (client_key, server_key)
    Note right of CC: client: _send_key = client_key, _recv_key = server_key
    CC->>CC: handshake_complete.set()

    opt HELLO_ACK không về sau 1.0s (HELLO_RETRY_INTERVAL)
        loop tối đa 5 lần (HELLO_MAX_RETRIES), mỗi 50ms kiểm tra
            C->>CC: tick() (từ ChatClient._tick_loop)
            CC-)SRV: _raw_send(_hello_raw)
        end
    end
```

---

## 2. Đăng nhập

Hàm: `app.js(submit handler)`, `HttpUI.handle_http_request`, `_handle_login`, `ChatClient.connect`, `_send_json`, `Stream.write`, `Session.process_payload`, `_process_auth`, `verify_auth`, `UserDatabase.verify_user`, `hash_password_with_salt`, `ChatServer._on_auth_success`, `broadcast_user_list`, `_broadcast`, `ChatClient._on_app_message`, `HttpUI._sse_pump`.

(Đường truyền gói DATA chi tiết xem mục 3 & 4; ở đây rút gọn thành "gửi/nhận qua Stream 0".)

```mermaid
sequenceDiagram
    autonumber
    participant B as Browser app.js
    participant H as HttpUI
    participant C as ChatClient
    participant ST as Stream 0 + Connection
    participant SRV as ChatServer
    participant SS as Session
    participant DB as UserDatabase (SQLite)

    B->>H: POST /api/login  body JSON {username, password}
    H->>H: handle_http_request(client_socket) → _read_text_body → _handle_login(sock, body)
    H->>C: connect(username, password)
    Note right of C: IN: 2 str. Nếu chưa chạy: handle_handshake() (mục 1)<br/>+ khởi 2 thread poll_network và _tick_loop
    C->>ST: handshake_complete.wait(timeout=5s)
    ST-->>C: True/False
    alt hết 5s chưa có khóa
        C-->>H: False (không gửi AUTH)
    else
        C->>C: password_hash = sha256(password) hex 64 ký tự
        C->>C: _send_json({"type":"AUTH","username","password_hash"})
        Note right of C: từ chối gửi nếu handshake_complete chưa set
        C->>ST: get_or_create_stream(0).write(json_bytes)
        Note right of ST: IN: bytes → chia chunk ≤1362B, chunk cuối có FLAG_EOM<br/>OUT: số byte đã xếp hàng (int)
        ST-)SRV: gói DATA mã hóa (mục 3)
        SRV->>SS: _on_app_message(conn_id, stream_id=0, data) → process_payload(data)
        Note right of SS: kiểm tra ≤64KB, JSON dict, có type kiểu str<br/>chưa login chỉ cho phép AUTH/LOGOUT
        SS->>SS: _process_auth(msg)
        Note right of SS: kiểm tra username ≤32 ký tự<br/>password_hash đúng 64 ký tự hex → lower()
        SS->>SS: verify_auth(username, password_hash)
        SS->>DB: verify_user(username, password_hash)
        DB->>DB: SELECT password_hash, salt WHERE username
        DB->>DB: hash_password_with_salt(password_hash, salt) = PBKDF2-SHA256 10000 vòng
        DB-->>SS: bool (so sánh hmac.compare_digest)
        SS-->>SS: nếu đúng: self.username=…, self.authenticated=True
        SS->>SS: _send_app_message({"type":"AUTH_RESP","success":bool,"username","message"})
        SS-)C: Stream 0 → DATA mã hóa → AUTH_RESP
        opt success và chưa từng authenticated
            SS->>SRV: _on_auth_success(conn_id, username)
            Note right of SRV: nếu _user_to_conn[username] đang là session khác<br/>→ gửi KICKED cho session cũ rồi đóng (mục 10)
            SRV->>SRV: _user_to_conn[username]=conn_id, broadcast_user_list()
            SRV->>SRV: get_online_users() → list[str] đã sort
            SRV->>SRV: _broadcast("SYSTEM", {"type":"USER_LIST","users":[…]})
            SS->>SRV: _broadcast_fn(username, {"type":"SYSTEM","message":"… has joined"}, exclude=username)
        end
    end

    C->>C: _on_app_message(0, data) khi AUTH_RESP về
    Note right of C: json.loads → _authenticated = msg["success"]<br/>event_queue.put(msg)
    loop chờ tối đa 2.5s, mỗi 50ms
        H->>C: is_authenticated (property → bool)
    end
    H-->>B: 200 {"status":"ok","username","message"} + Set-Cookie streamchat_user
    B->>B: initSSE(username) → EventSource("/api/events?username=…")
    B->>H: GET /api/events
    H->>H: _handle_sse(client_socket)
    Note right of H: thêm socket vào _sse_clients<br/>đẩy ngay AUTH_RESP + USER_LIST hiện có
    H->>H: _sse_pump() lấy event_queue.get() → _record_to_history → _broadcast_sse(event)
    H-)B: "data: {json}\n\n"
    B->>B: onmessage → handleServerEvent(data) → handleAuthResponse / handleUserList
```

---

## 3. Gửi một gói DATA

Hàm: `Stream.write`, `Connection._stream_send_callback`, `send_packet`, `_flush_send_queue`, `ReliabilityManager.can_send`, `_transmit_data`, `_alloc_seq`, `packet.pack`, `crypto.encrypt`, `_construct_nonce`, `ReliabilityManager.mark_sent`, `_raw_send`.

```mermaid
sequenceDiagram
    autonumber
    participant APP as Gọi từ tầng ứng dụng
    participant ST as Stream
    participant CN as Connection
    participant R as ReliabilityManager
    participant PK as packet.py
    participant CR as crypto.py
    participant NET as UDP socket

    APP->>ST: write(data: bytes)
    Note right of ST: IN: message ứng dụng (JSON hoặc chunk file ≤4096B)<br/>OUT: tổng số byte (int). Message rỗng → gửi 1 chunk b"" với EOM
    loop cắt theo chunk_size = 1362B (MAX_UDP_PAYLOAD − 22 header − 16 tag)
        ST->>CN: _stream_send_callback(stream_id, offset=_write_offset, chunk, flags)
        Note right of ST: flags = FLAG_EOM chỉ ở chunk cuối của lần write()<br/>_write_offset += len(chunk)
        CN->>CN: send_packet(PKT_DATA, stream_id, chunk, flags, offset)
        Note right of CN: DATA không gửi ngay: _tx_queue.append((stream_id, payload, flags, offset))<br/>→ không bao giờ block luồng gọi
        CN->>CN: _flush_send_queue()
        loop khi _tx_queue còn và cwnd còn chỗ
            CN->>R: can_send()
            R-->>CN: bool = (len(_unacked) < int(cwnd))
            CN->>CN: _transmit_data(stream_id, payload, flags, offset)
            CN->>CN: _alloc_seq()
            Note right of CN: có threading.Lock. OUT: seq:int tăng dần từ 0
            alt đã có _send_key (sau handshake)
                CN->>PK: pack(PKT_DATA, conn_id, seq, flags|FLAG_ENCRYPTED, stream_id, offset, payload = 0x00 × (len+16))
                PK-->>CN: bytes → lấy [:22] làm header_aad
                Note right of PK: payload_len trong header = len(plaintext)+16<br/>đúng bằng độ dài ciphertext+tag
                CN->>CR: encrypt(payload, _send_key, seq, associated_data=header_aad, is_server)
                CR->>CR: _construct_nonce(seq, is_server)
                Note right of CR: OUT: 12B = [0x00 client / 0x01 server] + seq 11B<br/>kiểm tra chống dùng lại nonce trong _used_nonces
                CR-->>CN: ciphertext || tag(16B)
                CN->>CN: raw = header_aad + ciphertext
            else chưa có khóa (chỉ trước handshake)
                CN->>PK: pack(PKT_DATA, …, payload) → raw (plaintext)
            end
            CN->>R: mark_sent(seq, raw)
            Note right of R: _unacked[seq] = _InFlightPacket(seq, raw, now, 0)<br/>validate: seq int ≥ 0, raw là bytes
            CN->>NET: _raw_send(raw) → sock.sendto(raw, remote_addr)
        end
    end
```

---

## 4. Nhận một gói DATA

Hàm: `ChatClient.poll_network` / `ChatServer._read_datagrams` → `handle_datagram` → `Connection.receive_packet` → `packet.unpack` → `_handle_data` → `crypto.decrypt` → `Stream.read_incoming` → `_try_assemble` → `_send_ack` → `Stream.get_complete_messages` → `on_app_message`.

```mermaid
sequenceDiagram
    autonumber
    participant NET as UDP socket
    participant RX as ChatServer / ChatClient (vòng đọc)
    participant CN as Connection
    participant PK as packet.py
    participant CR as crypto.py
    participant ST as Stream
    participant APP as on_app_message (callback)

    NET-->>RX: datagram bytes + addr
    alt phía Server
        RX->>RX: _read_datagrams() → handle_datagram(data, addr)
        RX->>PK: unpack(data) → (Header, payload)
        Note right of RX: lấy header.conn_id, tra _sessions<br/>chưa có → _create_session. Địa chỉ đổi → mục 9
    else phía Client
        RX->>RX: poll_network() (thread riêng, 5ms nếu BlockingIOError)
    end
    RX->>CN: receive_packet(raw_bytes)
    CN->>PK: unpack(raw_bytes)
    PK-->>CN: (Header, payload) hoặc ValueError nếu <22B (gói bị bỏ)
    CN->>CN: cập nhật _last_recv_time, rẽ nhánh theo msg_type
    Note right of CN: ACK→_handle_ack · DATA→_handle_data · HELLO→_handle_hello<br/>HELLO_ACK→_handle_hello_ack · HEARTBEAT→_send_heartbeat_ack
    CN->>CN: _handle_data(header, payload, header_aad = raw[:22])
    alt _recv_key chưa có (chưa handshake)
        CN-->>CN: bỏ gói, không ACK
    else gói không có FLAG_ENCRYPTED
        CN-->>CN: bỏ gói, không ACK
    else
        CN->>CR: decrypt(payload, _recv_key, header.seq_num, associated_data=header_aad, is_server = not self.is_server)
        CR->>CR: _construct_nonce(seq, is_server của BÊN GỬI)
        CR-->>CN: plaintext hoặc InvalidTag (sai khóa / header bị sửa)
        alt InvalidTag / ValueError
            CN-->>CN: bỏ gói, KHÔNG ACK (bên gửi sẽ truyền lại)
        end
        CN->>ST: get_or_create_stream(header.stream_id)
        ST-->>CN: Stream
        CN->>ST: read_incoming(offset=header.offset, data=plaintext, is_eom=bool(flags & EOM))
        Note right of ST: 1. chunk đã giao rồi (end ≤ _expected_offset) → bỏ qua<br/>2. chồng một phần → cắt phần trùng<br/>3. trùng offset đã đệm → bỏ bản đến sau<br/>4. chồng lấn chunk đệm khác → ValueError<br/>5. vượt 65536 chunk hoặc 16MB → ValueError<br/>6. lưu _recv_buffer[offset], chèn _sorted_offsets, ghi _eom_offsets
        loop đến khi không ghép được nữa
            ST->>ST: _try_assemble()
            Note right of ST: duyệt offset tăng dần từ _expected_offset<br/>gặp khoảng trống → None<br/>tới biên EOM → nối bytes, dọn buffer, _expected_offset = biên EOM<br/>OUT: bytes message đầy đủ hoặc None
        end
        ST-->>CN: first_assembled (bytes | None). Message hoàn chỉnh nằm trong _complete_messages
        alt ValueError từ read_incoming
            CN-->>CN: KHÔNG ACK
        else chấp nhận
            CN->>CN: _send_ack(header.seq_num)
            Note right of CN: ACK = pack(PKT_ACK, conn_id, seq=0, stream=0, payload = JSON {"ranges":[[seq,seq]]})<br/>gửi bằng _raw_send, KHÔNG mã hóa, KHÔNG vào hàng đợi cwnd
            CN->>ST: get_complete_messages()
            ST-->>CN: list[bytes] (xóa danh sách nội bộ)
            loop mỗi message
                CN->>APP: on_app_message(stream_id, msg)
            end
        end
    end
```

**Điểm cần chú ý:** chunk trùng (do truyền lại) vẫn được ACK lại vì `read_incoming` trả `None` mà không ném lỗi — nên bên gửi sẽ ngừng truyền lại.

---

## 5. ACK / SACK / RTT / CUBIC

Hàm: `Connection._handle_ack`, `ReliabilityManager.process_sack`, `update_rtt`, `update_cwnd`, `Connection._flush_send_queue`.

```mermaid
sequenceDiagram
    autonumber
    participant CN as Connection (bên gửi DATA)
    participant R as ReliabilityManager

    Note over CN: nhận gói PKT_ACK qua receive_packet
    CN->>CN: _handle_ack(payload: bytes)
    Note right of CN: json.loads → ranges = [(start,end), …]<br/>payload hỏng (JSON/Key/Value/Type/Index) → bỏ qua, không văng lỗi
    CN->>R: process_sack(ack_ranges: list[tuple[int,int]])
    Note right of R: validate mọi dải trước (int, ≥0, start ≤ end)<br/>sai → TypeError/ValueError
    loop mỗi dải (start,end)
        R->>R: chọn tập seq cần kiểm: duyệt dải nếu ngắn hơn _unacked, ngược lại duyệt _unacked (chống DoS)
        R->>R: _unacked.pop(seq) → newly_acked_count += 1, cập nhật max_acked_seq
        Note right of R: Luật Karn: chỉ lấy mẫu RTT nếu retransmit_count == 0<br/>sample_rtt = now − sent_time
    end
    loop mỗi mẫu RTT
        R->>R: update_rtt(sample_rtt: float ≥ 0)
        Note right of R: lần đầu: SRTT = s, RTTVAR = s/2<br/>sau đó: RTTVAR = 0.75·RTTVAR + 0.25·|SRTT−s|, SRTT = 0.875·SRTT + 0.125·s<br/>RTO = SRTT + max(0.05, 4·RTTVAR), kẹp [0.2, 10]s
    end
    alt newly_acked_count > 0
        R->>R: _consecutive_timeouts = 0, consecutive_dupacks = 0
        R->>R: update_cwnd(is_loss=False, acked_packets=n)
        Note right of R: Slow Start (cwnd < ssthresh): cwnd += n, trần ssthresh/1024<br/>Congestion Avoidance: W_cubic = C(t+RTT−K)³ + W_max<br/>w_target = max(W_cubic, W_tcp) ≤ 1.5·cwnd<br/>cwnd += n·(w_target−cwnd)/cwnd
    else ACK trùng / gói đã xóa
        R->>R: duplicate_acks += 1, consecutive_dupacks += 1 (không tăng cwnd)
    end
    CN->>CN: _flush_send_queue()
    Note right of CN: cửa sổ vừa mở → đẩy tiếp các gói đang chờ trong _tx_queue
```

---

## 6. Phát hiện mất gói & truyền lại

Hàm: `ChatClient._tick_loop` / `ChatServer._tick_loop` → `Connection.tick` → `ReliabilityManager.get_retransmits`, `update_cwnd(is_loss=True)`, `take_failed`, `_flush_send_queue`.

Có **hai cơ chế**: (a) hết RTO, (b) **fast retransmit dựa trên SACK** — khi đã có ≥ 3 gói có seq cao hơn được ACK.

```mermaid
sequenceDiagram
    autonumber
    participant L as _tick_loop (mỗi 50ms)
    participant CN as Connection
    participant R as ReliabilityManager
    participant NET as UDP socket

    L->>CN: tick()
    CN->>CN: now − _last_recv_time > 30s ? (IDLE_TIMEOUT)
    alt quá hạn
        CN-->>L: closed = True, return
    end
    CN->>CN: now − _last_heartbeat > 5s ? → _send_heartbeat() (mục 9)
    CN->>R: get_retransmits()
    Note right of R: IN: không có. OUT: list[bytes] (raw gói cần gửi lại, nguyên bản cũ)
    loop seq tăng dần trong _unacked
        R->>R: timed_out = (now − sent_time ≥ rto)
        R->>R: fast_lost = (max_acked_seq − max(packet_num, fr_mark) ≥ 3)
        alt timed_out hoặc fast_lost
            alt retransmit_count ≥ max_retransmits (8)
                R->>R: failed_packets.add(seq), _failed_pending.add(seq), xóa khỏi _unacked
            else
                R->>R: thêm raw vào kết quả, sent_time = now, retransmit_count += 1, fr_mark = max_acked_seq
            end
        end
    end
    opt có gói được truyền lại
        R->>R: nếu có timeout thật: _consecutive_timeouts += 1, rto = min(rto·2, 10) (exponential backoff)
        R->>R: nếu now − _last_loss_time ≥ max(srtt, 0.2s) → update_cwnd(is_loss=True)
        Note right of R: chỉ giảm cwnd tối đa 1 lần mỗi RTT (chống sụp cửa sổ, ADR-009)<br/>Fast Convergence: nếu cwnd < _w_last_max → W_max = cwnd·0.85, ngược lại W_max = cwnd<br/>ssthresh = max(0.7·cwnd, 2), cwnd = ssthresh, reset epoch
    end
    R-->>CN: list[bytes]
    loop mỗi raw
        CN->>NET: _raw_send(raw)
    end
    CN->>R: take_failed()
    R-->>CN: set[int] seq bỏ cuộc (đồng thời xóa _failed_pending)
    alt set không rỗng
        CN-->>L: closed = True (đối tác không liên lạc được), return
    end
    CN->>CN: _flush_send_queue()
```

---

## 7. Chat phòng chung & chat riêng

Hàm: `app.js sendMessage`, `HttpUI._handle_send`, `ChatClient.send_chat`, `_send_json`, `Session._process_msg`, `_validated_target`, `route_broadcast` / `route_unicast`, `ChatServer._broadcast` / `_unicast`, `ChatClient._on_app_message`, `HttpUI._sse_pump`, `app.js handleChatMessage`.

```mermaid
sequenceDiagram
    autonumber
    participant BA as Browser Alice
    participant HA as HttpUI (Alice)
    participant CA as ChatClient (Alice)
    participant SS as Session (Alice, trên server)
    participant SRV as ChatServer
    participant SB as Session (Bob, trên server)
    participant CB as ChatClient (Bob)
    participant HB as HttpUI (Bob)
    participant BB as Browser Bob

    BA->>HA: POST /api/send  {"target":"all"|"bob","text":str}
    HA->>HA: _handle_send(sock, body)
    HA->>CA: send_chat(target, text)
    CA->>CA: _send_json({"type":"MSG","target","text"})
    CA-)SS: Stream 0 → DATA mã hóa (mục 3,4)
    HA->>HA: ghi tin của mình vào _message_history (có "is_private")
    HA-->>BA: 200 {"status":"ok","message":"Sent"}

    SS->>SS: process_payload(app_msg) → _process_msg(msg)
    SS->>SS: _validated_target(msg) → str (strip, lower, rỗng → "all") hoặc None nếu sai
    Note right of SS: text phải là str, không rỗng, ≤10000 ký tự<br/>chat_msg = {"type":"MSG","from":username,"text","target","timestamp":time.time()}
    alt target == "all"
        SS->>SS: route_broadcast(chat_msg)
        SS->>SRV: _broadcast(sender, msg, exclude=username người gửi)
        Note right of SRV: json.dumps → bytes<br/>targets = các session đã authenticated, chưa closed, ≠ exclude
        loop mỗi session đích
            SRV->>SB: connection.get_or_create_stream(0).write(data)
            SB-)CB: DATA mã hóa bằng KHÓA CỦA BOB (mục 3)
        end
    else target là username
        SS->>SS: route_unicast(target_username, chat_msg)
        alt gửi cho chính mình
            SS->>SS: _send_app_message(msg) (echo)
        else
            SS->>SRV: _unicast(sender, target, msg) → bool
            Note right of SRV: tra _user_to_conn → conn_id → _sessions<br/>kiểm tra authenticated, username khớp, chưa closed<br/>OUT: True nếu ghi Stream 0 thành công
            alt False
                SS->>SS: _send_app_message({"type":"ERROR","message":"User '…' is offline or does not exist."})
            else True
                SRV->>SB: stream(0).write(data) → DATA tới Bob
            end
        end
    end

    CB->>CB: Connection._handle_data → on_app_message(0, data) = _on_app_message
    Note right of CB: json.loads → dict. Loại MSG: event_queue.put(msg)
    HB->>HB: _sse_pump(): event_queue.get() → _record_to_history(event) → _broadcast_sse(event)
    HB-)BB: SSE "data: {json}\n\n"
    BB->>BB: onmessage → handleServerEvent → handleChatMessage(data) → addChatMessage(sender, text, ts, isOutgoing=false, isPrivate, target)
```

---

## 8. Gửi file qua server

Hàm: `HttpUI._handle_file_stream_upload`, `ChatClient.start_file_transfer`, `Connection.allocate_stream`, `ChatClient._transfer_file_data`, `Session._process_file`, `process_file_chunk`, `ChatServer._on_app_message`, `_route_file_chunk`, `ChatClient._handle_incoming_file_chunk`, `get_download_file`, `HttpUI._handle_download`.

> **File đi A → Server → B, không P2P.** Chunk raw được server giải mã bằng khóa của A rồi `write()` lại sang stream của B (mã hóa bằng khóa của B).

```mermaid
sequenceDiagram
    autonumber
    participant BA as Browser Alice
    participant HA as HttpUI (Alice)
    participant CA as ChatClient (Alice)
    participant CNA as Connection (Alice)
    participant SRV as ChatServer
    participant SS as Session (Alice, server)
    participant CNB as Connection (Bob, server)
    participant CB as ChatClient (Bob)
    participant HB as HttpUI (Bob)
    participant BB as Browser Bob

    BA->>HA: POST /api/file?filename=…&target=bob  body = bytes file
    HA->>HA: _handle_file_stream_upload(sock, query, headers, body_prefix, content_length)
    Note right of HA: ghi file vào %TEMP%/streamchat_uploads/<ts>_<uuid>/<filename>
    HA->>CA: start_file_transfer(file_path, target)
    Note right of CA: IN: đường dẫn str, target str<br/>OUT: stream_id:int hoặc None nếu file không tồn tại
    CA->>CA: tính SHA-256 file (đọc từng 8192B)
    CA->>CNA: allocate_stream()
    CNA-->>CA: Stream với stream_id LẺ (1,3,5…), _next_stream_id += 2
    CA->>CA: _send_json({"type":"FILE","filename","size","sha256","stream_id","target"})
    CA-)SS: Stream 0 → DATA mã hóa
    CA->>CA: tạo thread chạy _transfer_file_data(file_path, stream, target)
    CA->>CA: ghi _file_transfers[sid] (status "transferring") và _incoming_files[sid] (status "completed", cho bản của chính người gửi)
    HA-->>BA: 200 {"status":"ok","stream_id","filename","download_url","message"}

    SS->>SS: process_payload → _process_file(msg)
    Note right of SS: kiểm tra: filename (không chứa / \\, ≤255, không ký tự điều khiển)<br/>size int ≥0 · sha256 64 hex · stream_id 1..65535 chưa dùng · download_url ≤2048
    SS->>SS: file_info = {"type":"FILE_READY","from","filename","size","sha256","stream_id","target","download_url","timestamp"}
    alt target == "all"
        SS->>SRV: route_broadcast(file_info) → _broadcast(...)
        SS->>SS: _file_transfers[stream_id] = ("all", size, 0) nếu size > 0
    else target là username
        SS->>SRV: route_unicast(target, file_info) → _unicast(...) → bool
        SS->>SS: nếu True và size > 0: _file_transfers[stream_id] = (target, size, 0)
    end
    SRV-)CB: DATA (Stream 0) chứa FILE_READY
    CB->>CB: _on_app_message(0, data) loại "FILE_READY"
    Note right of CB: tạo _incoming_files[sid] = {…, received:0, chunks:[], status:"receiving"}<br/>size == 0 → tạo file rỗng ngay, status "completed"<br/>event_queue.put(msg) → SSE → handleFileReady

    loop đọc file từng 4096B
        CA->>CNA: _transfer_file_data → stream.write(chunk 4096B)
        Note right of CNA: Stream cắt 4096B → 3 chunk (1362+1362+1362 … chunk cuối EOM)<br/>mỗi lần write() = 1 "message" độc lập trên stream_id
        CNA-)SRV: DATA mã hóa trên stream_id lẻ (mục 3)
        CA->>CA: cập nhật _file_transfers[sid]["sent"]; chờ reliability.can_send() (ngủ 1ms)
        SRV->>SRV: Connection(Alice)._handle_data → on_app_message(stream_id≠0, msg)
        SRV->>SS: _on_app_message(conn_id, stream_id, data) → process_file_chunk(stream_id, data)
        Note right of SS: kiểm tra đã đăng nhập, stream_id hợp lệ, data không rỗng ≤48KB<br/>phải có metadata trong _file_transfers, received+len ≤ expected_size
        SS->>SRV: _file_chunk_fn = _route_file_chunk(sender, target, stream_id, data) → bool
        SRV->>CNB: get_or_create_stream(stream_id).write(data)
        CNB-)CB: DATA mã hóa bằng khóa của Bob trên cùng stream_id
        SS->>SS: cập nhật received_size, bằng expected_size thì xóa _file_transfers[stream_id]
        CB->>CB: _on_app_message(stream_id≠0, data) → _handle_incoming_file_chunk(stream_id, data)
        Note right of CB: chunks.append(data), received += len<br/>event_queue.put({"type":"FILE_PROGRESS","progress":%,…})
    end

    CA->>CA: hết file: _file_transfers[sid].status = "completed"; event_queue.put({"type":"FILE_COMPLETE",…})
    CB->>CB: received ≥ size: b"".join(chunks) → sha256 so với metadata
    alt hash sai
        CB->>CB: status "corrupted", event_queue.put({"type":"FILE_ERROR","message":"File integrity verification failed."})
    else hash đúng
        CB->>CB: ghi file %TEMP%/streamchat_downloads/<sid>_<tên>, status "completed"
        CB->>CB: event_queue.put({"type":"FILE_READY","download_url":"/api/download?stream_id=…","completed":true})
    end
    HB->>HB: _sse_pump → _record_to_history (gộp theo stream_id) → _broadcast_sse
    HB-)BB: SSE FILE_READY (completed)

    BB->>HB: GET /api/download?stream_id=sid&filename=…
    HB->>HB: _handle_download(sock, query_params)
    HB->>CB: get_download_file(stream_id)
    CB-->>HB: (filename, file_path) hoặc None
    HB-->>BB: 200 + Content-Disposition: attachment, nội dung file (không có kết nối nào tới máy Alice)
```

---

## 9. Heartbeat, timeout, dọn session, NAT rebinding

Hàm: `Connection.tick`, `_send_heartbeat`, `_send_heartbeat_ack`, `ChatServer._tick_loop`, `handle_datagram`, `Connection.verify_migration_datagram`, `ChatServer._broadcast`, `broadcast_user_list`.

```mermaid
sequenceDiagram
    autonumber
    participant CC as Connection (client)
    participant SC as Connection (server, trong Session)
    participant SRV as ChatServer

    rect rgb(235,245,255)
    Note over CC,SC: Heartbeat 5s
    CC->>CC: tick() → _send_heartbeat()
    Note right of CC: pack(PKT_HEARTBEAT, conn_id, seq=0, payload=b"")<br/>cập nhật _last_heartbeat
    CC-)SC: HEARTBEAT (không mã hóa)
    SC->>SC: receive_packet → _send_heartbeat_ack()
    SC-)CC: HEARTBEAT_ACK
    CC->>CC: receive_packet() cập nhật _last_recv_time
    end

    rect rgb(255,245,235)
    Note over SRV,SC: Server dọn session mỗi 50ms
    SRV->>SRV: _tick_loop()
    loop mỗi session
        SRV->>SC: tick()
        SC-->>SRV: (cập nhật closed nếu idle > 30s, hoặc gói vượt max_retransmits)
    end
    SRV->>SRV: gom các session closed
    SRV->>SRV: xóa khỏi _sessions, _addr_to_conn, _user_to_conn (chỉ khi mapping còn trỏ đúng session này)
    SRV->>SRV: _broadcast("SYSTEM", {"type":"SYSTEM","message":"User @x has left the chat room."})
    SRV->>SRV: broadcast_user_list()
    Note right of SRV: mỗi 5s còn broadcast_user_list() định kỳ để đồng bộ danh sách online
    end

    rect rgb(240,255,240)
    Note over SRV,SC: NAT rebinding (địa chỉ nguồn thay đổi)
    SRV->>SRV: handle_datagram(data, addr_mới)
    Note right of SRV: addr_mới ≠ session.client_addr
    SRV->>SC: verify_migration_datagram(raw_bytes) → bool
    Note right of SC: chỉ chấp nhận nếu là gói DATA, có FLAG_ENCRYPTED, đúng conn_id<br/>và decrypt(…) bằng _recv_key thành công (AEAD hợp lệ)
    alt hợp lệ và addr_mới chưa thuộc conn_id khác
        SRV->>SRV: cập nhật session.client_addr, connection.remote_addr, _addr_to_conn
    else không hợp lệ
        SRV-->>SRV: bỏ datagram, ghi cảnh báo "Rejected NAT rebinding"
    end
    end
```

---

## 10. Logout & Kick

Hàm: `app.js` (fetch `/api/logout`), `HttpUI._handle_logout`, `ChatClient.logout`, `Session._process_logout`, `ChatServer._on_auth_logout`, `ChatServer._on_auth_success` (kick), `Session.send_kicked`, `ChatClient._on_app_message` (nhánh `KICKED`).

```mermaid
sequenceDiagram
    autonumber
    participant B as Browser
    participant H as HttpUI
    participant C as ChatClient
    participant SS as Session
    participant SRV as ChatServer
    participant SS2 as Session cũ (cùng tài khoản)
    participant C2 as ChatClient của session cũ

    rect rgb(235,245,255)
    Note over B,SRV: Logout chủ động (UDP vẫn giữ để đăng nhập lại)
    B->>H: POST /api/logout
    H->>H: _handle_logout(sock)
    Note right of H: xóa _message_history và _online_users, Set-Cookie hết hạn
    H->>C: logout()
    C->>C: _send_json({"type":"LOGOUT"}), _authenticated=False, _username=None
    C-)SS: Stream 0 → DATA
    SS->>SS: _process_logout()
    Note right of SS: authenticated=False, username=None<br/>gửi {"type":"LOGOUT_RESP","success":true}
    SS->>SRV: _on_auth_logout(conn_id, username)
    SRV->>SRV: pop _user_to_conn[username] nếu còn đúng session
    SRV->>SRV: _broadcast("SYSTEM", "… has left") và broadcast_user_list()
    H-->>B: 200 {"status":"ok","message":"Logged out"}
    end

    rect rgb(255,240,240)
    Note over SRV,C2: Kick: cùng tài khoản đăng nhập ở nơi khác
    Note over SRV: _on_auth_success(conn_id_mới, username) thấy _user_to_conn[username] là conn_id khác
    SRV->>SS2: send_kicked("Your account logged in from another location.")
    SS2->>SS2: _send_app_message({"type":"KICKED","message":…})
    SS2-)C2: DATA (Stream 0)
    SRV->>SS2: authenticated=False, username=None, connection.tick() rồi connection.closed=True
    C2->>C2: _on_app_message(0, data) loại KICKED
    Note right of C2: _authenticated=False, _running=False, đóng socket<br/>tạo lại socket + Connection mới để lần login sau sạch<br/>event_queue.put(msg) → SSE → handleKicked trên browser
    end
```

---

## 11. Ghi chú

- **`src/server/web_server.py` (`UnifiedChatServer`) hiện không được import ở đâu** (`run_server.py` chỉ khởi tạo `ChatServer` UDP) nên **không xuất hiện** trong các diagram. Nếu nhóm còn dùng nó thì cần vẽ riêng.
- **README đã lỗi thời so với code** ở các điểm: khóa mã hóa là **hai khóa theo chiều** (`derive_session_keys`) chứ không phải một khóa chung; có **fast retransmit** dựa trên SACK (`FAST_RETRANSMIT_DUPTHRESH = 3`); DATA đi qua **hàng đợi `_tx_queue`** bị chặn bởi cwnd; handshake có **gửi lại HELLO** (K11).
- Gói `PKT_ACK`, `PKT_HEARTBEAT`, `PKT_HEARTBEAT_ACK` **không được mã hóa / xác thực** (chỉ DATA có AEAD). Đây là điểm nên ghi trong phần hạn chế của báo cáo.
- Trong code còn các lệnh `print` debug (`[debug] encrypt()…`, `[PAYLOAD]`, `[DEBUG] … hex`, `HEHEHEHEH:`) in **nội dung tin nhắn dạng rõ** ra console — nên xóa trước khi nộp.
