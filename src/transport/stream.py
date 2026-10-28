"""
transport/stream.py
~~~~~~~~~~~~~~~~~~~
Multiplexing and in-order byte stream reassembly for a logical Stream
within a Connection.
"""

from __future__ import annotations

import threading
from typing import Callable, Optional

from common.constants import (
    FLAG_EOM,
    HEADER_SIZE,
    MAX_UDP_PAYLOAD,
    GCM_TAG_SIZE,
)

# Kích thước chunk an toàn tối đa cho ứng dụng (tính cả 16B GCM tag và 22B header)
SAFE_MAX_APP_PAYLOAD: int = MAX_UDP_PAYLOAD - HEADER_SIZE - GCM_TAG_SIZE  # 1362 bytes

# Dung lượng bộ đệm nhận tối đa mặc định cho mỗi stream để ngăn ngừa tấn công DoS tràn bộ nhớ
DEFAULT_MAX_STREAM_BUFFER_SIZE: int = 16 * 1024 * 1024  # 16 MB


class Stream:
    """Manages a logical byte stream within a Connection.

    Each Stream has a unique stream ID, a receive buffer, and byte offset pointers.
    """

    def __init__(
        self,
        stream_id: int,
        send_callback: Optional[Callable[[int, int, bytes, int], None]] = None,
        chunk_size: int = SAFE_MAX_APP_PAYLOAD,
        max_buffer_size: int = DEFAULT_MAX_STREAM_BUFFER_SIZE,
    ) -> None:
        """Initialize Stream.

        Args:
            stream_id: Stream identifier (uint16).
            send_callback: Callback function ``(stream_id, offset, chunk, flags)``
                           to transmit chunks via the Connection.
        """
        self.stream_id: int = stream_id
        self._send_callback: Optional[Callable[[int, int, bytes, int], None]] = send_callback
        self.chunk_size: int = chunk_size
        self.max_buffer_size: int = max_buffer_size

        # -- Sender side --
        self._write_offset: int = 0
        self._write_lock: threading.Lock = threading.Lock()

        # -- Receiver side --
        self._recv_buffer: dict[int, bytes] = {}
        self._recv_lock: threading.Lock = threading.Lock()
        self._expected_offset: int = 0
        self._eom_offsets: set[int] = set()  # Lưu các điểm biên EOM đã nhận được
        self._buffered_bytes: int = 0  # Theo dõi dung lượng bộ đệm hiện tại
        self._complete_messages: list[bytes] = []

    # ----------------------- Send -----------------------

    def write(self, data: bytes) -> int:
        """Fragment data into chunks by MTU, assign offsets, and transmit.

        Args:
            data: Application payload bytes to send on this stream.

        Returns:
            Total number of bytes queued for transmission.
        """
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError(f"data phải là bytes hoặc bytearray, nhận được: {type(data)}")

        with self._write_lock:
            # Xử lý trường hợp gửi message rỗng
            if len(data) == 0:
                if self._send_callback is not None:
                    self._send_callback(self.stream_id, self._write_offset, b"", FLAG_EOM)
                return 0

            view = memoryview(data)
            remaining: int = len(data)
            pos: int = 0
            total_sent: int = 0

            while remaining > 0:
                c_size: int = min(remaining, self.chunk_size)
                chunk: bytes = bytes(view[pos: pos + c_size])
                is_last: bool = (remaining - c_size) == 0
                flags: int = FLAG_EOM if is_last else 0

                if self._send_callback is not None:
                    self._send_callback(self.stream_id, self._write_offset, chunk, flags)

                self._write_offset += c_size
                pos += c_size
                remaining -= c_size
                total_sent += c_size

            return total_sent

    # ----------------------- Receive -----------------------

    def read_incoming(
        self, offset: int, data: bytes, is_eom: bool
    ) -> Optional[bytes]:
        """Place incoming chunk into its offset position in the receive buffer.

        Returns a complete message only when the EOM flag is received
        and all preceding contiguous chunks have arrived without gaps.

        Args:
            offset: Byte offset of the chunk within the stream.
            data: Received chunk payload bytes.
            is_eom: ``True`` if this is the final chunk of the message.

        Returns:
            Complete reassembled message bytes, or ``None`` if incomplete.
        """
        if not isinstance(offset, int):
            raise TypeError(f"offset phải là số nguyên int, nhận được: {type(offset)}")
        if offset < 0:
            raise ValueError(f"offset phải là số nguyên không âm, nhận được: {offset}")
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError(f"data phải là bytes hoặc bytearray, nhận được: {type(data)}")
        if not isinstance(is_eom, bool):
            raise TypeError(f"is_eom phải là bool, nhận được: {type(is_eom)}")

        with self._recv_lock:
            data_bytes: bytes = bytes(data)
            chunk_len: int = len(data_bytes)
            end_offset: int = offset + chunk_len

            # ── 1. Lọc bỏ chunk trùng lặp / stale đã bàn giao (ADR-008) ──
            if chunk_len > 0:
                if end_offset <= self._expected_offset:
                    # Toàn bộ chunk đã được ráp nối và trả lên app từ trước -> bỏ qua an toàn
                    return None

                if offset < self._expected_offset:
                    # Chunk bị chồng lấn một phần với phần đã nhận
                    overlap: int = self._expected_offset - offset
                    data_bytes = data_bytes[overlap:]
                    offset = self._expected_offset
                    chunk_len = len(data_bytes)
                    end_offset = offset + chunk_len
            else:
                # Chunk 0-byte (payload rỗng)
                if offset < self._expected_offset:
                    return None
                if not is_eom:
                    return None

            # ── 2. Kiểm soát giới hạn bộ đệm phòng chống DoS ──
            new_bytes: int = (
                chunk_len
                if offset not in self._recv_buffer
                else max(0, chunk_len - len(self._recv_buffer[offset]))
            )
            if self._buffered_bytes + new_bytes > self.max_buffer_size:
                raise ValueError(
                    f"Stream {self.stream_id} buffer overflow: {self._buffered_bytes + new_bytes} "
                    f"bytes vượt quá giới hạn {self.max_buffer_size} bytes."
                )

            # Cập nhật bộ đệm
            if offset in self._recv_buffer:
                self._buffered_bytes -= len(self._recv_buffer[offset])
            self._recv_buffer[offset] = data_bytes
            self._buffered_bytes += chunk_len

            # ── 3. Ghi nhận biên kết thúc message (EOM) ──
            if is_eom:
                self._eom_offsets.add(end_offset)

            first_assembled: Optional[bytes] = None
            while True:
                assembled = self._try_assemble()
                if assembled is None:
                    break
                self._complete_messages.append(assembled)
                if first_assembled is None:
                    first_assembled = assembled

            return first_assembled

    def _try_assemble(self) -> Optional[bytes]:
        """Attempt to assemble all buffered chunks into a continuous message.

        Returns:
            Reassembled message bytes if complete, or None if chunks are missing.
        """
        current: int = self._expected_offset
        parts: list[bytes] = []
        assembled_offsets: list[int] = []
        eom_found: bool = False

        # Sort available chunk offsets
        sorted_offsets = sorted(self._recv_buffer.keys())

        for off in sorted_offsets:
            if off != current:
                # Gap detected – missing data
                return None
            chunk = self._recv_buffer[off]
            parts.append(chunk)
            assembled_offsets.append(off)
            current += len(chunk)

            if current in self._eom_offsets:
                eom_found = True
                break

        if not eom_found:
            # Chưa đủ các chunk liên tục từ _expected_offset đến bất kỳ biên EOM nào
            return None

        # Reassembly successful – cleanup and advance pointer
        result: bytes = b"".join(parts)

        # Dọn dẹp có chọn lọc chỉ các chunk đã ráp nối (ADR-008)
        for off in assembled_offsets:
            self._buffered_bytes -= len(self._recv_buffer[off])
            del self._recv_buffer[off]

        self._eom_offsets.remove(current)
        self._expected_offset = current
        return result

    def get_complete_messages(self) -> list[bytes]:
        """Retrieve and clear all reassembled complete messages.

        Returns:
            List of complete message byte arrays.
        """
        with self._recv_lock:
            msgs = list(self._complete_messages)
            self._complete_messages.clear()
            return msgs
