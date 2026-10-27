"""
tests/test_stream.py
~~~~~~~~~~~~~~~~~~~~
Bộ kiểm thử đơn vị cho module quản lý phân mảnh và ráp nối luồng (src/transport/stream.py).
Kiểm thử toàn diện:
- Phân mảnh dữ liệu gửi (write): payload rỗng, kích thước biên (1362 bytes, 1363 bytes), payload lớn (64 KB).
- Ráp nối dữ liệu đến (read_incoming & _try_assemble): đúng thứ tự, đảo lộn thứ tự (out-of-order), thiếu chunk.
- Xử lý chunk trùng lặp (duplicate chunk) và loại bỏ hoàn toàn nguy cơ deadlock (ADR-008).
- Bảo toàn chunk của message tương lai trong bộ đệm khi ráp nối message trước (sửa lỗi _recv_buffer.clear() - ADR-008).
- Phòng chống tràn bộ đệm không giới hạn (DoS Buffer Overflow Protection).
- Xử lý dữ liệu đầu vào không hợp lệ (negative offset, non-bytes data, non-boolean is_eom).
"""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

# Đảm bảo src có trong sys.path
SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from common.constants import FLAG_EOM
from transport.stream import Stream, SAFE_MAX_APP_PAYLOAD


class TestStreamWrite(unittest.TestCase):
    """Kiểm thử tính năng phân mảnh dữ liệu gửi qua Stream.write()."""

    def setUp(self) -> None:
        self.sent_chunks: list[tuple[int, int, bytes, int]] = []

    def _mock_send_callback(self, stream_id: int, offset: int, chunk: bytes, flags: int) -> None:
        self.sent_chunks.append((stream_id, offset, chunk, flags))

    def test_write_empty_payload(self) -> None:
        """Gửi payload rỗng (0 bytes): phải gửi 1 chunk rỗng với cờ FLAG_EOM."""
        stream = Stream(stream_id=1, send_callback=self._mock_send_callback)
        sent = stream.write(b"")

        self.assertEqual(sent, 0)
        self.assertEqual(len(self.sent_chunks), 1)
        sid, offset, chunk, flags = self.sent_chunks[0]
        self.assertEqual(sid, 1)
        self.assertEqual(offset, 0)
        self.assertEqual(chunk, b"")
        self.assertEqual(flags, FLAG_EOM)

    def test_write_single_chunk_under_mtu(self) -> None:
        """Gửi dữ liệu nhỏ hơn kích thước chunk tối đa: chỉ gửi 1 chunk duy nhất mang cờ FLAG_EOM."""
        stream = Stream(stream_id=2, send_callback=self._mock_send_callback)
        data = b"Hello, StreamChat Stream 2!"
        sent = stream.write(data)

        self.assertEqual(sent, len(data))
        self.assertEqual(len(self.sent_chunks), 1)
        sid, offset, chunk, flags = self.sent_chunks[0]
        self.assertEqual(sid, 2)
        self.assertEqual(offset, 0)
        self.assertEqual(chunk, data)
        self.assertEqual(flags, FLAG_EOM)

    def test_write_boundary_size_exact_chunk(self) -> None:
        """Kích thước đúng bằng SAFE_MAX_APP_PAYLOAD (1362 bytes): gửi chính xác 1 chunk với cờ FLAG_EOM."""
        stream = Stream(stream_id=1, send_callback=self._mock_send_callback)
        data = b"A" * SAFE_MAX_APP_PAYLOAD
        sent = stream.write(data)

        self.assertEqual(sent, SAFE_MAX_APP_PAYLOAD)
        self.assertEqual(len(self.sent_chunks), 1)
        _, offset, chunk, flags = self.sent_chunks[0]
        self.assertEqual(offset, 0)
        self.assertEqual(len(chunk), SAFE_MAX_APP_PAYLOAD)
        self.assertEqual(flags, FLAG_EOM)

    def test_write_boundary_size_exact_plus_one(self) -> None:
        """Kích thước SAFE_MAX_APP_PAYLOAD + 1 (1363 bytes): phải tách thành 2 chunks (1362B + 1B)."""
        stream = Stream(stream_id=1, send_callback=self._mock_send_callback)
        data = b"B" * (SAFE_MAX_APP_PAYLOAD + 1)
        sent = stream.write(data)

        self.assertEqual(sent, len(data))
        self.assertEqual(len(self.sent_chunks), 2)

        # Chunk 1: 1362 bytes, offset 0, flags = 0 (chưa kết thúc message)
        _, off1, chunk1, flags1 = self.sent_chunks[0]
        self.assertEqual(off1, 0)
        self.assertEqual(len(chunk1), SAFE_MAX_APP_PAYLOAD)
        self.assertEqual(flags1, 0)

        # Chunk 2: 1 byte, offset 1362, flags = FLAG_EOM
        _, off2, chunk2, flags2 = self.sent_chunks[1]
        self.assertEqual(off2, SAFE_MAX_APP_PAYLOAD)
        self.assertEqual(len(chunk2), 1)
        self.assertEqual(flags2, FLAG_EOM)

    def test_write_large_payload_multi_chunk(self) -> None:
        """Gửi dữ liệu lớn (64 KB): phân mảnh chính xác và offset tăng đơn điệu liên tục."""
        stream = Stream(stream_id=3, send_callback=self._mock_send_callback)
        data_size = 65536  # 64 KB
        data = bytes(i % 256 for i in range(data_size))
        sent = stream.write(data)

        self.assertEqual(sent, data_size)
        expected_chunks = (data_size + SAFE_MAX_APP_PAYLOAD - 1) // SAFE_MAX_APP_PAYLOAD
        self.assertEqual(len(self.sent_chunks), expected_chunks)

        reassembled = bytearray()
        expected_offset = 0
        for i, (_, offset, chunk, flags) in enumerate(self.sent_chunks):
            self.assertEqual(offset, expected_offset)
            expected_offset += len(chunk)
            reassembled.extend(chunk)
            if i == expected_chunks - 1:
                self.assertEqual(flags, FLAG_EOM)
            else:
                self.assertEqual(flags, 0)

        self.assertEqual(bytes(reassembled), data)

    def test_write_consecutive_messages_monotonic_offset(self) -> None:
        """Gửi nhiều message liên tiếp trên cùng 1 stream: offset phải tiếp tục tăng từ message trước."""
        stream = Stream(stream_id=1, send_callback=self._mock_send_callback)
        msg1 = b"Message 1"
        msg2 = b"Message 2"

        stream.write(msg1)
        stream.write(msg2)

        self.assertEqual(len(self.sent_chunks), 2)
        _, off1, _, _ = self.sent_chunks[0]
        _, off2, _, _ = self.sent_chunks[1]
        self.assertEqual(off1, 0)
        self.assertEqual(off2, len(msg1))

    def test_write_invalid_input_type(self) -> None:
        """Truyền sai kiểu dữ liệu vào write() phải ném TypeError."""
        stream = Stream(stream_id=1)
        with self.assertRaises(TypeError):
            stream.write("not_bytes")  # type: ignore
        with self.assertRaises(TypeError):
            stream.write(None)  # type: ignore


