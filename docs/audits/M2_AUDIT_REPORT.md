# M2 AUDIT REPORT — Cryptography, Reliability & Streaming

> **Phạm vi lịch sử:** các phát hiện và kết quả trong báo cáo này ghi lại trạng thái được kiểm tra ngày 2026-11-08, trước khi tích hợp K1–K11 vào `connection.py`. Tại repository ngày 2026-11-13, `Connection.INTEGRATION_CONTRACT_VERSION == 1`; bộ test hiện tại chạy 156 tests và `tests/test_connection_loopback.py` bao phủ các kiểm tra tích hợp tương ứng bằng socket mô phỏng. Không dùng các kết luận FAIL bên dưới như trạng thái hiện tại. Test loopback không xác nhận full-stack qua HTTP UI và UDP server.

**Ngày audit:** 2026-11-08 · **Nhánh:** `feat/Jaden` · **Người audit:** Claude (chế độ chỉ đọc)
**Phạm vi:** `src/common/crypto.py`, `src/transport/reliability.py`, `src/transport/stream.py` và tích hợp với `src/transport/connection.py`, `src/common/packet.py` (đọc `client.py:136-232` để xác định cách app gửi file).
**Quy tắc đã tuân thủ:** không sửa file nguồn hay test nào. Các file duy nhất được tạo là báo cáo này và script/stub trong thư mục scratchpad (xem §IV).

---

## I. Executive Summary

**Kết luận tại thời điểm audit 2026-11-08:** các hàm M2 hoạt động đúng khi gọi riêng lẻ (85/85 test pass, có điều kiện ở §IV), nhưng khi đó pipeline `Connection` + `Stream` + `ReliabilityManager` + `crypto` có lỗi tích hợp dưới mất gói hoặc đảo thứ tự. Báo cáo giữ lại các kết quả này như lịch sử; không dùng chúng làm trạng thái hiện tại của code.

Bằng chứng chạy thật (§V): file 10 MB gửi bằng cách app đang dùng (các khối 4096 B, mỗi khối là một message) chỉ chuyển được **30/2560** khối lên tầng ứng dụng khi mất gói 5% và **9/2560** khi có đảo thứ tự. Mạng sạch và đúng thứ tự thì **2560/2560, SHA-256 khớp**.

**Đã tốt**

- X25519 → HKDF-SHA256 → AES-256-GCM dùng thư viện chuẩn. Tag được kiểm tra khi giải mã. Có nonce guard và `derive_session_keys` hai chiều.
- `process_sack` kiểm tra đầu vào và chặn DoS khi dải quá lớn. Luật Karn, RTO theo RFC 6298 (công thức SRTT/RTTVAR) và backoff có test.
- `Stream.write` cắt đúng 1362 B. Gói mã hóa đầy đủ dài đúng 1400 B (đã đo, P11). Dedupe và giữ chunk của message sau đều đúng ở mức đơn vị.
- Một message 10 MB gửi một lần qua pipeline thật, mất gói 5%, đảo thứ tự và trùng 3%: **SHA-256 khớp 100%**.

**Thiếu hoặc sai nghiêm trọng**

1. Đường mã hóa thật trong `connection.py` **không dùng** các tính năng an toàn M2 đã làm: khóa hai chiều, `is_server`, AAD. Hậu quả là tái sử dụng nonce (C-1), header không được xác thực (C-2), và DATA không mã hóa vẫn được chấp nhận (C-3).
2. Hợp đồng giao message giữa `Stream` và `Connection` bị hỏng, nên message hoàn chỉnh bị bỏ sót và toàn bộ dữ liệu bị giữ lại trong RAM (C-4).
3. ACK được gửi trước khi giải mã (H-1). Không có kiểm soát luồng ở tầng Connection (H-2).
4. Thuật toán CUBIC sai ở hệ số Fast Convergence (0.65 thay vì 0.85) và thiếu chặn 1.5×cwnd (H-3, H-4).
5. Gói vượt `max_retransmits` chiếm cửa sổ vĩnh viễn (H-5).

Nhiều lỗi nằm ở call-site của M1 (`connection.py`). Báo cáo ghi rõ chủ sở hữu từng mục.

---

## II. Requirement Checklist

Trạng thái: **PASS** (đúng + có bằng chứng) · **PARTIAL** · **FAIL** · **NOT VERIFIED**. "Test" nghĩa là chạy thật trong phiên này.

### A. Cryptography

