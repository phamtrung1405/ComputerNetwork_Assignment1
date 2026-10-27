"""
tests/test_reliability.py
~~~~~~~~~~~~~~~~~~~~~~~~~
Bộ kiểm thử đơn vị cho module quản lý độ tin cậy và truyền lại (src/transport/reliability.py).
Kiểm thử toàn diện:
- Xử lý SACK chuẩn (Single range, Contiguous range, Discontiguous ranges, DoS range protection).
- Xử lý ACK trùng lặp, ACK cũ (stale), ACK cho gói tin chưa từng gửi.
- Xử lý dữ liệu đầu vào không hợp lệ (malformed/invalid inputs).
- Cơ chế lựa chọn gói tin cần truyền lại (Retransmission Selection), Exponential Backoff và chống sụp đổ cửa sổ CUBIC (ADR-009).
- Luật Karn (RFC 6298) không đo RTT cho gói tin truyền lại.
"""

from __future__ import annotations

import math
from pathlib import Path
import sys
import time
import unittest

# Đảm bảo src có trong sys.path
SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from common.constants import (
    INITIAL_CWND,
    INITIAL_RTO,
    MAX_CWND,
    MAX_RTO,
    MIN_CWND,
    MIN_RTO,
    SSTHRESH_INITIAL,
)
from transport.reliability import ReliabilityManager, _InFlightPacket


class TestSackProcessing(unittest.TestCase):
    """Kiểm thử xử lý Selective ACK (SACK) trong các trường hợp thông thường và nâng cao."""

    def setUp(self) -> None:
        self.rel = ReliabilityManager()

    def test_single_packet_sack_acknowledgement(self) -> None:
        """Xác nhận 1 gói tin đơn lẻ: gói tin bị loại khỏi _unacked, cwnd tăng và RTT được cập nhật."""
        self.rel.mark_sent(1, b"packet_1")
        self.assertEqual(self.rel.in_flight, 1)

        # Giả lập thời gian trôi qua 50ms để có mẫu RTT
        time.sleep(0.01)
        self.rel.process_sack([(1, 1)])

        self.assertEqual(self.rel.in_flight, 0)
        self.assertNotIn(1, self.rel._unacked)
        self.assertIsNotNone(self.rel.srtt)
        self.assertGreater(self.rel.cwnd, INITIAL_CWND)

    def test_contiguous_range_sack_acknowledgement(self) -> None:
        """Xác nhận một dải liên tục (ví dụ: [0, 4]): tất cả các gói trong dải phải được xóa sạch."""
        for seq in range(5):
            self.rel.mark_sent(seq, f"packet_{seq}".encode())

        self.assertEqual(self.rel.in_flight, 5)
        self.rel.process_sack([(0, 4)])

        self.assertEqual(self.rel.in_flight, 0)
        for seq in range(5):
            self.assertNotIn(seq, self.rel._unacked)

    def test_discontiguous_selective_sack_acknowledgement(self) -> None:
        """Xác nhận không liên tục (SACK gaps): chỉ xóa các gói trong dải, các gói bị thiếu vẫn giữ nguyên."""
        # Gửi 6 gói: 0, 1, 2, 3, 4, 5
        for seq in range(6):
            self.rel.mark_sent(seq, f"packet_{seq}".encode())

        # Bên nhận nhận được [0..1] và [4..5], bị rơi gói 2 và 3
        self.rel.process_sack([(0, 1), (4, 5)])

        self.assertEqual(self.rel.in_flight, 2)
        self.assertNotIn(0, self.rel._unacked)
        self.assertNotIn(1, self.rel._unacked)
        self.assertIn(2, self.rel._unacked)
        self.assertIn(3, self.rel._unacked)
        self.assertNotIn(4, self.rel._unacked)
        self.assertNotIn(5, self.rel._unacked)

    def test_large_range_boundary_dos_protection(self) -> None:
        """Dải SACK cực lớn (ví dụ [0, 10_000_000]) không được làm treo CPU / event loop."""
        self.rel.mark_sent(42, b"packet_42")
        start_time = time.monotonic()

        # Giả lập range khổng lồ do attacker hoặc lỗi bit trên mạng
        self.rel.process_sack([(0, 10_000_000)])
        elapsed = time.monotonic() - start_time

        # Phải hoàn thành dưới 50ms, không được duyệt 10 triệu vòng lặp
        self.assertLess(elapsed, 0.05)
        self.assertEqual(self.rel.in_flight, 0)

    def test_karns_algorithm_ignores_retransmitted_packets_for_rtt(self) -> None:
        """Luật Karn: Gói tin đã từng bị truyền lại (retransmit_count > 0) không được lấy mẫu RTT."""
        self.rel.mark_sent(10, b"packet_10")
        pkt = self.rel._unacked[10]
        pkt.retransmit_count = 1  # Đánh dấu đã retransmit

        # Trước khi ACK, srtt là None
        self.assertIsNone(self.rel.srtt)

        self.rel.process_sack([(10, 10)])
        self.assertEqual(self.rel.in_flight, 0)
        # Vì là gói truyền lại, SRTT không được cập nhật mẫu sai lệch
        self.assertIsNone(self.rel.srtt)


