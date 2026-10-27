"""
transport/stream.py
~~~~~~~~~~~~~~~~~~~
Quản lý luồng dữ liệu logic (Stream), ghép kênh (Multiplexing),
phân mảnh (Fragmentation) và sắp xếp dữ liệu (Reassembly) trên nền giao thức tùy chỉnh.

Kiến trúc & Phân bổ Payload an toàn (Payload Budget Verification):
- Giới hạn MTU an toàn tránh phân mảnh IP: `MAX_UDP_PAYLOAD = 1400` bytes.
- Kích thước Header cố định của giao thức: `HEADER_SIZE = 22` bytes (`!BIIBHIHI`).
- Kích thước Authentication Tag của AES-256-GCM: `GCM_TAG_SIZE = 16` bytes.
- ĐẶC BIỆT LƯU Ý VỀ TÀI LIỆU ĐỀ BÀI:
  Tài liệu bài tập có đề cập kích thước chunk là 1378 bytes ($1400 - 22$). Tuy nhiên,
  khi tầng Transport kích hoạt mã hóa AEAD AES-256-GCM trên các gói tin `PKT_DATA`,
  thư viện `crypto.py` sẽ gắn thêm 16 bytes Authentication Tag vào cuối ciphertext.
  Nếu ứng dụng phân mảnh thành 1378 bytes plaintext:
    1378 (Plaintext) + 16 (Tag) + 22 (Header) = 1416 bytes > 1400 bytes (VƯỢT TRẦN MTU!).
  Do đó, kích thước phân mảnh ứng dụng an toàn tuyệt đối bắt buộc phải là:
    SAFE_MAX_APP_PAYLOAD = 1400 - 22 - 16 = 1362 bytes.
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
    """Quản lý một luồng dữ liệu logic (stream) trong Connection.

    Mỗi Stream có một ID riêng, bộ đệm nhận, và con trỏ byte offset độc lập.
    Đảm bảo:
    - Phân mảnh chính xác dữ liệu gửi đi (write) theo giới hạn MTU an toàn (1362B).
    - Ráp nối dữ liệu đến (read_incoming) đúng thứ tự, xử lý an toàn các chunk out-of-order.
    - Loại bỏ hoàn toàn nguy cơ deadlock khi nhận chunk trùng lặp (duplicate chunks) (ADR-008).
    - Bảo toàn các chunk của message tiếp theo đã nằm trong bộ đệm khi ráp nối message trước (ADR-008).
    - Giới hạn kích thước bộ đệm nhận tối đa để phòng ngừa tấn công làm cạn kiệt tài nguyên bộ nhớ.
    """

    def __init__(
        self,
        stream_id: int,
        send_callback: Optional[Callable[[int, int, bytes, int], None]] = None,
        chunk_size: int = SAFE_MAX_APP_PAYLOAD,
        max_buffer_size: int = DEFAULT_MAX_STREAM_BUFFER_SIZE,
    ) -> None:
        """Khởi tạo Stream.

        Args:
            stream_id: Mã định danh luồng (uint16).
            send_callback: Hàm callback `(stream_id, offset, chunk, flags)` để gửi chunk qua Connection.
            chunk_size: Kích thước tối đa của mỗi chunk phân mảnh (mặc định 1362 bytes).
            max_buffer_size: Dung lượng bộ đệm nhận tối đa cho phép (mặc định 16MB).
        """
        self.stream_id: int = stream_id
        self._send_callback: Optional[Callable[[int, int, bytes, int], None]] = send_callback
        self.chunk_size: int = chunk_size
        self.max_buffer_size: int = max_buffer_size

        # ── Phía gửi (Write State) ──
        self._write_offset: int = 0
        self._write_lock: threading.Lock = threading.Lock()

        # ── Phía nhận (Receive & Reassembly State) ──
        self._recv_buffer: dict[int, bytes] = {}
        self._recv_lock: threading.Lock = threading.Lock()
        self._expected_offset: int = 0
        self._eom_offsets: set[int] = set()  # Lưu các điểm biên EOM đã nhận được
        self._buffered_bytes: int = 0  # Theo dõi dung lượng bộ đệm hiện tại
        self._complete_messages: list[bytes] = []

    # ─────────────────────── Gửi (Write & Fragmentation) ───────────────────────

    def write(self, data: bytes) -> int:
        """Cắt dữ liệu thành các chunk theo trần an toàn MTU (1362 bytes), gán offset và gửi đi.

        Thuật toán:
        1. Kiểm tra tính hợp lệ của tham số đầu vào `data`.
        2. Nếu dữ liệu rỗng (`len(data) == 0`): Gửi 1 chunk 0-byte mang cờ `FLAG_EOM` và trả về 0.
        3. Nếu dữ liệu không rỗng: Chia thành các chunk có kích thước tối đa `self.chunk_size` (1362B).
        4. Gán byte offset tăng dần tương ứng với `self._write_offset`.
        5. Đánh dấu cờ `FLAG_EOM` trên chunk cuối cùng của message.
        6. Gọi `self._send_callback` để đẩy từng chunk xuống tầng Connection đóng gói.

        Args:
            data: Dữ liệu ứng dụng cần truyền trên luồng này (dạng bytes hoặc bytearray).

        Returns:
            int: Tổng số bytes dữ liệu ứng dụng đã được phân mảnh và gửi đi.

        Raises:
            TypeError: Nếu `data` không phải kiểu bytes hoặc bytearray.
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

    # ─────────────────────── Nhận (Read & Reassembly) ───────────────────────

    def read_incoming(
        self, offset: int, data: bytes, is_eom: bool
    ) -> Optional[bytes]:
        """Tiếp nhận chunk dữ liệu đến, lưu vào bộ đệm và thử ráp nối message hoàn chỉnh.

        Thuật toán xử lý:
        1. Kiểm tra tính hợp lệ của tham số (offset >= 0, data là bytes, is_eom là bool).
        2. Lọc bỏ các chunk trùng lặp (Duplicate / Stale Chunk):
           - Nếu `offset + len(data) <= self._expected_offset`, toàn bộ chunk này thuộc về phần dữ liệu
             đã được ráp nối và bàn giao cho tầng ứng dụng trước đó -> Bỏ qua an toàn, trả về None.
           - Nếu chunk bị chồng lấn một phần (`offset < self._expected_offset < offset + len(data)`),
             cắt bỏ phần dữ liệu đã nhận và chỉ lưu phần chưa có.
        3. Phòng chống tấn công làm cạn kiệt bộ nhớ (Buffer Overflow Guard):
           - Kiểm tra tổng số byte đang lưu trong `_recv_buffer`. Nếu vượt quá `self.max_buffer_size`,
             ném ngoại lệ `ValueError` để ngăn ngừa DoS.
        4. Ghi nhận dữ liệu vào `_recv_buffer` và lưu điểm kết thúc vào `_eom_offsets` nếu `is_eom=True`.
        5. Gọi `_try_assemble()` để ráp nối tất cả các message đã hoàn chỉnh liên tiếp.

        Args:
            offset: Byte offset của chunk trong luồng logic.
            data: Dữ liệu nhị phân của chunk nhận được.
            is_eom: `True` nếu đây là chunk cuối cùng của một message.

        Returns:
            Optional[bytes]: Message hoàn chỉnh đầu tiên vừa ráp nối được, hoặc `None` nếu còn thiếu dữ liệu.

        Raises:
            TypeError: Nếu các tham số sai kiểu dữ liệu.
            ValueError: Nếu `offset < 0` hoặc dung lượng bộ đệm vượt quá giới hạn an toàn.
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

            # ── 4. Thử ráp nối các message hoàn chỉnh ──
            first_assembled: Optional[bytes] = None
            while True:
                msg: Optional[bytes] = self._try_assemble()
                if msg is None:
                    break
                self._complete_messages.append(msg)
                if first_assembled is None:
                    first_assembled = msg

            return first_assembled

    def _try_assemble(self) -> Optional[bytes]:
        """Thử ráp nối chuỗi chunk liên tục bắt đầu từ `_expected_offset` đến biên EOM gần nhất.

        Nguyên tắc dọn dẹp bộ đệm an toàn (ADR-008):
        - TUYỆT ĐỐI KHÔNG DÙNG `self._recv_buffer.clear()`: Hành vi xóa toàn bộ bộ đệm sẽ làm mất sạch
          các chunk thuộc về message tiếp theo đã nằm sẵn trong bộ đệm do mạng xáo trộn thứ tự.
        - Thay vào đó, hàm CHỈ xóa chính xác các offset đã được ráp nối thành công vào message hiện tại.
        - Cập nhật `_expected_offset = current` để chuẩn bị cho message tiếp theo.

        Returns:
            Optional[bytes]: Chuỗi byte message hoàn chỉnh nếu đủ dữ liệu, ngược lại trả về `None`.
        """
        current: int = self._expected_offset

        # Xử lý trường hợp đặc biệt: Message rỗng (0 bytes) tại đúng current offset
        if current in self._eom_offsets and current not in self._recv_buffer:
            self._eom_offsets.remove(current)
            return b""

        parts: list[bytes] = []
        assembled_offsets: list[int] = []
        eom_found: bool = False

        # Duyệt nối tiếp các chunk liên tục bắt đầu từ current
        while current in self._recv_buffer:
            chunk: bytes = self._recv_buffer[current]
            parts.append(chunk)
            assembled_offsets.append(current)
            current += len(chunk)

            if current in self._eom_offsets:
                eom_found = True
                break

        if not eom_found:
            # Chưa đủ các chunk liên tục từ _expected_offset đến bất kỳ biên EOM nào
            return None

        # Ráp nối thành công
        result: bytes = b"".join(parts)

        # Dọn dẹp có chọn lọc chỉ các chunk đã ráp nối (ADR-008)
        for off in assembled_offsets:
            self._buffered_bytes -= len(self._recv_buffer[off])
            del self._recv_buffer[off]

        self._eom_offsets.remove(current)
        self._expected_offset = current
        return result

    def get_complete_messages(self) -> list[bytes]:
        """Lấy và xóa tất cả message đã hoàn chỉnh khỏi hàng đợi chờ của Stream.

        Returns:
            list[bytes]: Danh sách các message hoàn chỉnh đã nhận được theo thứ tự thời gian.
        """
        with self._recv_lock:
            msgs = list(self._complete_messages)
            self._complete_messages.clear()
            return msgs
