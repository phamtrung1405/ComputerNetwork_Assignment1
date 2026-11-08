"""
common/packet.py
~~~~~~~~~~~~~~~~
Pack and unpack 22-byte binary structure
of custom transport protocol Header.

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

assert _HEADER_SIZE == 22, f"Header must have 22 bytes, current is {_HEADER_SIZE}"


class Header(NamedTuple):
    """Represents unpacked header metadata."""
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
    """Pack parameters into a complete byte sequence (Header + Payload).

    Args:
        msg_type: Packet type (PKT_DATA, PKT_ACK, ...).
        conn_id: Connection identifier (uint32).
        seq_num: Packet sequence number (uint32).
        flags: Control flags (bitmask uint8).
        stream_id: Data stream ID (uint16).
        offset: Byte offset within stream (uint32).
        payload: Attached application payload data.

    Returns:
        bytes: Byte sequence containing 22-byte header concatenated with payload.
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
    """Unpack first 22 bytes to extract metadata, returning Header and Payload.

    Args:
        raw_bytes: Raw data received from socket (>= 22 bytes).

    Returns:
        Tuple of Header (NamedTuple) and remaining payload data.

    Raises:
        ValueError: When raw data is smaller than minimum header size.
    """
    if len(raw_bytes) < _HEADER_SIZE:
        raise ValueError(
            f"Data too short: requires at least {_HEADER_SIZE} bytes, "
            f"received {len(raw_bytes)} bytes."
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