| #   | Yêu cầu                                      | TT                  | Bằng chứng / vị trí                                                                                                                                     | Giải thích                                                                                                                                                      |
| :-- | :------------------------------------------- | :------------------ | :------------------------------------------------------------------------------------------------------------------------------------------------------ | :-------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| A1  | ECDH X25519                                  | PASS                | `crypto.py:96-114`, `:117-168`; `test_crypto.TestKeyAgreement` + `test_integration.TestEndpointKeyAgreement` pass                                       | Khóa raw 32 B, kiểm tra độ dài và kiểu.                                                                                                                         |
| A2  | HKDF dẫn xuất khóa                           | PASS (module)       | `crypto.py:161-168`, `:207-223`                                                                                                                         | HKDF-SHA256, `info` khác nhau cho hai chiều. **Nhưng** `connection.py:364,406` gọi `derive_shared_secret` (một khóa), không gọi `derive_session_keys`. Xem C-1. |
| A3  | AES-GCM đúng                                 | PASS (module)       | `crypto.py:293-295`, `:354-355`                                                                                                                         | `AESGCM` từ `cryptography`, tag 16 B gắn cuối.                                                                                                                  |
| A4  | Nonce duy nhất / cùng key                    | **FAIL (tích hợp)** | `crypto.py:51-89` đúng khi truyền `is_server`; `connection.py:192,285` **không truyền** (P2)                                                            | C-1.                                                                                                                                                            |
| A5  | Tag được xác minh khi decrypt                | PASS                | `crypto.py:355`; `test_tampered_*` pass                                                                                                                 | `InvalidTag` bắn ra.                                                                                                                                            |
| A6  | Xử lý sai key/nonce/ciphertext đổi/auth fail | PARTIAL             | Module đúng (test pass). `connection.py:287-289` bắt `Exception` và bỏ gói, **nhưng đã ACK trước** (H-1, P3).                                           | Gói hỏng bị ACK, bên gửi không bao giờ gửi lại.                                                                                                                 |
| A7  | Không hardcode key/secret                    | PASS                | Đọc toàn bộ `crypto.py`: không có khóa cố định; khóa sinh từ `X25519PrivateKey.generate()`                                                              | Chưa quét toàn repo.                                                                                                                                            |
| A8  | Tích hợp handshake M2↔M1                     | **PARTIAL**         | Handshake chạy, hai bên cùng khóa (P1). Nhưng khóa dùng chung một chiều (C-1), HELLO lặp làm lệch khóa (M-6, P13), không có xác thực danh tính đối tác. |                                                                                                                                                                 |
| A9  | Header bind vào AAD (ADR-006)                | **FAIL (tích hợp)** | `connection.py:192,285` không truyền `associated_data`. P4: đổi `stream_id` 1→7 được chấp nhận.                                                         | C-2.                                                                                                                                                            |
| A10 | Chỉ nhận DATA đã xác thực                    | **FAIL**            | `connection.py:282-289`; P12                                                                                                                            | C-3.                                                                                                                                                            |

### B. Reliability & Congestion Control

| #   | Yêu cầu                                 | TT                  | Bằng chứng / vị trí                                                                                                         | Giải thích                                                                                                                                                                                                    |
| :-- | :-------------------------------------- | :------------------ | :-------------------------------------------------------------------------------------------------------------------------- | :------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| B1  | `process_sack` đúng block/seq           | PASS                | `reliability.py:143-221`; `TestSackProcessing` (6), `TestDuplicate…` pass                                                   | Chỉ gỡ gói nằm trong dải; gap giữ nguyên.                                                                                                                                                                     |
| B1b | Có đúng RFC 2018 không                  | PARTIAL             | `connection.py:302-320`: mỗi gói đến sinh một ACK `[[seq,seq]]`, header `seq_num=0`                                         | Không có cumulative ACK point, không có danh sách block (≤4). Đây là ACK từng gói chứ không phải SACK theo RFC 2018 (RFC 2018 là TCP option). Chấp nhận như một thích ứng, nhưng cần ghi nhận trong tài liệu. |
| B2  | `get_retransmits` xác định gói mất      | PASS (module)       | `reliability.py:225-274`; `TestRetransmissionSelection` pass                                                                | Chỉ chọn gói quá RTO.                                                                                                                                                                                         |
| B3  | Không retransmit gói đã ACK             | PASS                | `test_acknowledged_packet_never_retransmitted`                                                                              |                                                                                                                                                                                                               |
| B4  | `update_rtt` đúng công thức             | PASS                | `reliability.py:278-326`; `TestRttEstimationRfc6298` (5)                                                                    | Thứ tự RTTVAR trước SRTT, α=1/8, β=1/4 đúng RFC 6298 §2.                                                                                                                                                      |
| B5  | Mẫu RTT không hợp lệ / mơ hồ retransmit | PASS                | `reliability.py:205` (Karn), `:306-307` (âm bị từ chối)                                                                     |                                                                                                                                                                                                               |
| B5b | RTO tối thiểu                           | PARTIAL             | `constants.py` `MIN_RTO=0.2`; RFC 6298 §2.4 khuyến nghị làm tròn lên ≥ 1 s (SHOULD)                                         | Chủ ý thích ứng cho LAN, nhưng module docstring chỉ nói về granularity G, không nêu điều này.                                                                                                                 |
| B6  | CUBIC thật hay chỉ AIMD                 | PARTIAL             | `reliability.py:330-430` có K, C, β, epoch, W_cubic, vùng TCP-friendly, Fast Convergence                                    | Là CUBIC thật, nhưng sai chi tiết: H-3, H-4, M-1, M-2.                                                                                                                                                        |
| B7  | Giới hạn cwnd + xử lý loss/timeout      | PARTIAL             | `MIN_CWND`/`MAX_CWND` kẹp đúng (`:430`); 1 lần giảm/RTT đúng (ADR-009, `:269-272`, test pass). Timeout chỉ giảm ×0.7 (M-1). |                                                                                                                                                                                                               |
| B8  | Phân biệt congestion với flow control   | **FAIL (tích hợp)** | `can_send()` đúng (`:434-441`) nhưng `connection.py:170-209` không gọi (grep `can_send` chỉ có `client.py:217`)             | H-2. P7: sau `write()`, `in_flight=7699` trong khi `int(cwnd)=10`.                                                                                                                                            |

