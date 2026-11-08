# STREAMCHAT — Shared Team Progress Tracker

**Last Updated:** 2026-11-08  
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
| **M1 — Trung** | Constants, Packet, Connection | 6 | 4 | 2 | 0 |
| **M2 — Phước** | Crypto, Reliability, Stream | 8 | 0 | 0 | 8 |
| **M3 — Toàn** | Server, Session, Routing | 5 | 3 | 2 | 0 |
| **M4 — Bảo** | Client, HTTP UI, Web Frontend | 6 | 4 | 2 | 0 |

---

## 2. Master Task Table

| Task ID | Owner | Module | Task | Status | Priority | Dependencies | Evidence | Next Action |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **T-M1-01** | M1 | `constants.py` | Align `HEADER_STRUCT_FMT` with actual 22-byte layout | `IN_PROGRESS` | High | None | [`constants.py:73`](file:///home/mpman123/Mine/BACH KHOA/BACH KHOA-CHUAN/BACH KHOA-BAI TAP/Networking/ComputerNetwork_Assignment1/src/common/constants.py#L73) defines obsolete `!BIIBHIH2s` (20B) vs `packet.py` (22B) | Remove legacy 20-byte comments and harmonize header format string to `!BIIBHIHI`. |
| **T-M1-02** | M1 | `constants.py` | Adjust `MAX_APP_PAYLOAD` to account for 16-byte GCM tag | `NOT_STARTED` | High | None | [`constants.py:80`](file:///home/mpman123/Mine/BACH KHOA/BACH KHOA-CHUAN/BACH KHOA-BAI TAP/Networking/ComputerNetwork_Assignment1/src/common/constants.py#L80) sets `MAX_APP_PAYLOAD = 1378`, causing 1416B datagrams | Update `MAX_APP_PAYLOAD = 1362` ($1400 - 22 - 16$). |
| **T-M1-03** | M1 | `packet.py` | Binary pack/unpack serialization routines | `IMPLEMENTED_UNTESTED` | High | T-M1-01 | [`packet.py:40-107`](file:///home/mpman123/Mine/BACH KHOA/BACH KHOA-CHUAN/BACH KHOA-BAI TAP/Networking/ComputerNetwork_Assignment1/src/common/packet.py#L40-L107) exists; no automated tests exist in repo | Write `tests/test_packet.py` testing boundary sizes and corrupted buffers. |
| **T-M1-04** | M1 | `connection.py` | Implement reliable retransmission for `PKT_HELLO` handshake | `BLOCKED` | High | T-M2-01 | [`connection.py:324`](file:///home/mpman123/Mine/BACH KHOA/BACH KHOA-CHUAN/BACH KHOA-BAI TAP/Networking/ComputerNetwork_Assignment1/src/transport/connection.py#L324) sends `PKT_HELLO` raw with no retry mechanism | Wait for M2's crypto handshake retransmission spec and add retry counter. |
| **T-M1-05** | M1 | `connection.py` | Enforce flow control check (`can_send`) before UDP transmission | `NOT_STARTED` | High | T-M2-04 | [`connection.py:170`](file:///home/mpman123/Mine/BACH KHOA/BACH KHOA-CHUAN/BACH KHOA-BAI TAP/Networking/ComputerNetwork_Assignment1/src/transport/connection.py#L170) sends datagrams without querying `reliability.can_send()` | Block or queue packets when `can_send()` returns `False`. |
| **T-M1-06** | M1 | `connection.py` | Postpone ACK transmission until after successful decryption | `NOT_STARTED` | Medium | T-M2-02 | [`connection.py:278`](file:///home/mpman123/Mine/BACH KHOA/BACH KHOA-CHUAN/BACH KHOA-BAI TAP/Networking/ComputerNetwork_Assignment1/src/transport/connection.py#L278) calls `_send_ack` before `decrypt()` executes | Move `_send_ack` after `decrypt()` try-block. |
| **T-M2-01** | M2 | `crypto.py` | X25519 ECDH key generation and HKDF shared secret derivation | `VERIFIED` | High | None | Passing unit tests in `tests/test_crypto.py` (`TestKeyAgreement`) | Maintain test suite and integrate into CI. |
| **T-M2-02** | M2 | `crypto.py` | Fix AES-GCM Nonce reuse & separate Client/Server subkeys | `VERIFIED` | Critical | T-M2-01 | Implemented `derive_session_keys()`, directional Nonce, and `_used_nonces` guard; verified by `test_crypto.py` | Coordinate with M1 to use directional keys in `connection.py`. |
| **T-M2-03** | M2 | `crypto.py` | Bind 22-byte packet header into AEAD Associated Data (AAD) | `VERIFIED` | Medium | T-M1-03 | Added `associated_data` support to `encrypt` and `decrypt`; verified by `test_tampered_associated_data_raises_invalid_tag` | Pass raw 22-byte header into `encrypt`/`decrypt` in `connection.py`. |
| **T-M2-04** | M2 | `reliability.py` | Sliding Window, SACK processing & duplicate ACK tracking | `VERIFIED` | High | T-M1-01 | Implemented `process_sack` with DoS protection and duplicate ACK detection; verified by `tests/test_reliability.py` | Coordinate with M1 on SACK generation in receiver. |
| **T-M2-05** | M2 | `reliability.py` | RFC 6298 RTT estimation, Karn's algorithm & Exponential Backoff | `VERIFIED` | High | None | Implemented in `update_rtt` and `get_retransmits`; verified by `TestRttEstimationRfc6298` (5/5) & `TestRetransmissionSelection` | Maintain test suite and integrate into CI loop. |
| **T-M2-06** | M2 | `reliability.py` | RFC 8312 CUBIC, multi-loss collapse fix & Circuit Breaker | `VERIFIED` | Critical | None | Implemented in `update_cwnd` (Concave, Convex, TCP-friendly, Fast Convergence); verified by `TestCubicCongestionControlRfc8312` (7/7) & `test_cubic_multi_loss_collapse_prevention_adr009` | Monitor performance under high artificial jitter. |
| **T-M2-07** | M2 | `stream.py` | Multi-stream chunk framing (`write`), EOM flags & 1362B MTU | `VERIFIED` | High | T-M1-02 | Implemented safe 1362B MTU chunking in `stream.py`; verified by `tests/test_stream.py` (`TestStreamWrite`, 7/7 tests) | Coordinate with M1 to update `MAX_APP_PAYLOAD = 1362` in `constants.py`. |
| **T-M2-08** | M2 | `stream.py` | Fix reassembly buffer wipe bug, duplicate deadlock & DoS protection | `VERIFIED` | Critical | T-M2-07 | Implemented selective pruning (ADR-008), duplicate chunk bypass, and buffer limit; verified by `tests/test_stream.py` (9/9 tests) | Ready for integration testing with `Connection`. |
| **T-M3-01** | M3 | `server.py` | Non-blocking UDP server socket and selector event loop | `IMPLEMENTED_UNTESTED` | High | T-M1-01 | [`server.py:64-113`](file:///home/mpman123/Mine/BACH KHOA/BACH KHOA-CHUAN/BACH KHOA-BAI TAP/Networking/ComputerNetwork_Assignment1/src/server/server.py#L64-L113) listens on UDP 9000; verified syntax only | Create loopback integration test sending test datagrams to `ChatServer`. |
| **T-M3-02** | M3 | `server.py` | Session mapping (`conn_id -> Session`, `user -> conn_id`) | `IMPLEMENTED_UNTESTED` | High | T-M3-01 | [`server.py:131-146`](file:///home/mpman123/Mine/BACH KHOA/BACH KHOA-CHUAN/BACH KHOA-BAI TAP/Networking/ComputerNetwork_Assignment1/src/server/server.py#L131-L146) tracks connections and IP changes | Test NAT rebinding behavior when remote client changes source port. |
| **T-M3-03** | M3 | `session.py` | Authentication against SQLite database (`users.db`) | `IMPLEMENTED_UNTESTED` | High | None | [`session.py:121`](file:///home/mpman123/Mine/BACH KHOA/BACH KHOA-CHUAN/BACH KHOA-BAI TAP/Networking/ComputerNetwork_Assignment1/src/server/session.py#L121) queries `db.verify_user` | Add salt to passwords instead of raw SHA-256 hash. |
| **T-M3-04** | M3 | `session.py` | Broadcast and Unicast routing on Stream 0 | `IMPLEMENTED_UNTESTED` | High | T-M2-07 | [`session.py:141-244`](file:///home/mpman123/Mine/BACH KHOA/BACH KHOA-CHUAN/BACH KHOA-BAI TAP/Networking/ComputerNetwork_Assignment1/src/server/session.py#L141-L244) serializes JSON to Stream 0 | Confirm whether private messages echo back to sender on Stream 0. |
| **T-M3-05** | M3 | `server.py` | Reconcile UDP engine vs monolithic `web_server.py` | `BLOCKED` | Critical | T-M4-05 | `run_server.py:76` runs `web_server.py`, bypassing `server.py` on main | Align with M4 to strip `web_server.py` from `run_server.py`. |
| **T-M4-01** | M4 | `client.py` | UDP network polling and event queue bridge | `IMPLEMENTED_UNTESTED` | High | T-M1-04 | [`client.py:236-253`](file:///home/mpman123/Mine/BACH KHOA/BACH KHOA-CHUAN/BACH KHOA-BAI TAP/Networking/ComputerNetwork_Assignment1/src/client/client.py#L236-L253) polls raw UDP socket in dedicated thread | Test socket clean shutdown when `disconnect()` is invoked. |
| **T-M4-02** | M4 | `client.py` | File transfer thread and stream allocation | `IMPLEMENTED_UNTESTED` | High | T-M2-07 | [`client.py:136-233`](file:///home/mpman123/Mine/BACH KHOA/BACH KHOA-CHUAN/BACH KHOA-BAI TAP/Networking/ComputerNetwork_Assignment1/src/client/client.py#L136-L233) sends chunks in 4096B blocks | Replace busy-wait `while not can_send()` with condition variable or event. |
| **T-M4-03** | M4 | `http_ui.py` | Raw TCP socket HTTP/1.1 static file & REST handler | `IMPLEMENTED_UNTESTED` | High | None | [`http_ui.py:77-108`](file:///home/mpman123/Mine/BACH KHOA/BACH KHOA-CHUAN/BACH KHOA-BAI TAP/Networking/ComputerNetwork_Assignment1/src/client/http_ui.py#L77-L108) accepts TCP connections and parses HTTP/1.1 | Verify concurrent browser requests do not block each other. |
| **T-M4-04** | M4 | `http_ui.py` | Server-Sent Events (SSE) pump from `ChatClient.event_queue` | `IMPLEMENTED_UNTESTED` | High | T-M4-01 | [`http_ui.py:243-314`](file:///home/mpman123/Mine/BACH KHOA/BACH KHOA-CHUAN/BACH KHOA-BAI TAP/Networking/ComputerNetwork_Assignment1/src/client/http_ui.py#L243-L314) broadcasts SSE events | Test browser reconnect handling and heartbeat delivery. |
| **T-M4-05** | M4 | `run_client.py` | Implement automatic port-hunting for multi-client testing | `BLOCKED` | Critical | None | `run_client.py` on `main` only opens browser; branch `origin/membau` has hunting code | Merge and verify auto-hunting (port 8080, 8081, 8082...) from `origin/membau`. |
| **T-M4-06** | M4 | `static/` | Web UI responsiveness, upload progress, and dark theme | `IMPLEMENTED_UNTESTED` | Medium | T-M4-04 | [`src/client/static/`](file:///home/mpman123/Mine/BACH KHOA/BACH KHOA-CHUAN/BACH KHOA-BAI TAP/Networking/ComputerNetwork_Assignment1/src/client/static/) verified visually on browser; untested under packet loss | Test UI behavior when transfer drops or disconnect occurs. |

---

## 3. Integration & Testing Gates

- **Gate 1 (M1 + M2 Core Transport):** `VERIFIED` — 85/85 tests passed (100% pass rate) across `test_crypto.py`, `test_reliability.py`, `test_stream.py`, and `test_integration.py`.
- **Gate 2 (Transport Loopback Verification):** `VERIFIED` — Thành công truyền payload 10 MB (7,699 gói) qua kênh truyền mô phỏng mất gói 5% tái lập được (seed 42), khắc phục mất 373 gói bằng cơ chế SACK/Retransmit, xác thực băm SHA-256 trùng khớp 100% (`tests/test_integration.py`).
- **Gate 3 (Full Stack Integration):** `NOT_STARTED` — Hai browser client đăng nhập qua các cổng HTTP độc lập (8080 và 8081) giao tiếp xuyên suốt qua UDP server trung tâm tại cổng 9000.

---

## 4. Cập nhật 2026-11-08 — Audit M2 và bind M1↔M2

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

### 4.4. Trạng thái tích hợp
- **Gate B (Connection↔Connection, mô phỏng):** `VERIFIED` **chỉ cho bản vá chưa nằm trong repo**. Trên repo hiện tại: `NOT_STARTED` (chờ M1 áp `docs/handoff/M1/connection.patch`).
- T-M1-04, T-M1-05, T-M1-06 do M1 sở hữu; trạng thái không đổi tại đây. Patch đề xuất giải quyết cả ba. Chưa có xác nhận của Trung.
- Gate 3 (Full Stack, UDP thật qua `server.py`/`client.py`): `NOT_STARTED`.
- Hạn chế: mô phỏng một process, độ trễ ≈ 0, `server.py`/`session.py`/`http_ui.py` chưa được kiểm thử tích hợp.

### 4.5. Câu hỏi/blocker
1. Trung (M1) có chấp nhận hợp đồng K1–K11 và áp patch không? Trung có đang sửa `connection.py` (T-M1-04) trên nhánh khác không?
2. Hàng đợi gửi `_tx_queue` không giới hạn: M1/M4 có cần giới hạn không?
3. Cài lại `cryptography` trên máy Phước để bỏ stub.

### 4.6. Việc tiếp theo
Gửi `docs/handoff/M1/` cho Trung; sau khi patch được áp dụng, chạy lại `tests.test_connection_loopback` (kỳ vọng 15 OK, 0 skip) và cập nhật Gate B.

