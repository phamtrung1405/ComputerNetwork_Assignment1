"""
common/packet.py
~~~~~~~~~~~~~~~~
Đóng gói (pack) và bóc tách (unpack) cấu trúc nhị phân 22-byte
của Header giao thức truyền tải tùy chỉnh.

Header Layout (22 bytes, big-endian):
    ┌──────────┬──────────┬──────────┬───────┬───────────┬────────┬─────────────┬──────────┐
    │ msg_type │ conn_id  │ seq_num  │ flags │ stream_id │ offset │ payload_len │ reserved │
    │  1 byte  │ 4 bytes  │ 4 bytes  │ 1 byte│  2 bytes  │4 bytes │   2 bytes   │ 4 bytes  │
    └──────────┴──────────┴──────────┴───────┴───────────┴────────┴─────────────┴──────────┘
"""

from __future__ import annotations

import struct
from typing import NamedTuple

# Format: big-endian  B=uint8  I=uint32  H=uint16
# msg_type(B) + conn_id(I) + seq_num(I) + flags(B) + stream_id(H) + offset(I) + payload_len(H) + reserved(I)
# 1 + 4 + 4 + 1 + 2 + 4 + 2 + 4 = 22 bytes
_HEADER_FMT: str = "!BIIBHIHI"
_HEADER_SIZE: int = struct.calcsize(_HEADER_FMT)

assert _HEADER_SIZE == 22, f"Header phải 22 bytes, hiện tại {_HEADER_SIZE}"


class Header(NamedTuple):
    """Biểu diễn header đã bóc tách."""
    msg_type: int
    conn_id: int
    seq_num: int
    flags: int
    stream_id: int
    offset: int
    payload_len: int
    reserved: int


def pack(
    msg_type: int,
    conn_id: int,
    seq_num: int,
    flags: int,
    stream_id: int,
    offset: int,
    payload: bytes,
) -> bytes:
    """Ghép các tham số thành chuỗi byte hoàn chỉnh (Header + Payload).

    Args:
        msg_type: Loại gói tin (PKT_DATA, PKT_ACK, …).
        conn_id: Định danh kết nối (uint32).
        seq_num: Số thứ tự gói tin (uint32).
        flags: Cờ điều khiển (bitmask uint8).
        stream_id: Mã luồng dữ liệu (uint16).
        offset: Byte offset trong luồng (uint32).
        payload: Dữ liệu ứng dụng đính kèm.

    Returns:
        bytes: Chuỗi bytes gồm 22-byte header nối với payload.
    """
    payload_len: int = len(payload)
    header_bytes: bytes = struct.pack(
        _HEADER_FMT,
        msg_type,
        conn_id,
        seq_num,
        flags,
        stream_id,
        offset,
        payload_len,
        0,  # reserved
    )
    return header_bytes + payload


def unpack(raw_bytes: bytes) -> tuple[Header, bytes]:
    """Bóc tách 22 bytes đầu tiên để lấy metadata, trả về Header và Payload.

    Args:
        raw_bytes: Dữ liệu thô nhận từ socket (>= 22 bytes).

    Returns:
        Tuple gồm Header (NamedTuple) và phần payload còn lại.

    Raises:
        ValueError: Khi dữ liệu nhỏ hơn kích thước header tối thiểu.
    """
    if len(raw_bytes) < _HEADER_SIZE:
        raise ValueError(
            f"Dữ liệu quá ngắn: cần ít nhất {_HEADER_SIZE} bytes, "
            f"nhận được {len(raw_bytes)} bytes."
        )
    fields = struct.unpack(_HEADER_FMT, raw_bytes[:_HEADER_SIZE])
    header = Header(
        msg_type=fields[0],
        conn_id=fields[1],
        seq_num=fields[2],
        flags=fields[3],
        stream_id=fields[4],
        offset=fields[5],
        payload_len=fields[6],
        reserved=fields[7],
    )
    payload: bytes = raw_bytes[_HEADER_SIZE: _HEADER_SIZE + header.payload_len]
    return header, payload