**Đối chiếu RFC.** RFC 9438 đã xác nhận trực tiếp từ `rfc9438.txt` trong phiên này: §4.2 (target kẹp [cwnd, 1.5·cwnd]), §4.7 (Fast Convergence là cwnd·(1+β)/2), §4.8 (timeout), §4.10 (W_max khi thoát slow start không mất gói), §5.1 (C=0.4). RFC 9438 **obsoletes RFC 8312**, nhưng code, docstring, ADR và test đều trích RFC 8312. RFC 6298 và RFC 2018 đối chiếu theo hiểu biết, **chưa tải lại** trong phiên này.

### C. Streaming & Reassembly

| #   | Yêu cầu                                                  | TT                  | Bằng chứng / vị trí                                                                                                                                                                        | Giải thích                                                    |
| :-- | :------------------------------------------------------- | :------------------ | :----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | :------------------------------------------------------------ |
| C1  | `write()` chia fragment theo MTU                         | PASS                | `stream.py:34,86-135`; P11: gói mã hóa 1362 B → đúng 1400 B                                                                                                                                |                                                               |
| C2  | Tính header + tag vào payload                            | PASS                | `stream.py:34` = 1400−22−16; `constants.MAX_APP_PAYLOAD` đã là 1362                                                                                                                        | README và `PROJECT_CONTEXT.md` từng ghi 1378; đã được cập nhật sau audit. |
| C3  | Offset đủ định vị                                        | PASS                | `stream.py:128,130`; header có `offset` 32-bit                                                                                                                                             |                                                               |
| C4  | `_try_assemble` ghép đúng theo offset                    | PASS (đơn vị)       | `stream.py:237-285`; `TestStreamReadAndReassembly`                                                                                                                                         |                                                               |
| C5  | Đảo thứ tự / trùng / mất / đến muộn                      | PARTIAL             | Đơn vị đúng. **Tích hợp sai** vì C-4: message hoàn chỉnh thứ 2 trở đi không bao giờ tới app.                                                                                               |                                                               |
| C6  | Phát hiện overlap / gap / dữ liệu sai                    | **FAIL**            | P10: chunk cùng offset ghi đè (`GOOD`→`EVIL`); chunk chồng lệch offset bị bỏ byte âm thầm (`AAAAAAAACC`); offset xa được nhận không kiểm tra                                               | M-3.                                                          |
| C7  | Không ghi đè dữ liệu đã nhận                             | PARTIAL             | Phần đã giao thì an toàn (`:185-195`). Phần đang đệm bị ghi đè (xem C6).                                                                                                                   |                                                               |
| C8  | Giới hạn bộ nhớ                                          | PARTIAL             | Có trần 16 MB (`:37,209`) nhưng chỉ đếm byte payload. Số khóa `_recv_buffer`/`_eom_offsets` không bị giới hạn. Sau đó `ValueError` không được `Connection` bắt (P10; `connection.py:293`). | M-3, H-6.                                                     |
| C9  | Truyền file lớn dạng stream, không giữ cả file trong RAM | **FAIL (tích hợp)** | P7 (mạng sạch): receiver giữ **2560 message / 10 485 760 B** trong `_complete_messages` vì không ai gọi `get_complete_messages()` (grep xác nhận)                                          | C-4.                                                          |

### D. Integration