class TestDuplicateAndStaleAcknowledgements(unittest.TestCase):
    """Kiểm thử xử lý ACK trùng lặp, ACK cũ và ACK cho gói tin chưa từng gửi."""

    def setUp(self) -> None:
        self.rel = ReliabilityManager()

    def test_duplicate_ack_does_not_inflate_cwnd(self) -> None:
        """Nhận ACK trùng lặp cho gói đã được xác nhận trước đó: cwnd không được phép tăng."""
        self.rel.mark_sent(1, b"packet_1")
        self.rel.process_sack([(1, 1)])
        initial_cwnd = self.rel.cwnd

        # Gửi lại ACK trùng lặp lần 2
        self.rel.process_sack([(1, 1)])
        self.assertEqual(self.rel.cwnd, initial_cwnd)
        self.assertGreaterEqual(self.rel.duplicate_acks, 1)

    def test_ack_for_never_sent_packet_handled_safely(self) -> None:
        """Nhận ACK cho sequence number chưa từng tồn tại: không gây lỗi ngoại lệ và không tăng cwnd."""
        initial_cwnd = self.rel.cwnd
        self.rel.process_sack([(999, 999)])
        self.assertEqual(self.rel.in_flight, 0)
        self.assertEqual(self.rel.cwnd, initial_cwnd)
        self.assertGreaterEqual(self.rel.duplicate_acks, 1)

    def test_stale_ack_behind_current_window(self) -> None:
        """Nhận ACK cũ nằm ngoài cửa sổ gửi hiện tại không làm ảnh hưởng các gói đang bay."""
        self.rel.mark_sent(10, b"packet_10")
        self.rel.mark_sent(11, b"packet_11")

        # Nhận ACK cho dải cũ [0, 5]
        self.rel.process_sack([(0, 5)])
        self.assertEqual(self.rel.in_flight, 2)
        self.assertIn(10, self.rel._unacked)
        self.assertIn(11, self.rel._unacked)

    def test_consecutive_dupacks_tracking_and_reset(self) -> None:
        """Theo dõi số lượng duplicate ACKs liên tiếp và reset khi có dữ liệu mới được xác nhận."""
        self.rel.mark_sent(5, b"pkt_5")
        self.assertEqual(self.rel.consecutive_dupacks, 0)

        # Gửi 3 duplicate ACKs
        self.rel.process_sack([(1, 1)])
        self.rel.process_sack([(1, 1)])
        self.rel.process_sack([(1, 1)])
        self.assertEqual(self.rel.consecutive_dupacks, 3)

        # Giờ xác nhận gói 5 (new progress) -> consecutive_dupacks phải reset về 0
        self.rel.process_sack([(5, 5)])
        self.assertEqual(self.rel.consecutive_dupacks, 0)
        self.assertEqual(self.rel.in_flight, 0)


