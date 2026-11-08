# Bàn giao cho M1 (Trung): patch `connection.py` tích hợp với module M2

**Người soạn:** Phước (M2), với sự hỗ trợ của Claude · **Ngày:** 2026-11-08
**File:** `connection.patch` (diff thống nhất, áp dụng cho `src/transport/connection.py`)
**Hợp đồng chi tiết:** [`docs/project/M1_M2_INTEGRATION_CONTRACT.md`](../../project/M1_M2_INTEGRATION_CONTRACT.md) (K1–K11)
**Trạng thái:** `PROPOSED`. Patch chưa được áp dụng vào repo. M1 xem xét và quyết định.

## Vì sao cần patch

Audit M2 (`docs/audits/M2_AUDIT_REPORT.md`) đo được trên pipeline thật khi `connection.py` chưa dùng API M2:

| Vấn đề                                                        | Bằng chứng                                                        |
| :------------------------------------------------------------ | :---------------------------------------------------------------- |
| Nonce AES-GCM bị dùng lại giữa hai chiều (cùng khóa, `seq=0`) | `C1 ⊕ C2 == P1 ⊕ P2`                                              |
| Header không được xác thực                                    | đổi `stream_id` 1→7, gói vẫn được giao                            |
| DATA không mã hóa vẫn được nhận sau handshake                 | gói giả mạo tới app                                               |
| Gói hỏng vẫn được ACK                                         | bên gửi không bao giờ truyền lại                                  |
| Chỉ message đầu tiên được giao                                | gửi file 10 MB theo khối 4096 B, mất 5%: **30/2560** khối tới app |
| Không thực thi `can_send()`                                   | `in_flight=7699` khi `int(cwnd)=10`                               |

## Patch làm gì (tóm tắt theo hunk)

| Hợp đồng | Thay đổi trong `connection.py`                                                                                                                             |
| :------- | :--------------------------------------------------------------------------------------------------------------------------------------------------------- |
| K1, K2   | `_install_keys()` gọi `derive_session_keys`, chọn `_send_key` / `_recv_key` theo vai; `is_server` truyền đúng cho `encrypt`/`decrypt`                      |
| K3       | `_transmit_data()` dựng header với `payload_len = len(plaintext)+16` và dùng làm AAD; `receive_packet` truyền `raw_bytes[:HEADER_SIZE]` cho `_handle_data` |
| K4       | `_handle_data` loại DATA thiếu `FLAG_ENCRYPTED` khi đã có khóa                                                                                             |
| K5       | ACK chỉ gửi sau khi giải mã thành công **và** `Stream.read_incoming` chấp nhận                                                                             |
| K6       | Sau ACK, `for msg in stream.get_complete_messages(): on_app_message(...)`                                                                                  |
| K7       | `send_packet(PKT_DATA)` đưa vào `_tx_queue`; `_flush_send_queue()` gọi từ `send_packet`, `_handle_ack`, `tick`                                             |
| K8, K9   | `_handle_ack` và `_handle_data` bắt các ngoại lệ do dữ liệu của peer                                                                                       |
| K10      | `tick()` đọc `reliability.take_failed()`; có gói bỏ cuộc thì `closed = True`                                                                               |
| K11      | HELLO lặp được trả lời bằng đúng HELLO_ACK cũ; client gửi lại HELLO khi chưa có HELLO_ACK                                                                  |
| (cờ)     | `Connection.INTEGRATION_CONTRACT_VERSION = 1` để test biết patch đã áp dụng                                                                                |

Không đổi: chữ ký công khai của `Connection` (`send_packet`, `receive_packet`, `handle_handshake`, `get_or_create_stream`, `allocate_stream`, `tick`, `closed`, `handshake_complete`), định dạng header `!BIIBHIHI`, `FLAG_*`, `PKT_*`. `_session_key` vẫn còn (bằng khóa gửi) để mã cũ kiểm tra "đã có khóa" không bị vỡ.

**Chú ý cho M3/M4:** `server.py`, `session.py`, `client.py` chỉ dùng `get_or_create_stream`, `allocate_stream`, `reliability.can_send`, `closed`, `receive_packet`, `tick`. Cả năm đều không đổi. `client.py:217` vẫn chạy được; với K7 có thể bỏ vòng chờ nếu muốn (việc của M4).

## Cách áp dụng

```bash
git apply --check docs/handoff/M1/connection.patch   # kiểm tra
git apply docs/handoff/M1/connection.patch
```

Nếu `core.autocrlf=true` (Windows) làm file thành CRLF thì dùng `git -c core.autocrlf=false apply …`. Repo đang lưu LF.

Nếu `connection.py` của M1 đã đổi nhiều (ví dụ T-M1-04, retry HELLO), dùng `K1`–`K11` trong hợp đồng làm checklist thay vì áp patch nguyên khối.

## Đã kiểm tra

- Patch được tạo từ bản `connection.py` hiện tại (HEAD, không sửa đổi): `git apply --check` và `git apply` thành công trên bản sao, kết quả giống từng byte với file đã chạy test; `git apply -R --check` cũng thành công.
- `tests/test_connection_loopback.py` chạy trên bản đã vá: **15/15 OK**, lặp **10 lần liên tiếp đều OK** (mỗi lần 21–42 s). Gồm truyền file 10 MB (khối 4096 B, mất 5% + đảo thứ tự + trùng 3%) và ma trận 2 MB × 5 kịch bản × 2 cách gửi, SHA-256 khớp.
- Trên repo **chưa vá**, cùng file test cho: 11 expected failure (đúng các lỗi nêu trên), 3 skip, 1 pass.
- Đây là mô phỏng trong một process qua socket giả; chưa chạy UDP thật qua `server.py` / `client.py`.

## Cách kiểm tra sau khi áp patch

```bash
python -m unittest tests.test_connection_loopback -v      # kỳ vọng: 15 test OK, 0 skip, 0 expected failure
python -m unittest discover -s tests -p "test_*.py"        # toàn bộ
python run_all.py --check
```

Khi patch đã áp dụng, các test `expectedFailure` tự chuyển sang bình thường và phải pass; nếu một test báo "unexpected success" nghĩa là `INTEGRATION_CONTRACT_VERSION` chưa được thêm.

## Rủi ro cần M1 lưu ý

- Hàng đợi `_tx_queue` không giới hạn kích thước (ghi một file lớn một lần sẽ giữ bản sao dữ liệu trong RAM tới khi được gửi). `client.py` ghi từng khối 4096 B và đã tự chờ `can_send()`, nên không bị ảnh hưởng.
- Khi có gói bị bỏ cuộc (vượt 8 lần truyền lại), kết nối bị đánh dấu `closed`. `server.py` hiện dọn session khi `closed`; xác nhận đó là hành vi mong muốn.
- Handshake vẫn chưa xác thực danh tính server (MITM chủ động có thể xen vào). Ngoài phạm vi patch này.