| #   | Yêu cầu                                                            | TT       | Bằng chứng                                                                                                                          |
| :-- | :----------------------------------------------------------------- | :------- | :---------------------------------------------------------------------------------------------------------------------------------- |
| D1  | Interface crypto/reliability/stream ↔ Connection                   | **FAIL** | C-1..C-4                                                                                                                            |
| D2  | Handshake → send → recv → decrypt → SACK → retransmit → reassembly | PARTIAL  | Chạy được nếu không mất gói và đúng thứ tự (P7: 0% loss, no reorder → pass). Hỏng khi có mất gói/đảo thứ tự với message nhiều khối. |
| D3  | Nhất quán packet format / seq / offset / MTU / overhead            | PASS     | P11; header 22 B (`packet.py:22-25` có assert)                                                                                      |
| D4  | Module chạy riêng nhưng không tích hợp được                        | CÓ       | C-1, C-2, C-4, H-2                                                                                                                  |

---

## III. Bug & Risk Analysis

Ghi chú: **[Đã xác nhận]** = đã tái hiện bằng probe; **[Code]** = chỉ chứng minh bằng đọc code; **[Nguy cơ]** = chưa tái hiện.

### CRITICAL

**C-1 · Tái sử dụng nonce AES-GCM trên đường mã hóa thật** [Đã xác nhận, P2] — chủ sở hữu: M1 (call-site); M2 đã có API đúng

- `connection.py:364,406`: cả hai phía dùng một `session_key` từ `derive_shared_secret`.
- `connection.py:192` và `:285` gọi `encrypt/decrypt(payload, key, seq)` không có `is_server`, nên nonce = `seq` ở cả hai chiều. `_alloc_seq` bắt đầu từ 0 ở mỗi phía.
- Bằng chứng: mã hóa hai plaintext với `seq=0` bằng đúng lệnh gọi của `send_packet` cho **C1⊕C2 == P1⊕P2 = True**. Đây là rò rỉ plaintext và mở đường giả mạo tag.
- Nonce guard (`crypto.py:38,281-290`) chỉ có hiệu lực trong **một process**. Trong một process nó làm `send_packet` văng `ValueError` ở chiều thứ hai (P2). Client và server là hai process khác nhau nên guard không bắt được.
- Hướng sửa (chưa áp dụng, cần M1): dùng `derive_session_keys`, truyền `is_server=self.is_server`, chọn đúng khóa theo chiều (ADR-005).

**C-2 · Header không được xác thực (ADR-006 chưa tích hợp)** [Đã xác nhận, P4] — M1

- `connection.py:192,285` không truyền `associated_data`. Đổi `stream_id` 1→7 trên đường truyền: gói vẫn được giải mã và giao cho app với `stream_id=7`.
- Cùng lỗ hổng cho `offset`, `flags`, `seq_num` (kẻ tấn công trên đường có thể trộn dữ liệu giữa các stream hoặc xáo offset).
- Lưu ý khi sửa: AAD phải là 22 B header **ở dạng người gửi tạo ra**. Hiện `payload_len` được tính trong `packet.pack` sau khi mã hóa, nên cần thiết kế thứ tự dựng header cẩn thận.

**C-3 · DATA không mã hóa vẫn được chấp nhận sau handshake** [Đã xác nhận, P12] — M1

- `connection.py:282-289`: chỉ giải mã khi `flags & FLAG_ENCRYPTED`. Nếu cờ tắt, payload dùng nguyên văn.
- Bằng chứng: sau handshake, `PKT_DATA` thô trên stream 0 được `on_app_message` nhận. Kẻ tấn công không cần khóa để chèn tin nhắn giả.
- Hướng sửa: khi `_session_key` đã có, DATA thiếu `FLAG_ENCRYPTED` phải bị loại bỏ.

**C-4 · Hợp đồng giao message `Stream` ↔ `Connection` bị hỏng** [Đã xác nhận, P6/P7] — liên quan M2 (`stream.py:226-235`) và M1 (`connection.py:293-300`)

- `read_incoming` có thể ghép nhiều message cùng lúc nhưng **chỉ trả message đầu** (`first_assembled`); phần còn lại nằm trong `_complete_messages`. `Connection._handle_data` chỉ dùng giá trị trả về, và `get_complete_messages()` không được gọi ở đâu ngoài `stream.py`.
- Hậu quả quan sát được (10 MB, các khối 4096 B như `client.py:205-210`, `client.py` là M4):

  | Điều kiện mạng                 | Message tới app / kỳ vọng | SHA-256 |
  | :----------------------------- | :------------------------ | :------ |
  | Sạch, đúng thứ tự              | 2560 / 2560               | khớp    |
  | Mất 5%, không đảo, không trùng | **30 / 2560**             | sai     |
  | Mất 5% + đảo + trùng 3%        | **7 / 2560**              | sai     |
  | Mất 0% + đảo + trùng 3%        | **9 / 2560**              | sai     |

  Dữ liệu **được ghép đúng** trong `_complete_messages` (SHA-256 của phần nối lại khớp bản gốc) nhưng không bao giờ tới app.

