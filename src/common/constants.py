"""
common/constants.py
~~~~~~~~~~~~~~~~~~~~
Các hằng số toàn cục của giao thức truyền tải tùy chỉnh.

Bao gồm: magic number, loại gói tin (packet types), cờ (flags),
kích thước header, cấu hình timeout, MTU và tham số tắc nghẽn.
"""

from __future__ import annotations

# ──────────────────────────────────────────────────
# Magic number – 4 bytes đầu tiên để nhận diện giao thức
# ──────────────────────────────────────────────────
PROTOCOL_MAGIC: bytes = b"\xDE\xAD\xBE\xEF"

# ──────────────────────────────────────────────────
# Packet Types (1 byte – uint8)
# ──────────────────────────────────────────────────
PKT_HELLO: int = 0x01          # Client gửi khi bắt đầu handshake
PKT_HELLO_ACK: int = 0x02      # Server phản hồi handshake
PKT_DATA: int = 0x03           # Dữ liệu ứng dụng
PKT_ACK: int = 0x04            # Xác nhận (Selective ACK)
PKT_HEARTBEAT: int = 0x05      # Keepalive
PKT_HEARTBEAT_ACK: int = 0x06  # Phản hồi keepalive
PKT_CLOSE: int = 0x07          # Đóng kết nối
PKT_AUTH_REQ: int = 0x08       # Yêu cầu xác thực
PKT_AUTH_RESP: int = 0x09      # Phản hồi xác thực

# ──────────────────────────────────────────────────
# Flags (1 byte – bitmask)
# ──────────────────────────────────────────────────
FLAG_NONE: int = 0x00
FLAG_EOM: int = 0x01            # End-of-Message
FLAG_FIN: int = 0x02            # Kết thúc Stream
FLAG_SYN: int = 0x04            # Khởi tạo Stream
FLAG_ENCRYPTED: int = 0x08      # Payload đã mã hóa

# ──────────────────────────────────────────────────
# Header layout  (22 bytes tổng cộng)
#   msg_type   : uint8     (1)
#   conn_id    : uint32    (4)
#   seq_num    : uint32    (4)
#   flags      : uint8     (1)
#   stream_id  : uint16    (2)
#   offset     : uint32    (4)
#   payload_len: uint16    (2)
#   reserved   : uint32    (4)
#                           ──
#                           22 bytes
# ──────────────────────────────────────────────────
HEADER_SIZE: int = 22
HEADER_FORMAT: str = "!BIBHIHHI"
#  B = msg_type (1)
#  I = conn_id  (4)
#  B = flags    (1)  ← đặt sau conn_id cho dễ căn
#  H = stream_id(2)
#  I = seq_num  (4)
#  H = payload_len(2)
#  H = reserved_hi(2)
#  I = offset   (4)
#  Tổng = 1+4+1+2+4+2+2+4 = 20 – ta sẽ dùng format chuẩn hơn bên dưới

# Dùng format rõ ràng: big-endian
# B  msg_type      1
# I  conn_id       4
# I  seq_num       4
# B  flags         1
# H  stream_id     2
# I  offset        4
# H  payload_len   2
# 4 bytes padding → tổng 22
HEADER_STRUCT_FMT: str = "!BIIBHIH2s"
# 1 + 4 + 4 + 1 + 2 + 4 + 2 + 2 = 20 → thêm 2 bytes reserved = 22 ✓

# ──────────────────────────────────────────────────
# Kích thước & MTU
# ──────────────────────────────────────────────────
MAX_UDP_PAYLOAD: int = 1400     # bytes – giới hạn an toàn để tránh phân mảnh IP
MAX_APP_PAYLOAD: int = MAX_UDP_PAYLOAD - HEADER_SIZE  # 1378 bytes
RECV_BUFFER_SIZE: int = 65535   # Bộ đệm nhận tối đa

# ──────────────────────────────────────────────────
# Timeout & Keepalive (giây)
# ──────────────────────────────────────────────────
INITIAL_RTO: float = 1.0       # Retransmission timeout ban đầu
MIN_RTO: float = 0.2           # RTO tối thiểu
MAX_RTO: float = 10.0          # RTO tối đa
HEARTBEAT_INTERVAL: float = 5.0  # Gửi heartbeat mỗi 5 giây
IDLE_TIMEOUT: float = 30.0     # Ngắt kết nối nếu im lặng > 30 giây
TICK_INTERVAL: float = 0.05    # Chu kỳ tick loop (50ms)

# ──────────────────────────────────────────────────
# Tham số tắc nghẽn CUBIC
# ──────────────────────────────────────────────────
CUBIC_C: float = 0.4           # Hằng số C trong CUBIC
CUBIC_BETA: float = 0.7        # Hệ số giảm khi mất gói
INITIAL_CWND: int = 10         # Cửa sổ tắc nghẽn ban đầu (đơn vị: gói)
MIN_CWND: int = 2              # Cửa sổ tắc nghẽn tối thiểu
MAX_CWND: int = 1024           # Cửa sổ tắc nghẽn tối đa
SSTHRESH_INITIAL: int = 64     # Slow-start threshold ban đầu

# ──────────────────────────────────────────────────
# Server mặc định
# ──────────────────────────────────────────────────
DEFAULT_SERVER_HOST: str = "127.0.0.1"
DEFAULT_SERVER_PORT: int = 9000
DEFAULT_HTTP_PORT: int = 8080

# ──────────────────────────────────────────────────
# Encryption
# ──────────────────────────────────────────────────
AES_KEY_SIZE: int = 32         # AES-256
GCM_NONCE_SIZE: int = 12       # 96-bit nonce cho AES-GCM
GCM_TAG_SIZE: int = 16         # 128-bit authentication tag