class TestInvalidInputs(unittest.TestCase):
    """Kiểm thử xử lý ngoại lệ với dữ liệu đầu vào không hợp lệ."""

    def setUp(self) -> None:
        self.rel = ReliabilityManager()

    def test_mark_sent_negative_sequence(self) -> None:
        with self.assertRaises(ValueError):
            self.rel.mark_sent(-1, b"data")

    def test_mark_sent_invalid_type_sequence(self) -> None:
        with self.assertRaises((ValueError, TypeError)):
            self.rel.mark_sent("invalid_seq", b"data")  # type: ignore

    def test_mark_sent_invalid_payload_type(self) -> None:
        with self.assertRaises(TypeError):
            self.rel.mark_sent(1, "not_bytes")  # type: ignore

    def test_process_sack_invalid_container_type(self) -> None:
        with self.assertRaises(TypeError):
            self.rel.process_sack("not_a_list")  # type: ignore
        with self.assertRaises(TypeError):
            self.rel.process_sack(None)  # type: ignore

    def test_process_sack_invalid_element_structure(self) -> None:
        with self.assertRaises((ValueError, TypeError)):
            self.rel.process_sack([(1,)])  # type: ignore
        with self.assertRaises((ValueError, TypeError)):
            self.rel.process_sack([(1, 2, 3)])  # type: ignore

    def test_process_sack_non_integer_boundaries(self) -> None:
        with self.assertRaises(TypeError):
            self.rel.process_sack([("a", "b")])  # type: ignore
        with self.assertRaises(TypeError):
            self.rel.process_sack([(1.0, 2.0)])  # type: ignore

    def test_process_sack_negative_boundaries(self) -> None:
        with self.assertRaises(ValueError):
            self.rel.process_sack([(-5, 10)])
        with self.assertRaises(ValueError):
            self.rel.process_sack([(0, -2)])

    def test_process_sack_inverted_range(self) -> None:
        with self.assertRaises(ValueError):
            self.rel.process_sack([(10, 5)])

    def test_update_rtt_invalid_input(self) -> None:
        with self.assertRaises(ValueError):
            self.rel.update_rtt(-0.5)
        with self.assertRaises(TypeError):
            self.rel.update_rtt("invalid_rtt")  # type: ignore