- Tác dụng phụ: `_complete_messages` không bao giờ được dọn, nên mọi message nhận được đều bị giữ lại trong RAM (ví dụ 10 MB khi nhận file 10 MB).
- Test hiện tại không bắt được vì chỉ gọi `Stream` trực tiếp và lấy giá trị trả về của message cuối (xem §IV).
- Cần quyết định interface giữa M1 và M2 (ví dụ `Connection` gọi `get_complete_messages()` sau mỗi `read_incoming`, hoặc `Stream` nhận callback). Chưa thay đổi gì.

### HIGH

**H-1 · ACK gửi trước khi giải mã/xác thực (ADR-010 vẫn PROPOSED)** [Đã xác nhận, P3] — M1 (`connection.py:278`)
Gói bị sửa 1 byte vẫn được ACK. Bên gửi gỡ nó khỏi `_unacked` (`in_flight=0`) và **không bao giờ gửi lại**; bên nhận bỏ payload. Mất dữ liệu vĩnh viễn, và kẻ tấn công có thể chủ động làm mất dữ liệu.

**H-2 · Không thực thi `can_send()` ở tầng Connection** [Đã xác nhận, P7] — M1 (`connection.py:170-209`)
Một `write()` 10 MB đẩy 7699 gói ra socket trong khi `int(cwnd)=10`. App client có vòng chờ riêng (`client.py:217`) nhưng chỉ kiểm tra **sau** mỗi `write()` 4096 B (4 gói), và phía server không có. CUBIC/RTO không thực sự điều tiết lưu lượng.

**H-3 · Hệ số Fast Convergence sai** [Đã xác nhận, P9] — M2 (`reliability.py:375`)
Code dùng `cwnd·(2−β)/2 = 0.65·cwnd`. RFC 9438 §4.7 (và RFC 8312 §4.6) là `cwnd·(1+β)/2 = 0.85·cwnd`. Docstring (`:336-337`) ghi đúng 0.85. **Test `test_fast_convergence` (`tests/test_reliability.py:426-436`) khẳng định giá trị 0.65 nên đang che lỗi này.**

**H-4 · Thiếu chặn target ≤ 1.5×cwnd (RFC 9438 §4.2)** [Đã xác nhận, P9] — M2 (`reliability.py:421-428`)
Một ACK sau epoch dài 30 s làm cwnd 20 → 562.7 (RFC cho tối đa ≈ 30). Khi chạy thật cwnd đã tăng lên 156 chỉ sau 0.3 s trên mạng sạch (P7).

**H-5 · Gói vượt `max_retransmits` chiếm cửa sổ vĩnh viễn và không được báo cáo** [Đã xác nhận, P8] — M2 (`reliability.py:252-254`)
Gói được thêm vào `failed_packets` nhưng không bị gỡ khỏi `_unacked`. Nó tiếp tục tính vào `in_flight`, nên với `cwnd=2` và 2 gói hỏng thì `can_send()` là `False` mãi (client rơi vào vòng chờ vô hạn ở `client.py:217`). Không module nào đọc `failed_packets` (grep xác nhận), nên không có tín hiệu lỗi truyền về app.

**H-6 · Dữ liệu sai/hỏng của peer làm văng ngoại lệ ra ngoài `receive_packet`** [Đã xác nhận, P5; nguy cơ với server] — M1
`_handle_ack` (`connection.py:261-268`) chỉ bắt `JSONDecodeError`, `UnicodeDecodeError`, `KeyError`. Các payload `[[-1,2]]`, `[[5,1]]`, `[[1]]`, `[["a",2]]`, `[[1.5,2]]`, `{"ranges":5}`, `null` đều làm ngoại lệ thoát khỏi `receive_packet`. Tương tự `read_incoming` có thể bắn `ValueError` khi vượt buffer (`connection.py:293`, P10). Chưa đọc `server.py` nên **chưa xác nhận** ảnh hưởng tới event loop server. ACK cũng không được xác thực, nên có thể bị giả mạo (kẻ tấn công biết `conn_id` có thể ACK hộ để làm bên gửi bỏ gói).

### MEDIUM