class TestStreamReadAndReassembly(unittest.TestCase):
    """Kiểm thử tính năng tiếp nhận và ráp nối (reassembly) của read_incoming()."""

    def setUp(self) -> None:
        self.stream = Stream(stream_id=1)

    def test_read_single_chunk_message(self) -> None:
        """Nhận 1 chunk hoàn chỉnh có cờ EOM: trả về ngay lập tức dữ liệu."""
        data = b"Complete single chunk payload"
        result = self.stream.read_incoming(offset=0, data=data, is_eom=True)

        self.assertEqual(result, data)
        self.assertEqual(self.stream.get_complete_messages(), [data])

    def test_read_in_order_multi_chunk(self) -> None:
        """Nhận các chunk theo đúng thứ tự offset tăng dần: chỉ trả về khi nhận được chunk EOM cuối cùng."""
        c1 = b"Part 1 - "
        c2 = b"Part 2 - "
        c3 = b"Part 3 End"

        r1 = self.stream.read_incoming(offset=0, data=c1, is_eom=False)
        self.assertIsNone(r1)

        r2 = self.stream.read_incoming(offset=len(c1), data=c2, is_eom=False)
        self.assertIsNone(r2)

        r3 = self.stream.read_incoming(offset=len(c1) + len(c2), data=c3, is_eom=True)
        self.assertEqual(r3, c1 + c2 + c3)
        self.assertEqual(self.stream.get_complete_messages(), [c1 + c2 + c3])

    def test_read_out_of_order_multi_chunk(self) -> None:
        """Nhận các chunk bị xáo trộn thứ tự: ráp nối chính xác khi đủ dữ liệu."""
        c1 = b"AAAA"
        c2 = b"BBBB"
        c3 = b"CCCC"

        # Thứ tự đến: Chunk 3 (EOM) -> Chunk 1 -> Chunk 2
        r3 = self.stream.read_incoming(offset=8, data=c3, is_eom=True)
        self.assertIsNone(r3)

        r1 = self.stream.read_incoming(offset=0, data=c1, is_eom=False)
        self.assertIsNone(r1)

        r2 = self.stream.read_incoming(offset=4, data=c2, is_eom=False)
        self.assertEqual(r2, c1 + c2 + c3)

    def test_read_incomplete_missing_middle_chunk(self) -> None:
        """Thiếu chunk ở giữa: không được trả về dữ liệu cho đến khi chunk thiếu được bù đắp."""
        c1 = b"Part_00;"  # 8 bytes: 0..8
        c2 = b"Part_01;"  # 8 bytes: 8..16
        c3 = b"Part_02!"  # 8 bytes: 16..24 (EOM)

        # Nhận chunk 0 và chunk 2 (thiếu chunk 1)
        self.assertIsNone(self.stream.read_incoming(offset=0, data=c1, is_eom=False))
        self.assertIsNone(self.stream.read_incoming(offset=16, data=c3, is_eom=True))

        # Bây giờ chunk 1 đến (offset 8..16)
        res = self.stream.read_incoming(offset=8, data=c2, is_eom=False)
        self.assertEqual(res, c1 + c2 + c3)

    def test_read_empty_message(self) -> None:
        """Nhận message rỗng (0 bytes, is_eom=True): trả về b''."""
        res = self.stream.read_incoming(offset=0, data=b"", is_eom=True)
        self.assertEqual(res, b"")


