"""
transport/stream.py
~~~~~~~~~~~~~~~~~~~
Multiplexing and in-order byte stream reassembly for a logical Stream
within a Connection.
"""

from __future__ import annotations

import threading
from typing import Optional, Callable

from common.constants import MAX_APP_PAYLOAD, FLAG_EOM


class Stream:
    """Manages a logical byte stream within a Connection.

    Each Stream has a unique stream ID, a receive buffer, and byte offset pointers.
    """

    def __init__(
        self,
        stream_id: int,
        send_callback: Optional[Callable[[int, int, bytes, int], None]] = None,
    ) -> None:
        """Initialize Stream.

        Args:
            stream_id: Stream identifier (uint16).
            send_callback: Callback function ``(stream_id, offset, chunk, flags)``
                           to transmit chunks via the Connection.
        """
        self.stream_id: int = stream_id
        self._send_callback: Optional[Callable[[int, int, bytes, int], None]] = (
            send_callback
        )

        # -- Sender side --
        self._write_offset: int = 0
        self._write_lock: threading.Lock = threading.Lock()

        # -- Receiver side --
        self._recv_buffer: dict[int, bytes] = {}
        self._recv_lock: threading.Lock = threading.Lock()
        self._expected_offset: int = 0
        self._eom_offset: Optional[int] = None
        self._complete_messages: list[bytes] = []

    # ----------------------- Send -----------------------

    def write(self, data: bytes) -> int:
        """Fragment data into chunks by MTU, assign offsets, and transmit.

        Args:
            data: Application payload bytes to send on this stream.

        Returns:
            Total number of bytes queued for transmission.
        """
        total_sent: int = 0
        with self._write_lock:
            view = memoryview(data)
            remaining: int = len(data)
            pos: int = 0

            while remaining > 0:
                chunk_size: int = min(remaining, MAX_APP_PAYLOAD)
                chunk: bytes = bytes(view[pos: pos + chunk_size])
                is_last: bool = (remaining - chunk_size) == 0
                flags: int = FLAG_EOM if is_last else 0

                if self._send_callback is not None:
                    self._send_callback(
                        self.stream_id, self._write_offset, chunk, flags
                    )

                self._write_offset += chunk_size
                pos += chunk_size
                remaining -= chunk_size
                total_sent += chunk_size

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
        with self._recv_lock:
            self._recv_buffer[offset] = data

            if is_eom:
                self._eom_offset = offset + len(data)

            # Attempt to reassemble a complete message
            if self._eom_offset is not None:
                assembled = self._try_assemble()
                if assembled is not None:
                    self._complete_messages.append(assembled)
                    return assembled

        return None

    def _try_assemble(self) -> Optional[bytes]:
        """Attempt to assemble all buffered chunks into a continuous message.

        Returns:
            Reassembled message bytes if complete, or None if chunks are missing.
        """
        if self._eom_offset is None:
            return None

        parts: list[bytes] = []
        current: int = self._expected_offset

        # Sort available chunk offsets
        sorted_offsets = sorted(self._recv_buffer.keys())

        for off in sorted_offsets:
            if off != current:
                # Gap detected – missing data
                return None
            chunk = self._recv_buffer[off]
            parts.append(chunk)
            current += len(chunk)

        if current < self._eom_offset:
            return None

        # Reassembly successful – cleanup and advance pointer
        result: bytes = b"".join(parts)
        self._recv_buffer.clear()
        self._expected_offset = current
        self._eom_offset = None
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
