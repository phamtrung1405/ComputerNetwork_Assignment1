# STREAMCHAT — Shared Team Progress Tracker

**Last Updated:** 2026-11-13
**Scope note:** M1/M2 integration statuses below were updated against the current code and tests. M3/M4 task ownership rows remain a team tracker and have not all been re-audited in this update.
**Status Legend:**
- `NOT_STARTED`: Work has not yet begun.
- `IN_PROGRESS`: Actively under development or refactoring.
- `BLOCKED`: Development halted due to unresolved external dependencies or architecture conflict.
- `IMPLEMENTED_UNTESTED`: Code is written but has zero automated unit or integration tests.
- `TESTING`: Automated test cases are currently being written and executed.
- `VERIFIED`: Confirmed passing via repeatable automated unit/integration tests or manual trace logs.
- `DEFERRED`: Intentionally postponed for a future milestone.

---

## 1. Executive Summary Across Members

| Member | Assigned Area | Total Tasks | Implemented (Untested) | In Progress / Blocked | Verified |
| :--- | :--- | :---: | :---: | :---: | :---: |
| **M1 — Trung** | Constants, Packet, Connection | 6 | 0 | 0 | 6 |
| **M2 — Phước** | Crypto, Reliability, Stream | 8 | 0 | 0 | 8 |
| **M3 — Toàn** | Server, Session, Routing | 5 | 2 | 0 | 3 |
| **M4 — Bảo** | Client, HTTP UI, Web Frontend | 6 | 6 | 0 | 0 |

---

## 2. Master Task Table

