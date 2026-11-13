"""Tests for the shared packet header format."""

from pathlib import Path
import struct
import sys
import unittest

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from common.constants import HEADER_FORMAT, HEADER_SIZE, HEADER_STRUCT_FMT
from common.packet import pack, unpack


class TestPacketHeader(unittest.TestCase):
    def test_constants_describe_the_22_byte_wire_header(self) -> None:
        self.assertEqual(HEADER_STRUCT_FMT, HEADER_FORMAT)
        self.assertEqual(struct.calcsize(HEADER_FORMAT), HEADER_SIZE)
        self.assertEqual(HEADER_SIZE, 22)

    def test_pack_and_unpack_round_trip(self) -> None:
        payload = b"stream payload"
        raw = pack(
            msg_type=3,
            conn_id=0x12345678,
            seq_num=9,
            flags=1,
            stream_id=7,
            offset=1024,
            payload=payload,
        )

        header, decoded_payload = unpack(raw)

        self.assertEqual(len(raw[:HEADER_SIZE]), 22)
        self.assertEqual(header.conn_id, 0x12345678)
        self.assertEqual(header.seq_num, 9)
        self.assertEqual(header.payload_len, len(payload))
        self.assertEqual(decoded_payload, payload)


if __name__ == "__main__":
    unittest.main()