class TestStreamDuplicatesAndBugFixes(unittest.TestCase):
    """Kiểm thử phòng chống deadlock khi nhận chunk trùng lặp và sửa lỗi xóa sạch bộ đệm (ADR-008)."""

    def setUp(self) -> None:
        self.stream = Stream(stream_id=1)

    def test_duplicate_chunk_does_not_deadlock_adr008(self) -> None:
        """Gói tin retransmit trùng lặp (offset < expected_offset) không được làm deadlock stream."""
        # Message 1 hoàn thành (0..10)
        msg1 = b"0123456789"
        res1 = self.stream.read_incoming(offset=0, data=msg1, is_eom=True)
        self.assertEqual(res1, msg1)
        self.assertEqual(self.stream._expected_offset, 10)

        # Bên gửi truyền lại duplicate chunk của msg1 do ACK bị trễ
        dup = self.stream.read_incoming(offset=0, data=msg1, is_eom=True)
        self.assertIsNone(dup)  # Phải bỏ qua an toàn

        # Message 2 tiếp tục đến bình thường (offset 10..20)
        msg2 = b"ABCDEFGHIJ"
        res2 = self.stream.read_incoming(offset=10, data=msg2, is_eom=True)
        # Phải ráp nối thành công, KHÔNG được bị deadlock!
        self.assertEqual(res2, msg2)

    def test_reassembly_preserves_future_chunks_adr008(self) -> None:
        """Sửa lỗi _recv_buffer.clear(): Không được xóa các chunk của message tiếp theo đã nằm trong buffer."""
        # Kịch bản:
        # Message 1 gồm: Chunk A (offset 0..10), Chunk B (offset 10..20, EOM)
        # Message 2 gồm: Chunk C (offset 20..30), Chunk D (offset 30..40, EOM)
        # Giả sử mạng làm xáo trộn: Chunk C và Chunk D của Message 2 ĐẾN TRƯỚC!
        self.assertIsNone(self.stream.read_incoming(offset=20, data=b"CCCCCCCCCC", is_eom=False))
        self.assertIsNone(self.stream.read_incoming(offset=30, data=b"DDDDDDDDDD", is_eom=True))

        # Sau đó Chunk B của Message 1 đến
        self.assertIsNone(self.stream.read_incoming(offset=10, data=b"BBBBBBBBBB", is_eom=True))

        # Cuối cùng Chunk A của Message 1 đến -> Message 1 hoàn thành!
        res1 = self.stream.read_incoming(offset=0, data=b"AAAAAAAAAA", is_eom=False)
        self.assertEqual(res1, b"AAAAAAAAAABBBBBBBBBB")

        # Sau khi Message 1 hoàn thành, Message 2 (đã có đủ C và D trong bộ đệm) phải được ráp nối!
        complete_msgs = self.stream.get_complete_messages()
        # Danh sách hoàn chỉnh phải chứa cả Message 1 và Message 2
        self.assertIn(b"CCCCCCCCCCDDDDDDDDDD", complete_msgs)


class TestStreamBufferProtectionAndInvalidInputs(unittest.TestCase):
    """Kiểm thử phòng chống DoS bộ nhớ và xử lý dữ liệu đầu vào không hợp lệ."""

    def test_stream_buffer_overflow_protection(self) -> None:
        """Nếu kẻ tấn công gửi liên tục các chunk rác không có EOM vượt quá max_buffer_size, phải chặn đứng."""
        # Giới hạn bộ đệm tối đa 5000 bytes
        stream = Stream(stream_id=1, max_buffer_size=5000)

        # Gửi 4000 bytes hợp lệ
        stream.read_incoming(offset=1000, data=b"X" * 4000, is_eom=False)

        # Gửi thêm 2000 bytes khiến tổng dung lượng vượt quá 5000 bytes -> Phải ném ValueError
        with self.assertRaises(ValueError):
            stream.read_incoming(offset=6000, data=b"Y" * 2000, is_eom=False)

    def test_invalid_read_inputs(self) -> None:
        """Kiểm tra xử lý ngoại lệ khi tham số read_incoming không hợp lệ."""
        stream = Stream(stream_id=1)

        with self.assertRaises(ValueError):
            stream.read_incoming(offset=-1, data=b"data", is_eom=True)

        with self.assertRaises(TypeError):
            stream.read_incoming(offset="invalid", data=b"data", is_eom=True)  # type: ignore

        with self.assertRaises(TypeError):
            stream.read_incoming(offset=0, data="not_bytes", is_eom=True)  # type: ignore

        with self.assertRaises(TypeError):
            stream.read_incoming(offset=0, data=b"data", is_eom="not_bool")  # type: ignore


if __name__ == "__main__":
    unittest.main()