class TestRetransmissionSelection(unittest.TestCase):
    """Kiểm thử thuật toán lựa chọn gói tin cần truyền lại và kiểm soát tắc nghẽn."""

    def setUp(self) -> None:
        self.rel = ReliabilityManager()

    def test_no_retransmits_when_within_rto_window(self) -> None:
        """Gói tin vừa mới gửi chưa vượt quá RTO thì không được đưa vào danh sách truyền lại."""
        self.rel.mark_sent(1, b"pkt_1")
        self.rel.mark_sent(2, b"pkt_2")

        retransmits = self.rel.get_retransmits()
        self.assertEqual(len(retransmits), 0)

    def test_expired_packet_selected_for_retransmission(self) -> None:
        """Gói tin vượt quá RTO phải được chọn để truyền lại và cập nhật sent_time mới."""
        self.rel.mark_sent(1, b"pkt_1")
        now = time.monotonic()
        # Giả lập gói 1 đã gửi cách đây (RTO + 0.1s)
        self.rel._unacked[1].sent_time = now - (self.rel.rto + 0.1)

        retransmits = self.rel.get_retransmits()
        self.assertEqual(len(retransmits), 1)
        self.assertEqual(retransmits[0], b"pkt_1")
        self.assertEqual(self.rel._unacked[1].retransmit_count, 1)
        # sent_time phải được cập nhật lại xấp xỉ thời điểm hiện tại
        self.assertAlmostEqual(self.rel._unacked[1].sent_time, now, delta=0.5)

    def test_selective_retransmission_only_expired_packets(self) -> None:
        """Chỉ những gói đã hết hạn mới được truyền lại, các gói gửi sau vẫn còn hạn thì giữ nguyên."""
        now = time.monotonic()
        self.rel.mark_sent(1, b"pkt_1")
        self.rel.mark_sent(2, b"pkt_2")

        # Gói 1 hết hạn, gói 2 vừa mới gửi
        self.rel._unacked[1].sent_time = now - (self.rel.rto + 0.5)
        self.rel._unacked[2].sent_time = now

        retransmits = self.rel.get_retransmits()
        self.assertEqual(len(retransmits), 1)
        self.assertEqual(retransmits[0], b"pkt_1")
        self.assertEqual(self.rel._unacked[1].retransmit_count, 1)
        self.assertEqual(self.rel._unacked[2].retransmit_count, 0)

    def test_acknowledged_packet_never_retransmitted(self) -> None:
        """Gói tin đã được ACK tuyệt đối không bao giờ xuất hiện trong get_retransmits."""
        now = time.monotonic()
        self.rel.mark_sent(1, b"pkt_1")
        self.rel._unacked[1].sent_time = now - (self.rel.rto + 0.5)

        # Bên nhận gửi SACK cho gói 1
        self.rel.process_sack([(1, 1)])

        retransmits = self.rel.get_retransmits()
        self.assertEqual(len(retransmits), 0)

    def test_exponential_backoff_on_consecutive_timeouts(self) -> None:
        """Khi xảy ra timeout liên tiếp, RTO phải nhân đôi theo chuẩn RFC 6298."""
        self.rel.mark_sent(1, b"pkt_1")
        initial_rto = self.rel.rto

        now = time.monotonic()
        self.rel._unacked[1].sent_time = now - (initial_rto + 0.1)

        # Lần 1: Timeout -> RTO phải gấp đôi
        self.rel.get_retransmits()
        self.assertEqual(self.rel.rto, min(initial_rto * 2.0, MAX_RTO))

        # Giả lập timeout lần 2
        current_rto = self.rel.rto
        self.rel._unacked[1].sent_time = now - (current_rto + 0.1)
        self.rel.get_retransmits()
        self.assertEqual(self.rel.rto, min(current_rto * 2.0, MAX_RTO))

    def test_cubic_multi_loss_collapse_prevention_adr009(self) -> None:
        """Nhiều gói cùng timeout trong 1 tick: cwnd chỉ được giảm tối đa 1 lần (ADR-009)."""
        # Gửi 5 gói
        now = time.monotonic()
        for seq in range(5):
            self.rel.mark_sent(seq, f"pkt_{seq}".encode())
            self.rel._unacked[seq].sent_time = now - (self.rel.rto + 0.5)

        initial_cwnd = self.rel.cwnd
        retransmits = self.rel.get_retransmits()

        # Cả 5 gói đều được truyền lại
        self.assertEqual(len(retransmits), 5)

        # Kiểm tra cwnd không bị giảm 5 lần liên tiếp (0.7^5 = 0.168)
        # Chỉ giảm đúng 1 lần: cwnd = initial_cwnd * CUBIC_BETA (hoặc ssthresh)
        expected_reduced_cwnd = max(initial_cwnd * 0.7, float(MIN_CWND))
        self.assertAlmostEqual(self.rel.cwnd, expected_reduced_cwnd, delta=0.01)

    def test_circuit_breaker_max_retransmits(self) -> None:
        """Vượt quá ngưỡng max_retransmits (8 lần): dừng retransmit và ghi nhận failed packet."""
        self.rel.mark_sent(1, b"pkt_dead")
        self.rel._unacked[1].retransmit_count = self.rel.max_retransmits

        now = time.monotonic()
        self.rel._unacked[1].sent_time = now - (self.rel.rto + 0.5)

        retransmits = self.rel.get_retransmits()
        # Không được tiếp tục gửi lại gói đã chết
        self.assertEqual(len(retransmits), 0)
        self.assertIn(1, self.rel.failed_packets)


