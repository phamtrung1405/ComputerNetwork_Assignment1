# Bàn giao cho M1 (Trung): `connection.py` đã được vá sẵn

**Người thực hiện:** Phước (M2), với sự hỗ trợ của Claude · **Ngày:** 2026-11-08
**Trạng thái:** đã áp dụng trực tiếp vào `src/transport/connection.py` theo yêu cầu của Phước; M1 xem lại và xác nhận.
**Hợp đồng:** [`docs/project/M1_M2_INTEGRATION_CONTRACT.md`](../../project/M1_M2_INTEGRATION_CONTRACT.md) (K1–K11)

`connection.patch` cũ đã bị xoá vì không còn khớp sau khi `main` được merge (Trung đã tự thêm AAD và `is_server`).

## Đã có sẵn trên `main` (Trung)

K2 (`is_server=`), K3 (header 22 B làm AAD, `payload_len` gồm tag), `verify_migration_datagram`.

## Phần được thêm vào `connection.py`

| Hợp đồng | Thay đổi |
| :-- | :-- |
| K1 | `_install_keys()` dùng `derive_session_keys`, chọn `_send_key` / `_recv_key` theo vai. `_session_key` giữ nghĩa cũ (khóa chung `derive_shared_secret`, hai phía bằng nhau) để test/mã cũ không vỡ |
| K3 | Dựng và mã hóa DATA trong `_transmit_data()` |
| K4 | Sau handshake, DATA không có `FLAG_ENCRYPTED` bị loại |
| K5 | ACK chỉ gửi sau khi giải mã thành công **và** Stream chấp nhận chunk |
| K6 | Sau ACK, rút mọi message bằng `stream.get_complete_messages()` |
| K7 | `send_packet(PKT_DATA)` đưa vào `_tx_queue`, `_flush_send_queue()` gọi từ `send_packet`, `_handle_ack`, `tick` (không chặn thread) |
| K8, K9 | `_handle_ack` / `_handle_data` bắt lỗi do dữ liệu peer, không để thoát khỏi `receive_packet` |
| K10 | `tick()` đọc `reliability.take_failed()`; có gói bỏ cuộc thì `closed = True` |
| K11 | HELLO lặp được trả lời bằng đúng HELLO_ACK cũ; client gửi lại HELLO (1 s, tối đa 5 lần) |
| (cờ) | `Connection.INTEGRATION_CONTRACT_VERSION = 1` |
| (tương thích) | `verify_migration_datagram` dùng `_recv_key`; chỉ khi chưa có khóa hai chiều (test gán tay `_session_key`) mới dùng `_session_key` |

Không đổi: chữ ký công khai của `Connection`, header `!BIIBHIHI`, `FLAG_*`, `PKT_*`.

## Đã kiểm tra

```
python -m unittest tests.test_connection_loopback   # 15 test OK, 0 skip; lặp 5 lần đều OK
python -m unittest discover -s tests -p "test_*.py" # 150 test OK
python run_all.py --check                           # sạch
```

Gồm truyền file 10 MB (khối 4096 B, mất 5% + đảo thứ tự + trùng 3%) và ma trận 2 MB × 5 kịch bản, SHA-256 khớp. Đây là mô phỏng trong một process qua socket giả; chưa chạy UDP thật qua `server.py` / `client.py`.

## Rủi ro cần M1 lưu ý

- `_tx_queue` không giới hạn kích thước; `client.py` ghi từng khối 4096 B và tự chờ `can_send()` nên không ảnh hưởng.
- Gói vượt 8 lần truyền lại làm `closed = True`; `server.py` dọn session khi `closed`. Xác nhận đó là hành vi mong muốn.
- Handshake vẫn chưa xác thực danh tính server (ngoài phạm vi).