- **M-1 Phản ứng timeout:** timeout chỉ giảm cwnd ×0.7 (40→28, P9). RFC 9438 §4.8 yêu cầu theo Reno (RFC 5681 §3.1: cwnd về 1 SMSS khi RTO) nhưng đặt ssthresh bằng β_cubic. Module docstring không nêu đây là chủ ý.
- **M-2 Khởi tạo CA khi chưa có mất gói:** `_w_max` giữ giá trị khởi tạo 10 trong khi cwnd=64, K=0 (P9). RFC 9438 §4.10 đặt W_max = cwnd tại thời điểm vào CA. Vùng "TCP-friendly" dùng công thức đóng của RFC 8312 (`W_max·β + …`), trong khi RFC 9438 §4.3 định nghĩa W_est khởi đầu bằng `cwnd_epoch`, cập nhật theo từng ACK. Code đang theo bản RFC đã bị thay thế.
- **M-3 Stream không phát hiện overlap/gap:** ghi đè chunk cùng offset (P10), bỏ byte của chunk chồng lệch, không từ chối offset xa. Trần 16 MB chỉ đếm byte payload, nên nhiều chunk 1 byte có thể làm bộ nhớ thực lớn hơn nhiều so với 16 MB (**[Nguy cơ]**, chưa đo). `_eom_offsets` không bị giới hạn.
- **M-4 ACK không phải RFC 2018 SACK** (xem B1b) và không có phát hiện mất gói bằng dupACK/SACK. Chỉ dựa vào RTO (docstring `reliability.py:24-25` có ghi).
- **M-5 `_used_nonces` tăng không giới hạn** (`crypto.py:38`): mỗi gói thêm một nonce vào set cho đến khi tiến trình thoát. File rất lớn hoặc kết nối dài sẽ tăng bộ nhớ. **[Code]**
- **M-6 HELLO lặp làm lệch khóa** [Đã xác nhận, P13] — M1: server sinh lại keypair và khóa cho mỗi HELLO nhận được (`connection.py:363-364`), sau đó hai phía có khóa khác nhau. HELLO không được gửi lại khi mất (T-M1-04). Không có xác thực danh tính server nên MITM chủ động có thể xen vào (hạn chế thiết kế, ngoài phạm vi M2).
- **M-7 Kẹt đuôi do RTO phình:** truyền 10 MB mất 5% mất 24.3 s trong mô phỏng độ trễ ≈ 0; RTO kết thúc ở 10.0 s (MAX_RTO) vì các gói retransmit không cho mẫu RTT (Karn). Đúng RFC 6298 §5.5 nhưng ảnh hưởng thời gian hoàn tất.

### LOW

- `_consecutive_timeouts` được ghi nhưng không được đọc ở đâu cả (`reliability.py:101,262,326`).
- `_alloc_seq` không kiểm tra tràn uint32 (`connection.py:108-117`).
- `encrypt(..., is_server=None)` giữ nonce hướng đơn làm mặc định; an toàn hơn nếu buộc truyền rõ hướng khi có `session_key`.
- Tại thời điểm audit: `README.md` (WebSocket, 1378), `PROJECT_CONTEXT.md` §6-7 (danh sách lỗi đã sửa); các tài liệu trạng thái đã được cập nhật sau audit.
- `read_incoming` chấp nhận chunk lớn hơn 1362 B (P10). Không phải lỗi bảo mật, chỉ là thiếu kiểm tra đầu vào.

---

## IV. Test Coverage

### Môi trường và cách chạy

- `python -m unittest discover -s tests -p "test_*.py"` **không chạy được nguyên trạng**: 2 module (`test_crypto`, `test_integration`) lỗi import.
- **Nguyên nhân (đã xác nhận):** file ngoài repo `C:\Users\mhp23\AppData\Roaming\Python\Python313\site-packages\cryptography\hazmat\primitives\asymmetric\rsa.py` dòng 16 có `from Buoi6.src.GenerateKey import gene` (sửa lúc 2026-11-04 13:53). Không có đoạn nào khác trong file dùng `gene`. Không phải lỗi mã nguồn dự án. Khuyến nghị cài lại: `pip install --force-reinstall cryptography`. Tôi **không** sửa file này.
- Để chạy audit, tôi tạo stub rỗng `Buoi6` trong scratchpad và đặt vào `PYTHONPATH` chỉ cho các lệnh chạy. Stub chỉ đáp ứng một import không dùng đến, không thay đổi hành vi `cryptography`.

### Kết quả chạy thật

| Lệnh                                                               | Kết quả thực tế                                            |
| :----------------------------------------------------------------- | :--------------------------------------------------------- |
| `python -m unittest discover -s tests -p "test_*.py"` (không stub) | `Ran 55 tests … FAILED (errors=2)` (2 lỗi import nêu trên) |
| Cùng lệnh, có stub `Buoi6`                                         | `Ran 85 tests in 0.226s … OK`                              |
| `audit_probe.py` P1–P13                                            | xem §III và §V                                             |

### Test hiện có và độ tin cậy