| Task ID | Owner | Module | Task | Status | Priority | Dependencies | Evidence | Next Action |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **T-M1-01** | M1 | `constants.py` | Align both exported header format names with the 22-byte wire layout | `VERIFIED` | High | None | `HEADER_FORMAT == HEADER_STRUCT_FMT == "!BIIBHIHI"`; packet tests assert 22 bytes | Keep the format constants and `packet.py` synchronized. |
| **T-M1-02** | M1 | `constants.py` | Account for the 16-byte GCM tag in the maximum application chunk | `VERIFIED` | High | None | `MAX_APP_PAYLOAD = 1362`; stream and loopback tests exercise the MTU boundary | Keep the 1400-byte datagram ceiling. |
| **T-M1-03** | M1 | `packet.py` | Binary pack/unpack serialization routines | `VERIFIED` | High | T-M1-01 | `tests/test_packet.py` checks the shared format, size, and round-trip | Retain packet boundary coverage. |
| **T-M1-04** | M1 | `connection.py` | Retransmit `PKT_HELLO` during handshake | `VERIFIED` | High | T-M2-01 | `HELLO` retry behavior is implemented and covered by Connection loopback tests | Keep retry tests in the integration gate. |
| **T-M1-05** | M1 | `connection.py` | Enforce congestion-window flow control before DATA transmission | `VERIFIED` | High | T-M2-04 | DATA queue is flushed only when `reliability.can_send()` permits; loopback tests cover the window | Keep send queue bounded as a separate follow-up. |
| **T-M1-06** | M1 | `connection.py` | ACK DATA only after successful decryption and stream acceptance | `VERIFIED` | Medium | T-M2-02 | `tests/test_connection_loopback.py` verifies invalid ciphertext/header is not delivered or ACKed | Preserve decrypt/stream/ACK ordering. |
| **T-M2-01** | M2 | `crypto.py` | X25519 ECDH key generation and HKDF shared secret derivation | `VERIFIED` | High | None | Passing unit tests in `tests/test_crypto.py` (`TestKeyAgreement`) | Maintain test suite and integrate into CI. |
| **T-M2-02** | M2 | `crypto.py` | Fix AES-GCM Nonce reuse & separate Client/Server subkeys | `VERIFIED` | Critical | T-M2-01 | Implemented `derive_session_keys()`, directional nonce, and `_used_nonces` guard; used by `Connection` and covered by tests | Keep directional key behavior covered by loopback tests. |
| **T-M2-03** | M2 | `crypto.py` | Bind 22-byte packet header into AEAD Associated Data (AAD) | `VERIFIED` | Medium | T-M1-03 | `Connection` passes the raw header to `encrypt`/`decrypt`; tampering tests pass | Preserve header-AAD coverage. |
| **T-M2-04** | M2 | `reliability.py` | Sliding Window, SACK processing & duplicate ACK tracking | `VERIFIED` | High | T-M1-01 | SACK processing is used by `Connection`; reliability and loopback tests pass | Keep ACK/SACK integration covered by loopback tests. |
| **T-M2-05** | M2 | `reliability.py` | RFC 6298 RTT estimation, Karn's algorithm & Exponential Backoff | `VERIFIED` | High | None | Implemented in `update_rtt` and `get_retransmits`; verified by `TestRttEstimationRfc6298` (5/5) & `TestRetransmissionSelection` | Maintain test suite and integrate into CI loop. |
| **T-M2-06** | M2 | `reliability.py` | RFC 8312 CUBIC, multi-loss collapse fix & Circuit Breaker | `VERIFIED` | Critical | None | Implemented in `update_cwnd` (Concave, Convex, TCP-friendly, Fast Convergence); verified by `TestCubicCongestionControlRfc8312` (7/7) & `test_cubic_multi_loss_collapse_prevention_adr009` | Monitor performance under high artificial jitter. |
| **T-M2-07** | M2 | `stream.py` | Multi-stream chunk framing (`write`), EOM flags & 1362B MTU | `VERIFIED` | High | T-M1-02 | Implemented safe 1362B MTU chunking in `stream.py`; verified by `tests/test_stream.py` (`TestStreamWrite`, 7/7 tests) | Keep stream and header MTU tests aligned. |
| **T-M2-08** | M2 | `stream.py` | Fix reassembly buffer wipe bug, duplicate deadlock & DoS protection | `VERIFIED` | Critical | T-M2-07 | Implemented selective pruning (ADR-008), duplicate chunk handling, and buffer limits; covered by stream and Connection loopback tests | Keep reassembly behavior covered by tests. |
| **T-M3-01** | M3 | `server.py` | Non-blocking UDP server socket and selector event loop | `IMPLEMENTED_UNTESTED` | High | T-M1-01 | [`server.py:64-113`](file:///home/mpman123/Mine/BACH KHOA/BACH KHOA-CHUAN/BACH KHOA-BAI TAP/Networking/ComputerNetwork_Assignment1/src/server/server.py#L64-L113) listens on UDP 9000; verified syntax only | Create loopback integration test sending test datagrams to `ChatServer`. |
| **T-M3-02** | M3 | `server.py` | Session mapping (`conn_id -> Session`, `user -> conn_id`) | `VERIFIED` | High | T-M3-01 | `tests/test_server.py` covers session mapping, duplicate login, and NAT rebinding | Keep NAT rebinding and session lifecycle tests. |
| **T-M3-03** | M3 | `session.py` | Authentication against SQLite database (`users.db`) | `VERIFIED` | High | None | `tests/test_server.py` covers valid/invalid authentication, normalization, and logout | Salted password hashing remains a security follow-up. |
| **T-M3-04** | M3 | `session.py` | Broadcast and Unicast routing on Stream 0 | `VERIFIED` | High | T-M2-07 | `tests/test_server.py` covers broadcast/unicast privacy and routing | Preserve routing isolation tests. |
| **T-M3-05** | M3 | `server.py` | Reconcile UDP engine vs monolithic `web_server.py` | `IMPLEMENTED_UNTESTED` | Critical | T-M4-05 | `run_server.py` starts `ChatServer`; the legacy `web_server.py` is not used by the active entry point | Add an entry-point smoke test if this wiring changes. |
| **T-M4-01** | M4 | `client.py` | UDP network polling and event queue bridge | `IMPLEMENTED_UNTESTED` | High | T-M1-04 | [`client.py:236-253`](file:///home/mpman123/Mine/BACH KHOA/BACH KHOA-CHUAN/BACH KHOA-BAI TAP/Networking/ComputerNetwork_Assignment1/src/client/client.py#L236-L253) polls raw UDP socket in dedicated thread | Test socket clean shutdown when `disconnect()` is invoked. |
| **T-M4-02** | M4 | `client.py` | File transfer thread and stream allocation | `IMPLEMENTED_UNTESTED` | High | T-M2-07 | [`client.py:136-233`](file:///home/mpman123/Mine/BACH KHOA/BACH KHOA-CHUAN/BACH KHOA-BAI TAP/Networking/ComputerNetwork_Assignment1/src/client/client.py#L136-L233) sends chunks in 4096B blocks | Replace busy-wait `while not can_send()` with condition variable or event. |
| **T-M4-03** | M4 | `http_ui.py` | Raw TCP socket HTTP/1.1 static file & REST handler | `IMPLEMENTED_UNTESTED` | High | None | [`http_ui.py:77-108`](file:///home/mpman123/Mine/BACH KHOA/BACH KHOA-CHUAN/BACH KHOA-BAI TAP/Networking/ComputerNetwork_Assignment1/src/client/http_ui.py#L77-L108) accepts TCP connections and parses HTTP/1.1 | Verify concurrent browser requests do not block each other. |
| **T-M4-04** | M4 | `http_ui.py` | Server-Sent Events (SSE) pump from `ChatClient.event_queue` | `IMPLEMENTED_UNTESTED` | High | T-M4-01 | [`http_ui.py:243-314`](file:///home/mpman123/Mine/BACH KHOA/BACH KHOA-CHUAN/BACH KHOA-BAI TAP/Networking/ComputerNetwork_Assignment1/src/client/http_ui.py#L243-L314) broadcasts SSE events | Test browser reconnect handling and heartbeat delivery. |
| **T-M4-05** | M4 | `run_client.py` | Implement automatic port-hunting for multi-client testing | `IMPLEMENTED_UNTESTED` | Critical | None | `run_client.py` has `find_available_port()` and uses it to select the local HTTP UI port | Add a focused test for port selection when needed. |
| **T-M4-06** | M4 | `static/` | Web UI responsiveness, upload progress, and dark theme | `IMPLEMENTED_UNTESTED` | Medium | T-M4-04 | [`src/client/static/`](file:///home/mpman123/Mine/BACH KHOA/BACH KHOA-CHUAN/BACH KHOA-BAI TAP/Networking/ComputerNetwork_Assignment1/src/client/static/) verified visually on browser; untested under packet loss | Test UI behavior when transfer drops or disconnect occurs. |

---

## 3. Integration & Testing Gates

- **Core/unit suite:** `VERIFIED` — `.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"` ran 156 tests, all passing, on 2026-11-13.
- **Connection loopback gate:** `VERIFIED` — `tests/test_connection_loopback.py` exercises two `Connection` instances over simulated sockets, including encrypted delivery, loss handling, flow control, and file hash checks.
- **10 MB transport simulation:** `VERIFIED` — `tests/test_integration.py` simulates packet loss and checks SHA-256 at the Stream/Reliability layer; this is not a browser or full application test.
- **Gate 3 (Full Stack Integration):** `NOT_STARTED` — login and chat/file transfer between two browser clients through separate HTTP UI ports and the real UDP server still need a recorded end-to-end run.

---

## 4. Lịch sử audit 2026-11-08 — Audit M2 và bind M1↔M2

The findings in §§4.1–4.3 describe the pre-integration state on 2026-11-08. They are historical and must not be read as the current status of `connection.py`.

**Nguồn:** `docs/audits/M2_AUDIT_REPORT.md`, `docs/project/M1_M2_INTEGRATION_CONTRACT.md`, `docs/handoff/M1/`.

### 4.1. Phát hiện của audit (chạy thật, mô phỏng một process qua socket giả)
Các hàm M2 đúng ở mức đơn vị, nhưng `connection.py` chưa dùng API M2: nonce dùng lại giữa hai chiều (C-1), header không là AAD (C-2), DATA thô được nhận (C-3), chỉ message đầu tới app (C-4: 30/2560 khối khi mất 5%), ACK trước giải mã (H-1), không thực thi `can_send()` (H-2). Các mục này thuộc `connection.py` (M1).

### 4.2. Việc đã làm phía M2 (file của M2, có test)
| Mục | Thay đổi | Bằng chứng |
| :-- | :-- | :-- |
| H-3 | Fast Convergence `(1+β)/2 = 0.85` (RFC 9438 §4.7); sửa test cũ khẳng định 0.65 | `tests.test_reliability.TestCubicCongestionControlRfc8312.test_fast_convergence` |
| H-4 | Chặn `target ≤ 1.5·cwnd` (RFC 9438 §4.2); điều chỉnh `test_congestion_avoidance_convex_probing` theo RFC | `test_cubic_target_clamped_to_1_5x_cwnd` |
| H-5 | Gói vượt `max_retransmits` rời `_unacked`, thêm `take_failed()` | `test_failed_packet_released_from_window`, `test_take_failed_returns_and_clears` |
| M-3 | `Stream` từ chối chunk chồng lấn, giữ chunk trùng offset đến trước, giới hạn `max_buffered_chunks` | `tests.test_stream.TestStreamOverlapAndChunkLimits` (6 test) |
| M-4 | SACK-based fast retransmit (≥ 3 gói cao hơn được ACK; không backoff RTO) | 4 test `test_fast_retransmit_*` |

Chưa làm (cần quyết định riêng, ADR): M-1 (phản ứng timeout theo RFC 5681), M-2 (W_max/W_est theo RFC 9438 §4.10/§4.3), M-5 (giới hạn `_used_nonces`).

### 4.3. Kết quả test đã chạy (lệnh và kết quả thực tế)
Môi trường: `cryptography` trong site-packages người dùng có dòng lạ `from Buoi6.src.GenerateKey import gene` (ngoài repo) làm `test_crypto`/`test_integration` lỗi import. Các lần chạy dưới đây dùng một stub `Buoi6` rỗng trong `PYTHONPATH`; cần cài lại `cryptography` để chạy không stub.

| Lệnh | Kết quả |
| :-- | :-- |
| `python -m unittest discover -s tests -p "test_*.py"` (có stub, repo hiện tại) | `Ran 113 tests … OK (skipped=3, expected failures=11)` |
| `python run_all.py --check` | thành công, không lỗi cú pháp |
| `tests.test_connection_loopback` trên repo **chưa vá** | 11 expected failure (đúng các lỗi C-1..H-6), 3 skip, 1 pass |
| Cùng file test trên **bản `connection.py` đã vá** (`STREAMCHAT_SRC_DIR` trỏ tới bản sao) | `Ran 15 tests … OK`; lặp 10 lần liên tiếp đều OK (21–42 s/lần) |

### 4.4. Trạng thái tích hợp hiện tại (2026-11-13)
- **Gate B (Connection↔Connection, socket mô phỏng):** `VERIFIED` trên repository hiện tại; `Connection.INTEGRATION_CONTRACT_VERSION == 1` và `tests/test_connection_loopback.py` pass.
- T-M1-01..T-M1-06 đã được cập nhật theo code hiện tại ở §2.
- Gate 3 (Full Stack, UDP thật qua `server.py`/`client.py`): `NOT_STARTED`.
- Hạn chế: Gate B dùng socket mô phỏng trong một process; Gate B không thay thế kiểm thử ứng dụng qua `server.py`, `client.py` và `http_ui.py`.

### 4.5. Câu hỏi/blocker
1. Hàng đợi gửi `_tx_queue` không giới hạn: cần quyết định giới hạn/back-pressure ở tầng ứng dụng hay transport.

### 4.6. Việc tiếp theo
Chạy và lưu bằng chứng Gate 3 với hai browser client; giữ riêng kết quả của test mô phỏng và test full-stack.