class TestRttEstimationRfc6298(unittest.TestCase):
    """Kiểm thử tính toán RTT và RTO theo RFC 6298."""

    def setUp(self) -> None:
        self.rel = ReliabilityManager()

    def test_first_rtt_sample_calculation(self) -> None:
        """Mẫu RTT đầu tiên: SRTT = R, RTTVAR = R/2, RTO = SRTT + max(G, 4*RTTVAR)."""
        r = 0.1  # 100ms
        self.rel.update_rtt(r)
        self.assertEqual(self.rel.srtt, 0.1)
        self.assertEqual(self.rel.rttvar, 0.05)
        expected_rto = 0.1 + max(0.05, 4.0 * 0.05)  # 0.1 + 0.2 = 0.3s
        self.assertAlmostEqual(self.rel.rto, expected_rto)

    def test_subsequent_rtt_sample_calculation(self) -> None:
        """Mẫu RTT tiếp theo: RTTVAR cập nhật từ SRTT cũ, sau đó SRTT cập nhật theo alpha=0.125."""
        self.rel.update_rtt(0.1)
        # Mẫu 2: r2 = 0.2
        # RTTVAR = (1 - 0.25) * 0.05 + 0.25 * |0.1 - 0.2| = 0.0375 + 0.025 = 0.0625
        # SRTT = (1 - 0.125) * 0.1 + 0.125 * 0.2 = 0.0875 + 0.025 = 0.1125
        # RTO = 0.1125 + max(0.05, 4.0 * 0.0625) = 0.1125 + 0.25 = 0.3625
        self.rel.update_rtt(0.2)
        self.assertAlmostEqual(self.rel.srtt, 0.1125)
        self.assertAlmostEqual(self.rel.rttvar, 0.0625)
        self.assertAlmostEqual(self.rel.rto, 0.3625)

    def test_rto_clamped_to_min_rto(self) -> None:
        """RTT cực nhỏ (1ms) tạo ra RTO < MIN_RTO -> phải ép về MIN_RTO (0.2s)."""
        self.rel.update_rtt(0.001)
        self.assertEqual(self.rel.rto, MIN_RTO)

    def test_rto_clamped_to_max_rto(self) -> None:
        """RTT cực lớn (15s) tạo ra RTO > MAX_RTO -> phải ép về MAX_RTO (10.0s)."""
        self.rel.update_rtt(15.0)
        self.assertEqual(self.rel.rto, MAX_RTO)

    def test_rto_resets_backoff_on_valid_sample(self) -> None:
        """Mẫu RTT mới hợp lệ sau chuỗi timeout phải xóa bỏ trạng thái Exponential Backoff."""
        self.rel.rto = 4.0
        self.rel._consecutive_timeouts = 2

        self.rel.update_rtt(0.1)
        self.assertEqual(self.rel._consecutive_timeouts, 0)
        self.assertAlmostEqual(self.rel.rto, 0.3)