| File                                                            | Phủ                                               | Hạn chế                                                                                                                    |
| :-------------------------------------------------------------- | :------------------------------------------------ | :------------------------------------------------------------------------------------------------------------------------- |
| `test_crypto.py` (+ phần `test_integration`: 30 test cùng nhau) | keygen, HKDF, roundtrip, tamper, AAD, nonce guard | Kiểm tra `crypto.py` đứng riêng; không có test nào gọi `Connection`                                                        |
| `test_reliability.py` (37 theo tài liệu M2)                     | SACK, RTO, Karn, backoff, CUBIC                   | `test_fast_convergence` khẳng định 0.65 (sai, H-3). Không test 1.5×cwnd. Không test `failed_packets` ảnh hưởng `can_send`. |
| `test_stream.py`                                                | write, reassembly, dedupe, overflow               | Không test overlap/gap; không test giao nhiều message cùng lúc qua `Connection`                                            |
| `test_integration.py`                                           | handshake qua UDP loopback, 10 MB SHA-256         | Xem bên dưới                                                                                                               |

**Hạn chế của test 10 MB hiện có** (`test_integration.py:254-359`): không dùng `Connection`; nonce theo `derive_shared_secret` + `seq` đơn hướng; ACK mô phỏng gọi `process_sack` trực tiếp; không đảo thứ tự, không gói trùng; **ép `sent_time` về quá khứ** (`:335-336`) nên RTO/backoff thật không được thực hiện; không kiểm soát luồng; gửi 10 MB thành **một** message (khác cách app gửi các khối 4096 B).

### Test còn thiếu (đề xuất, chưa viết)

1. Test `Connection`↔`Connection` hai chiều qua socket giả: nonce không trùng giữa hai chiều, AAD bị sửa phải bị từ chối, DATA không mã hóa bị loại, gói hỏng không được ACK.
2. Test nhiều message hoàn chỉnh trong một `read_incoming` có tới được app (P6) và test không rò rỉ `_complete_messages`.
3. Test fast convergence = 0.85; test chặn 1.5×cwnd; test timeout; test `failed_packets` giải phóng cửa sổ hoặc báo lỗi.
4. Test ACK sai định dạng không làm văng ngoại lệ ra ngoài `receive_packet`.
5. Test overlap/gap/offset xa ở `Stream`; test số khóa buffer bị giới hạn.
6. Test file lớn có mất gói > 5% (mục tiêu Phase 7: 10%, 50 MB+) với RTO thật (không ép `sent_time`).

---

## V. 10 MB File Transfer Acceptance Test

**Đã chạy thật** bằng `audit_probe.py pipe …`: hai `Connection` thật (client/server) nối qua socket giả có mất gói, đảo thứ tự và trùng gói (seed 42). Dữ liệu ngẫu nhiên `os.urandom(10 MiB)`, SHA-256 tính trên bytes. Đây là **mô phỏng trong một process**, không phải UDP thật và độ trễ ≈ 0; không dùng `client.py`/`server.py`.

| Điều kiện              | Kịch bản gửi       | Kết quả                                        | SHA-256        |
| :--------------------- | :----------------- | :--------------------------------------------- | :------------- |
| Mất 5%, đảo, trùng 3%  | 1 message 10 MB    | 1/1 message, 10 485 760 B, 24.3 s, 802 gói mất | **Khớp**       |
| Mạng sạch, đúng thứ tự | 2560 khối × 4096 B | 2560/2560                                      | **Khớp**       |
| Mất 5%, đúng thứ tự    | 2560 khối × 4096 B | 30/2560                                        | **Không khớp** |
| Mất 5%, đảo, trùng 3%  | 2560 khối × 4096 B | 7/2560                                         | **Không khớp** |
| Mất 0%, đảo, trùng 3%  | 2560 khối × 4096 B | 9/2560                                         | **Không khớp** |

Đánh giá theo tiêu chí đề bài:

| Tiêu chí                                        | TT                                                           | Nhận xét                                                                                                                        |
| :---------------------------------------------- | :----------------------------------------------------------- | :------------------------------------------------------------------------------------------------------------------------------ |
| Tạo file gốc 10 MB, tính SHA-256                | Đã chạy                                                      |                                                                                                                                 |
| Truyền qua toàn bộ pipeline                     | **PARTIAL**                                                  | Có `Connection`, `Stream`, `Reliability`, `crypto`. Không có UDP thật, `client.py`/`server.py`, hay trễ mạng.                   |
| Mô phỏng mất, đảo, trùng, retransmission        | Đã chạy                                                      | Retransmit hoạt động (unacked về 0, `failed_packets=0` ở mọi lần chạy).                                                         |
| Lắp ráp tại đích, SHA-256 khớp                  | **PASS với 1 message, FAIL với khối 4096 B (cách app dùng)** | C-4.                                                                                                                            |
| Không mất/lặp/đảo byte                          | **FAIL** ở kịch bản khối                                     | Dữ liệu ghép đúng trong `_complete_messages` nhưng không giao lên app.                                                          |
| Không crash / không vượt bộ nhớ không kiểm soát | **PARTIAL**                                                  | Không crash. Giữ lại toàn bộ 10 MB trong RAM (C-4); message đơn > 16 MB sẽ bị `ValueError` không bắt được. Chưa đo đỉnh bộ nhớ. |
| Test riêng cho lỗi từng module                  | PARTIAL                                                      | Mục §IV.                                                                                                                        |

