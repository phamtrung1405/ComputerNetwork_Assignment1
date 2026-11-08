# STREAMCHAT — Hợp đồng tích hợp M1 (`connection.py`) ↔ M2 (`crypto` / `reliability` / `stream`)

**Trạng thái:** `connection.py` đã được vá theo K1–K11 (2026-11-08, theo yêu cầu của Phước); chờ M1 (Trung) xác nhận. K2/K3 đã do M1 làm trước. Xem `docs/handoff/M1/README.md`.
**Căn cứ:** `docs/audits/M2_AUDIT_REPORT.md` (các mã C-1…H-6), ADR-005, ADR-006, ADR-010, ADR-011, ADR-012.
**Phạm vi sửa:** chỉ `src/transport/connection.py` (M1). API M2 đã sẵn sàng; không cần đổi chữ ký hàm nào.

---

## 1. Quy tắc bắt buộc cho `connection.py`

| #   | Quy tắc                                                                                                                                                                                                                                                                      | API M2                                       | Lỗi audit     |
| :-- | :--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | :------------------------------------------- | :------------ |
| K1  | Dẫn xuất hai khóa hai chiều: `client_write_key, server_write_key = derive_session_keys(priv, peer_pub)`. Gửi bằng khóa ghi của **vai mình**; nhận bằng khóa ghi của **vai đối tác**. Client: gửi `client_write_key`, nhận `server_write_key`. Server: ngược lại.             | `crypto.derive_session_keys`                 | C-1           |
| K2  | Truyền `is_server=` là vai của **người gửi**: khi `encrypt` là `self.is_server`; khi `decrypt` là `not self.is_server`.                                                                                                                                                      | `encrypt/decrypt(..., is_server=)`           | C-1           |
| K3  | Header là AAD. Người gửi: đặt `flags \|= FLAG_ENCRYPTED`, dựng header với `payload_len = len(plaintext) + GCM_TAG_SIZE`, gọi `encrypt(..., associated_data=header)`, gói `header + ciphertext`. Người nhận: truyền đúng `raw_bytes[:HEADER_SIZE]` nhận được làm AAD.         | `encrypt/decrypt(associated_data=)`          | C-2           |
| K4  | Sau khi `_session_key` đã có, gói DATA **không** có `FLAG_ENCRYPTED` phải bị loại bỏ (không ACK, không giao lên app).                                                                                                                                                        | —                                            | C-3           |
| K5  | Thứ tự xử lý DATA: unpack → kiểm tra cờ/khóa → `decrypt` thành công → `stream.read_incoming` được chấp nhận (không ném `ValueError`) → **mới** `_send_ack` → giao message. Giải mã lỗi hoặc Stream từ chối thì không ACK (bên gửi sẽ truyền lại, không mất dữ liệu âm thầm). | —                                            | H-1 (ADR-010) |
| K6  | Sau `read_incoming`: `for m in stream.get_complete_messages(): on_app_message(stream_id, m)`. Bỏ qua giá trị trả về của `read_incoming` để không giao hai lần.                                                                                                               | `Stream.get_complete_messages`               | C-4 (ADR-011) |
| K7  | Chỉ ghi DATA ra socket khi `reliability.can_send()` là `True`. **Không chặn thread**: DATA vào hàng đợi `_tx_queue`, được đẩy ra (cấp seq, mã hóa, `mark_sent`, gửi) trong `send_packet`, sau `process_sack` ở `_handle_ack` và trong `tick()`. Không chờ bên trong `send_packet`: server nhận và gửi trên cùng một thread nên sẽ tự làm nghẽn việc xử lý ACK. | `ReliabilityManager.can_send` | H-2 |
| K8  | `_handle_ack`: bắt `ValueError, TypeError, IndexError, AttributeError` quanh `process_sack`, kiểm tra `ranges` là list; bỏ gói lỗi, không để ngoại lệ thoát khỏi `receive_packet`.                                                                                           | `process_sack` (vẫn ném lỗi khi dữ liệu sai) | H-6           |
| K9  | `read_incoming` có thể ném `ValueError` (tràn bộ đệm, chunk chồng lấn, vượt số chunk): bắt, log, bỏ gói; không để thoát khỏi `receive_packet`.                                                                                                                               | `Stream.read_incoming`                       | H-6           |
| K10 | `tick()`: sau `get_retransmits()`, gọi `reliability.take_failed()`; nếu khác rỗng thì log và xử lý (đóng kết nối hoặc báo app).                                                                                                                                              | `ReliabilityManager.take_failed` (mới)       | H-5           |
| K11 | HELLO lặp: server bỏ qua HELLO nếu đã có `_session_key` cho cùng `conn_id`; client gửi lại HELLO khi chưa nhận HELLO_ACK (T-M1-04).                                                                                                                                          | —                                            | M-6           |

