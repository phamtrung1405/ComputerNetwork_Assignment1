"""
tests/test_integration.py
~~~~~~~~~~~~~~~~~~~~~~~~~
Bộ kiểm thử tích hợp (Integration Tests) liên module cho STREAMCHAT:
- Tích hợp M2 (crypto.py, reliability.py, stream.py) với tầng Packet (packet.py) và Connection (connection.py).
- Kiểm chứng toàn diện 8 yêu cầu tích hợp:
  1. Hai endpoint bắt tay và dẫn xuất cùng một khóa phiên đối xứng (X25519 + HKDF).
  2. AEAD AES-256-GCM từ chối mọi gói tin bị can thiệp (tampered ciphertext / AAD header).
  3. Phân mảnh (Stream.write) và ráp nối (Stream.read_incoming) bảo toàn 100% dữ liệu gốc.
  4. Gói tin trùng lặp (duplicate) hoặc đảo lộn thứ tự (out-of-order) không làm hỏng message.
  5. Sổ sách theo dõi SACK và truyền lại (ReliabilityManager) hoạt động nhất quán, không sót gói.
  6. RTT/RTO (RFC 6298) và CUBIC (RFC 8312) phản hồi chính xác với xác nhận và mất gói.
  7. Truyền thành công file lớn 10 MB dưới kịch bản mất gói 5% tái lập được (reproducible packet loss).
  8. So khớp băm SHA-256 giữa file gốc và file nhận được đạt độ chính xác 100%.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import random
import socket
import sys
import time
import unittest

# Đảm bảo src có trong sys.path
SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from common import packet
from common.constants import (
    FLAG_EOM,
    FLAG_NONE,
    PKT_DATA,
    PKT_HELLO,
    PKT_HELLO_ACK,
)
from common.crypto import (
    decrypt,
    derive_shared_secret,
    encrypt,
    generate_keypair,
)
from cryptography.exceptions import InvalidTag
from transport.connection import Connection
from transport.reliability import ReliabilityManager
from transport.stream import Stream, SAFE_MAX_APP_PAYLOAD


class TestEndpointKeyAgreement(unittest.TestCase):
    """Yêu cầu 1: Hai endpoint hoàn tất bắt tay và dẫn xuất cùng một khóa phiên đối xứng."""

    def test_loopback_udp_handshake_derives_identical_session_key(self) -> None:
        """Alice (Client) và Bob (Server) trao đổi HELLO/HELLO_ACK qua UDP loopback thực tế."""
        sock_a = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock_a.bind(("127.0.0.1", 0))
        addr_a = sock_a.getsockname()

        sock_b = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock_b.bind(("127.0.0.1", 0))
        addr_b = sock_b.getsockname()

        try:
            conn_a = Connection(sock=sock_a, remote_addr=addr_b, conn_id=1001, is_server=False)
            conn_b = Connection(sock=sock_b, remote_addr=addr_a, conn_id=1001, is_server=True)

            # 1. Alice gửi PKT_HELLO
            conn_a.handle_handshake()

            # 2. Bob nhận PKT_HELLO và phản hồi PKT_HELLO_ACK
            data_hello, _ = sock_b.recvfrom(65535)
            conn_b.receive_packet(data_hello)

            # 3. Alice nhận PKT_HELLO_ACK
            data_ack, _ = sock_a.recvfrom(65535)
            conn_a.receive_packet(data_ack)

            # Kiểm tra trạng thái bắt tay
            self.assertTrue(conn_a.handshake_complete.is_set())
            self.assertTrue(conn_b.handshake_complete.is_set())
            self.assertIsNotNone(conn_a._session_key)
            self.assertIsNotNone(conn_b._session_key)

            # Khóa phiên đối xứng tại 2 đầu bắt buộc phải trùng khớp 100%
            self.assertEqual(conn_a._session_key, conn_b._session_key)
            self.assertEqual(len(conn_a._session_key), 32)
        finally:
            sock_a.close()
            sock_b.close()

    def test_encrypted_data_authenticates_header_and_direction(self) -> None:
        """Encrypted transport data must use peer-direction nonces and authenticate its header."""
        sock_a = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock_a.bind(("127.0.0.1", 0))
        sock_a.settimeout(1.0)
        sock_b = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock_b.bind(("127.0.0.1", 0))
        sock_b.settimeout(1.0)
        received: list[tuple[int, bytes]] = []

        try:
            conn_a = Connection(sock=sock_a, remote_addr=sock_b.getsockname(), is_server=False)
            conn_b = Connection(
                sock=sock_b,
                remote_addr=sock_a.getsockname(),
                is_server=True,
                on_app_message=lambda stream_id, data: received.append((stream_id, data)),
            )

            conn_a.handle_handshake()
            hello, _ = sock_b.recvfrom(65535)
            conn_b.receive_packet(hello)
            hello_ack, _ = sock_a.recvfrom(65535)
            conn_a.receive_packet(hello_ack)

            conn_a.send_packet(PKT_DATA, 1, b"authenticated message", flags=FLAG_EOM)
            raw_packet, _ = sock_b.recvfrom(65535)
            conn_b.receive_packet(raw_packet)
            self.assertEqual(received, [(1, b"authenticated message")])

            tampered_packet = bytearray(raw_packet)
            tampered_packet[10] ^= 0x01
            with self.assertLogs("transport.connection", level="WARNING"):
                conn_b.receive_packet(bytes(tampered_packet))
            self.assertEqual(received, [(1, b"authenticated message")])
        finally:
            sock_a.close()
            sock_b.close()


class TestTamperedCiphertextRejection(unittest.TestCase):
    """Yêu cầu 2: AEAD AES-256-GCM từ chối mọi ciphertext bị can thiệp và bảo vệ toàn vẹn."""

    def setUp(self) -> None:
        priv_a, pub_a = generate_keypair()
        priv_b, pub_b = generate_keypair()
        self.session_key = derive_shared_secret(priv_a, pub_b)

    def test_tampered_payload_rejected_by_decrypt(self) -> None:
        """Đảo bit trong payload ciphertext khiến giải mã thất bại với ngoại lệ InvalidTag."""
        plaintext = b"Sensitive transaction data"
        seq_num = 1
        ciphertext = bytearray(encrypt(plaintext, self.session_key, seq_num))

        # Đảo 1 bit
        ciphertext[0] ^= 0x01

        with self.assertRaises(InvalidTag):
            decrypt(bytes(ciphertext), self.session_key, seq_num)

    def test_tampered_header_metadata_rejected(self) -> None:
        """Kẻ tấn công sửa đổi metadata trong Header (AAD) khiến AEAD từ chối giải mã."""
        plaintext = b"Message with Header AAD protection"
        header_aad = packet.pack(
            msg_type=PKT_DATA,
            conn_id=500,
            seq_num=10,
            flags=FLAG_NONE,
            stream_id=1,
            offset=0,
            payload=b"",
        )[:22]

        ciphertext = encrypt(plaintext, self.session_key, 10, associated_data=header_aad)

        # Giả lập sửa đổi stream_id trong header AAD
        tampered_header = bytearray(header_aad)
        tampered_header[9] ^= 0x01  # Sửa đổi bit trong stream_id

        with self.assertRaises(InvalidTag):
            decrypt(ciphertext, self.session_key, 10, associated_data=bytes(tampered_header))


class TestStreamFragmentationAndReassembly(unittest.TestCase):
    """Yêu cầu 3 & 4: Phân mảnh và ráp nối bảo toàn 100% dữ liệu gốc dưới xáo trộn và trùng lặp."""

    def test_end_to_end_stream_transmission(self) -> None:
        """Dữ liệu lớn truyền qua Stream.write -> Encrypt -> Decrypt -> Stream.read_incoming."""
        priv_a, pub_a = generate_keypair()
        priv_b, pub_b = generate_keypair()
        key = derive_shared_secret(priv_a, pub_b)

        original_data = os.urandom(50000)  # ~50 KB (37 chunks)
        chunks_in_flight: list[tuple[int, int, bytes, int]] = []

        def send_cb(sid: int, offset: int, chunk: bytes, flags: int) -> None:
            chunks_in_flight.append((sid, offset, chunk, flags))

        sender_stream = Stream(stream_id=1, send_callback=send_cb, chunk_size=SAFE_MAX_APP_PAYLOAD)
        receiver_stream = Stream(stream_id=1, chunk_size=SAFE_MAX_APP_PAYLOAD)

        # 1. Phân mảnh
        sender_stream.write(original_data)
        self.assertGreater(len(chunks_in_flight), 1)

        # 2. Mã hóa, giải mã và nạp vào receiver stream
        assembled_message = None
        for seq, (sid, offset, chunk, flags) in enumerate(chunks_in_flight):
            ct = encrypt(chunk, key, seq)
            pt = decrypt(ct, key, seq)
            is_eom = bool(flags & FLAG_EOM)
            msg = receiver_stream.read_incoming(offset, pt, is_eom)
            if msg is not None:
                assembled_message = msg

        self.assertIsNotNone(assembled_message)
        self.assertEqual(assembled_message, original_data)

    def test_duplicate_and_out_of_order_fragments_do_not_corrupt(self) -> None:
        """Gói tin bị tráo đổi thứ tự và trùng lặp không làm sai lệch message."""
        c1 = b"Alpha_"
        c2 = b"Bravo_"
        c3 = b"Charlie"

        receiver_stream = Stream(stream_id=1)

        # Thứ tự đến: Chunk 3 (EOM) -> Duplicate Chunk 3 -> Chunk 1 -> Chunk 2 -> Duplicate Chunk 1
        self.assertIsNone(receiver_stream.read_incoming(offset=12, data=c3, is_eom=True))
        self.assertIsNone(receiver_stream.read_incoming(offset=12, data=c3, is_eom=True))  # Duplicate
        self.assertIsNone(receiver_stream.read_incoming(offset=0, data=c1, is_eom=False))
        # Chunk 2 đến làm đầy chuỗi
        res = receiver_stream.read_incoming(offset=6, data=c2, is_eom=False)
        self.assertEqual(res, c1 + c2 + c3)

        # Duplicate của Chunk 1 đến sau khi đã ráp nối xong
        dup1 = receiver_stream.read_incoming(offset=0, data=c1, is_eom=False)
        self.assertIsNone(dup1)  # Phải bị loại bỏ an toàn, không được làm hỏng stream


class TestSackAndRetransmissionConsistency(unittest.TestCase):
    """Yêu cầu 5: Sổ sách SACK và truyền lại hoạt động nhất quán, không sót hoặc gửi thừa."""

    def test_selective_ack_clears_only_acked_packets(self) -> None:
        rel = ReliabilityManager()

        # Alice gửi 5 gói
        for seq in range(1, 6):
            rel.mark_sent(seq, f"pkt_{seq}".encode())

        self.assertEqual(rel.in_flight, 5)

        # Bob nhận được gói 1, 2 và 5 (rơi mất gói 3, 4)
        rel.process_sack([(1, 2), (5, 5)])

        self.assertEqual(rel.in_flight, 2)
        self.assertEqual(sorted(rel._unacked.keys()), [3, 4])

        # Quá hạn RTO: Alice retransmit
        now = time.monotonic()
        for pkt in rel._unacked.values():
            pkt.sent_time = now - (rel.rto + 0.1)

        retrans = rel.get_retransmits()
        self.assertEqual(len(retrans), 2)
        # Chỉ truyền lại gói 3 và 4
        self.assertEqual(retrans[0], b"pkt_3")
        self.assertEqual(retrans[1], b"pkt_4")

        # Bob nhận được và ACK nốt [3, 4]
        rel.process_sack([(3, 4)])
        self.assertEqual(rel.in_flight, 0)
        self.assertEqual(len(rel.get_retransmits()), 0)


class TestRttAndCubicDynamicsUnderLoss(unittest.TestCase):
    """Yêu cầu 6: RTT/RTO và CUBIC phản hồi chính xác khi nhận xác nhận và khi mất gói."""

    def test_rtt_and_cubic_dynamics(self) -> None:
        rel = ReliabilityManager()

        # 1. Slow start
        rel.update_cwnd(is_loss=False, acked_packets=5)
        self.assertEqual(rel.cwnd, 15.0)

        # 2. Cập nhật RTT
        rel.update_rtt(0.08)
        self.assertEqual(rel.srtt, 0.08)
        self.assertAlmostEqual(rel.rto, 0.08 + max(0.05, 4.0 * 0.04))  # 0.24s

        # 3. Sự kiện mất gói: giảm cửa sổ multiplicatively (beta = 0.7)
        old_cwnd = rel.cwnd
        rel.update_cwnd(is_loss=True)
        self.assertAlmostEqual(rel.cwnd, old_cwnd * 0.7, delta=0.01)
        self.assertEqual(rel._w_max, old_cwnd)


class TestLargeFileTransfer10MBUnderPacketLoss(unittest.TestCase):
    """Yêu cầu 7 & 8: Truyền file 10 MB dưới kịch bản mất gói 5% tái lập được và khớp hash SHA-256."""

    def test_10mb_transfer_with_reproducible_loss(self) -> None:
        """Truyền payload 10 MB (7699 chunks) với tỷ lệ mất gói 5% xác định bằng seed cố định."""
        payload_size = 10 * 1024 * 1024  # 10 MB = 10,485,760 bytes
        original_data = os.urandom(payload_size)
        original_sha256 = hashlib.sha256(original_data).hexdigest()

        # Khóa phiên
        priv_a, pub_a = generate_keypair()
        priv_b, pub_b = generate_keypair()
        session_key = derive_shared_secret(priv_a, pub_b)

        # Tầng truyền tải Alice
        alice_rel = ReliabilityManager()
        alice_rel.rto = 0.01  # Tối ưu RTO nhanh cho môi trường kiểm thử

        # Tầng truyền tải Bob
        bob_stream = Stream(stream_id=1, chunk_size=SAFE_MAX_APP_PAYLOAD)

        # Hàng đợi gói tin đang bay
        packets_to_send: list[tuple[int, bytes]] = []
        seq_counter = 0

        def send_callback(sid: int, offset: int, chunk: bytes, flags: int) -> None:
            nonlocal seq_counter
            seq_counter += 1
            # Mã hóa AES-256-GCM
            ciphertext = encrypt(chunk, session_key, seq_counter)
            raw_pkt = packet.pack(
                msg_type=PKT_DATA,
                conn_id=999,
                seq_num=seq_counter,
                flags=flags,
                stream_id=sid,
                offset=offset,
                payload=ciphertext,
            )
            alice_rel.mark_sent(seq_counter, raw_pkt)
            packets_to_send.append((seq_counter, raw_pkt))

        alice_stream = Stream(stream_id=1, send_callback=send_callback, chunk_size=SAFE_MAX_APP_PAYLOAD)

        # 1. Phân mảnh 10 MB
        t0 = time.monotonic()
        total_written = alice_stream.write(original_data)
        self.assertEqual(total_written, payload_size)

        expected_chunks = (payload_size + SAFE_MAX_APP_PAYLOAD - 1) // SAFE_MAX_APP_PAYLOAD
        self.assertEqual(len(packets_to_send), expected_chunks)

        # 2. Kênh truyền mô phỏng mất gói 5% có thể tái lập (seed 42)
        rng = random.Random(42)
        loss_rate = 0.05
        dropped_packets = 0
        retransmitted_packets = 0
        received_payload = None

        # Vòng lặp truyền tải và khắc phục mất gói
        while alice_rel.in_flight > 0:
            current_batch = list(packets_to_send)
            packets_to_send.clear()

            for seq, raw in current_batch:
                # Mô phỏng mất gói
                if rng.random() < loss_rate:
                    dropped_packets += 1
                    continue

                # Bob nhận được gói tin
                hdr, ct = packet.unpack(raw)
                pt = decrypt(ct, session_key, hdr.seq_num)
                is_eom = bool(hdr.flags & FLAG_EOM)
                assembled = bob_stream.read_incoming(hdr.offset, pt, is_eom)
                if assembled is not None:
                    received_payload = assembled

                # Bob gửi ACK về Alice
                alice_rel.process_sack([(hdr.seq_num, hdr.seq_num)])

            # Nếu vẫn còn gói chưa ACK, Alice kích hoạt retransmission
            if alice_rel.in_flight > 0:
                now = time.monotonic()
                for pkt in alice_rel._unacked.values():
                    pkt.sent_time = now - (alice_rel.rto + 0.1)

                retrans = alice_rel.get_retransmits()
                retransmitted_packets += len(retrans)
                for raw in retrans:
                    hdr, _ = packet.unpack(raw)
                    packets_to_send.append((hdr.seq_num, raw))

        t1 = time.monotonic()

        # Kiểm tra hoàn tất
        self.assertEqual(alice_rel.in_flight, 0)
        self.assertGreater(dropped_packets, 0, "Kịch bản kiểm thử phải gây ra mất gói thực tế")
        self.assertEqual(retransmitted_packets, dropped_packets, "Số gói truyền lại phải bù đắp chính xác số gói mất")

        if received_payload is None:
            complete_msgs = bob_stream.get_complete_messages()
            self.assertTrue(len(complete_msgs) > 0)
            received_payload = complete_msgs[0]

        # Kiểm tra tính toàn vẹn tuyệt đối qua mã băm SHA-256
        received_sha256 = hashlib.sha256(received_payload).hexdigest()
        self.assertEqual(received_sha256, original_sha256)
        self.assertEqual(len(received_payload), payload_size)


if __name__ == "__main__":
    unittest.main()