Chưa thực hiện: UDP thật qua `server.py`/`client.py` (Gate 3), file 50 MB+, mất 10%, đo bộ nhớ.

---

## VI. Remaining Tasks (ưu tiên + phụ thuộc)

| #   | Việc                                                                                                                                                         | Chủ        | Phụ thuộc                                    |
| :-- | :----------------------------------------------------------------------------------------------------------------------------------------------------------- | :--------- | :------------------------------------------- |
| 1   | Sửa môi trường: cài lại `cryptography` để test chạy không cần stub                                                                                           | Phước      | —                                            |
| 2   | Thống nhất interface giao message `Stream`→`Connection` (C-4) và quyết định ai sửa                                                                           | M1+M2      | Cần Phước phê duyệt, ghi ADR mới             |
| 3   | `connection.py`: dùng `derive_session_keys`, `is_server`, AAD; loại DATA thiếu `FLAG_ENCRYPTED`; ACK sau giải mã (C-1, C-2, C-3, H-1)                        | M1         | ADR-005/006/010; đã có API trong `crypto.py` |
| 4   | `connection.py`: gọi `can_send()`; bắt ngoại lệ trong `_handle_ack`/`read_incoming` (H-2, H-6)                                                               | M1         | T-M1-05                                      |
| 5   | `reliability.py`: sửa Fast Convergence 0.85 + sửa test, thêm chặn 1.5×cwnd, giải quyết `failed_packets`, timeout, W_max khi vào CA (H-3, H-4, H-5, M-1, M-2) | Phước (M2) | RFC 9438 §4.2/4.7/4.8/4.10                   |
| 6   | `stream.py`: phát hiện overlap/gap, giới hạn số khóa buffer (M-3)                                                                                            | Phước (M2) | Mục 2                                        |
| 7   | Viết các test ở §IV, sau đó chạy lại toàn bộ và cập nhật `PROGRESS.md`                                                                                       | Phước      | Mục 3-6                                      |
| 8   | Đồng bộ tài liệu (README 1378/WebSocket, `PROJECT_CONTEXT.md` §6-7, trích dẫn RFC 8312→9438) tại thời điểm audit                                                                 | Phước      | Lịch sử; README và trạng thái project đã được cập nhật sau audit |
| 9   | Gate 3: UDP thật qua `server.py`/`client.py`, file lớn, mất 10%                                                                                              | Cả nhóm    | Mục 3-4                                      |

---

## VII. Final Verdict

### **NOT READY**

- **Theo module riêng lẻ:** gần mức READY WITH ISSUES. Các hàm M2 đạt yêu cầu ở mức đơn vị; lỗi riêng của M2 là H-3, H-4, H-5, M-3.
- **Theo tiêu chí bàn giao (pipeline hoạt động):** chưa đạt, vì file truyền bằng cách app đang dùng bị mất ngay ở mất gói 5%, đồng thời tồn tại tái sử dụng nonce và bỏ qua xác thực (C-1..C-4).
- Điều kiện để nâng lên **READY WITH ISSUES**: xử lý C-1..C-4 và H-1/H-2 (phần lớn cần M1), rồi chạy lại §V bằng các test mới và đạt SHA-256 khớp ở mọi kịch bản.

**Giới hạn của báo cáo:** các con số §V đến từ mô phỏng một process, không phải UDP thật. `server.py`, `session.py`, `http_ui.py` chưa được đọc. RFC 6298 và RFC 2018 chưa được tải lại trong phiên này. Nhận định về P13 và M-3 (khuếch đại bộ nhớ) có phần là suy luận từ code.

**Tái hiện:** script `C:\Users\mhp23\AppData\Local\Temp\claude\48b95267d903554e\9278ee1c-c670-4358-894d-904733e4465e\scratchpad\audit_probe.py` (các phần `handshake nonce ackfirst aad badack multimsg failed cubic stream mtu inject hello2 pipe`), chạy với `PYTHONPATH` trỏ tới thư mục stub `…\scratchpad\stubs` và `PYTHONIOENCODING=utf-8`. Bản sao RFC 9438 dạng text: `…\scratchpad\rfc\rfc9438.txt`.