class TestCubicCongestionControlRfc8312(unittest.TestCase):
    """Kiểm thử thuật toán kiểm soát tắc nghẽn CUBIC (RFC 8312)."""

    def setUp(self) -> None:
        self.rel = ReliabilityManager()

    def test_slow_start_growth(self) -> None:
        """Trong Slow Start (cwnd < ssthresh), mỗi gói ACK làm tăng cwnd tương ứng."""
        self.rel.cwnd = 10.0
        self.rel.ssthresh = 64.0
        self.rel.update_cwnd(is_loss=False, acked_packets=3)
        self.assertEqual(self.rel.cwnd, 13.0)

    def test_slow_start_transition_to_congestion_avoidance(self) -> None:
        """Khi chạm ngưỡng ssthresh, Slow Start dừng lại và chuyển tiếp sang Congestion Avoidance."""
        self.rel.cwnd = 63.0
        self.rel.ssthresh = 64.0
        self.rel.update_cwnd(is_loss=False, acked_packets=5)
        self.assertEqual(self.rel.cwnd, 64.0)
        self.assertIsNone(self.rel._epoch_start)

    def test_congestion_avoidance_concave_region(self) -> None:
        """Vùng Concave (t < K): cwnd tăng dần hướng về W_max."""
        self.rel.cwnd = 20.0
        # Gây mất gói để thiết lập W_max = 20.0, ssthresh = cwnd = 14.0
        self.rel.update_cwnd(is_loss=True)
        self.assertEqual(self.rel._w_max, 20.0)
        self.assertEqual(self.rel.cwnd, 14.0)
        self.assertEqual(self.rel.ssthresh, 14.0)

        # Tính K = ((20 - 14) / 0.4)^(1/3) = (15)^(1/3) ~ 2.466s
        # Mô phỏng nhận ACK tại thời điểm t < K (ví dụ t = 0.5s)
        self.rel.srtt = 0.1
        self.rel.update_cwnd(is_loss=False, acked_packets=1)
        # cwnd phải tăng nhưng chưa vượt quá W_max (20.0)
        self.assertGreater(self.rel.cwnd, 14.0)
        self.assertLess(self.rel.cwnd, 20.0)

    def test_congestion_avoidance_convex_probing(self) -> None:
        """Vùng Convex (t > K): Sau khi đạt W_max, CUBIC tăng tốc thăm dò băng thông mới."""
        self.rel.cwnd = 20.0
        self.rel.update_cwnd(is_loss=True)
        # Giả lập thời gian trôi qua vượt xa K (ví dụ 10s sau epoch_start)
        self.rel._epoch_start = time.monotonic() - 10.0
        self.rel.srtt = 0.1

        old_cwnd = self.rel.cwnd
        self.rel.update_cwnd(is_loss=False, acked_packets=5)
        # cwnd phải tăng vượt qua W_max
        self.assertGreater(self.rel.cwnd, 20.0)

    def test_fast_convergence(self) -> None:
        """Fast Convergence (RFC 8312 Mục 4.6): Nếu mất gói khi cwnd < W_last_max, giảm W_max sâu hơn (0.65)."""
        # Đợt mất gói 1: cwnd = 30.0
        self.rel.cwnd = 30.0
        self.rel.update_cwnd(is_loss=True)
        self.assertEqual(self.rel._w_last_max, 30.0)

        # Giả sử mạng hồi phục lên cwnd = 25.0 (< 30.0) thì gặp đợt mất gói 2
        self.rel.cwnd = 25.0
        self.rel.update_cwnd(is_loss=True)
        # W_max mới = 25.0 * (2.0 - 0.7) / 2.0 = 25.0 * 0.65 = 16.25
        self.assertAlmostEqual(self.rel._w_max, 16.25, delta=0.01)

    def test_duplicate_ack_does_not_grow_cwnd(self) -> None:
        """Duplicate ACK (acked_packets <= 0) không làm tăng cwnd."""
        self.rel.cwnd = 10.0
        self.rel.update_cwnd(is_loss=False, acked_packets=0)
        self.assertEqual(self.rel.cwnd, 10.0)

    def test_cwnd_clamped_to_max_cwnd(self) -> None:
        """Cửa sổ cwnd không được vượt quá MAX_CWND."""
        self.rel.cwnd = float(MAX_CWND - 1)
        self.rel.ssthresh = float(MAX_CWND)
        self.rel.update_cwnd(is_loss=False, acked_packets=100)
        self.assertEqual(self.rel.cwnd, float(MAX_CWND))


if __name__ == "__main__":
    unittest.main()