## 2. Điểm kỹ thuật dễ sai

- **AAD khớp từng byte:** `payload_len` trong header người gửi dựng phải bằng độ dài ciphertext (plaintext + 16). Nếu dựng header bằng độ dài plaintext thì mọi gói giải mã thất bại.
- **Retransmit không mã hóa lại:** `ReliabilityManager` lưu `raw_data` đã mã hóa; `get_retransmits()` trả đúng các byte đó. Không gọi `encrypt` lần nữa (sẽ tạo nonce mới và đổi ciphertext, hoặc ném `Nonce reuse`).
- **Đổi khóa theo vai:** nếu đảo `is_server` ở phía nhận, mọi gói bị `InvalidTag`; test hai chiều trong `tests/test_connection_loopback.py` bắt lỗi này.
- **Không chặn trong `send_packet`:** server xử lý nhận và gửi trên cùng thread `selectors`; chờ `can_send()` trong `send_packet` sẽ khiến ACK không được xử lý và cửa sổ không bao giờ mở. Vì vậy K7 dùng hàng đợi. Hàng đợi không giới hạn kích thước; ứng dụng muốn áp lực ngược vẫn nên kiểm tra `reliability.can_send()` như `client.py:217`.
- **Seq và nonce chỉ cấp khi gửi thật:** `_alloc_seq` được gọi trong `_transmit_data`, không phải lúc enqueue, nên nonce luôn duy nhất theo thứ tự ghi ra socket.
- **Idempotent HELLO:** nhận lại HELLO không được sinh lại cặp khóa của server (hiện làm lệch khóa hai phía, đã tái hiện bằng probe P13).

## 3. Thay đổi phía M2 đã thực hiện (không đổi chữ ký hiện có)

| Thay đổi                                                                                                                                                                           | File             | Ghi chú                                                                                               |
| :--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | :--------------- | :---------------------------------------------------------------------------------------------------- |
| Fast Convergence dùng `(1+β)/2 = 0.85` (RFC 9438 §4.7)                                                                                                                             | `reliability.py` | Trước đây 0.65                                                                                        |
| Chặn `target ≤ 1.5·cwnd` (RFC 9438 §4.2)                                                                                                                                           | `reliability.py` |                                                                                                       |
| Gói vượt `max_retransmits` rời `_unacked`; thêm `take_failed()`                                                                                                                    | `reliability.py` | `failed_packets` vẫn là bản tích lũy                                                                  |
| Từ chối chunk chồng lấn; chunk trùng offset giữ bản đến trước; giới hạn `max_buffered_chunks` (mặc định 65536, tham số mới có default)                                             | `stream.py`      | `Stream(...)` và `read_incoming(...)` giữ nguyên chữ ký tương thích ngược                             |
| SACK-based fast retransmit: gói có ≥ 3 gói seq cao hơn đã được ACK được truyền lại ngay; không backoff RTO; vẫn tối đa một lần giảm cwnd mỗi loss epoch (RFC 6675 / RFC 5681 §3.2) | `reliability.py` | Cần thiết khi K7 bật: nếu không, mất gói chỉ được phát hiện bằng RTO và 2 MB/5% mất mất 26 s (xem §3b) |

## 3b. Hiệu năng quan sát được khi K7 có hiệu lực

Mạng giả, 2 MB, độ trễ ≈ 0 (cùng một process; số liệu chỉ để so sánh tương đối):

| Điều kiện            | Chỉ có RTO                           | Có fast retransmit |
| :------------------- | :----------------------------------- | :----------------- |
| Mất 5%, khối 4096 B  | 26.5 s                               | 0.8 s              |
| Mất 10%, khối 4096 B | > 100 s, chưa xong (495/512 message) | 3.2 s              |

## 4. Tiêu chí chấp nhận

`tests/test_connection_loopback.py` (xem file) phải xanh sau khi M1 áp dụng K1–K11: nonce không trùng hai chiều, AAD bị sửa bị từ chối, DATA thô bị loại, gói hỏng không ACK, ACK lỗi không văng ngoại lệ, nhiều message cùng lúc đều tới app, `in_flight ≤ int(cwnd)`, và truyền file 10 MB có SHA-256 khớp ở mọi kịch bản mất/đảo/trùng.
