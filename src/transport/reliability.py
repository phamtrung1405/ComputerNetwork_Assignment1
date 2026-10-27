r"""
transport/reliability.py
~~~~~~~~~~~~~~~~~~~~~~~~
Sliding Window mechanism, RTT estimation (RFC 6298), and CUBIC congestion
control algorithm combined into a single manager class.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Optional

from common.constants import (
    INITIAL_RTO,
    MIN_RTO,
    MAX_RTO,
    INITIAL_CWND,
    MIN_CWND,
    MAX_CWND,
    SSTHRESH_INITIAL,
    CUBIC_C,
    CUBIC_BETA,
)

# Số lần truyền lại tối đa mặc định trước khi đánh dấu gói tin hỏng vĩnh viễn
DEFAULT_MAX_RETRANSMITS: int = 8

# Chu kỳ đồng hồ tối thiểu cho tính toán RTO (RFC 6298 quy định granular clock G)
TICK_INTERVAL_CLOCK: float = 0.05


@dataclass
class _InFlightPacket:
    """Metadata of an in-flight packet awaiting ACK."""

    packet_num: int
    raw_data: bytes
    sent_time: float
    retransmit_count: int = 0


class ReliabilityManager:
    """Manages Sliding Window, RTT calculation, and CUBIC congestion control."""

    def __init__(self) -> None:
        """Initialize congestion window, unacked buffer, and RTT variables."""
        # -- Congestion control --
        self.cwnd: float = float(INITIAL_CWND)
        self.ssthresh: float = float(SSTHRESH_INITIAL)
        self._w_max: float = float(INITIAL_CWND)
        self._w_last_max: float = float(INITIAL_CWND)  # Phục vụ Fast Convergence (RFC 8312 Section 4.6)
        self._epoch_start: Optional[float] = None
        self._k: float = 0.0
        self._last_loss_time: float = 0.0  # Thời điểm xảy ra sự kiện mất gói gần nhất (ADR-009)

        # -- RTT estimation (RFC 6298) --
        self.srtt: Optional[float] = None
        self.rttvar: Optional[float] = None
        self.rto: float = INITIAL_RTO
        self._consecutive_timeouts: int = 0  # Đếm số lần timeout liên tiếp phục vụ Exponential Backoff

        # -- Unacknowledged packet buffer --
        self._unacked: dict[int, _InFlightPacket] = {}

    # -------------------------- Record Sent --------------------------

    def mark_sent(self, packet_num: int, packet_data: bytes) -> None:
        """Store newly sent packet in the unacked buffer with its timestamp.

        Args:
            packet_num: Packet sequence number.
            packet_data: Raw pre-packed packet bytes.
        """
        if not isinstance(packet_num, int):
            raise TypeError(f"packet_num phải là số nguyên int, nhận được: {type(packet_num)}")
        if packet_num < 0:
            raise ValueError(f"packet_num phải là số nguyên không âm, nhận được: {packet_num}")
        if not isinstance(packet_data, (bytes, bytearray)):
            raise TypeError(f"packet_data phải là bytes hoặc bytearray, nhận được: {type(packet_data)}")

        with self._lock:
            self._unacked[packet_num] = _InFlightPacket(
                packet_num=packet_num,
                raw_data=bytes(packet_data),
                sent_time=time.monotonic(),
                retransmit_count=0,
            )

    # -------------------------- SACK Processing --------------------------

    def process_sack(self, ack_ranges: list[tuple[int, int]]) -> None:
        """Process Selective ACK ranges and remove acknowledged packets.

        Args:
            ack_ranges: List of ``(start, end)`` ranges acknowledged by peer
                        (inclusive on both ends).
        """
        if not isinstance(ack_ranges, (list, tuple)):
            raise TypeError(f"ack_ranges phải là list hoặc tuple, nhận được: {type(ack_ranges)}")

        # Validate toàn bộ các dải trước khi biến đổi trạng thái
        validated_ranges: list[tuple[int, int]] = []
        for rng in ack_ranges:
            if not isinstance(rng, (list, tuple)) or len(rng) != 2:
                raise ValueError(f"Mỗi phần tử trong ack_ranges phải là (start, end) gồm 2 phần tử, nhận được: {rng}")
            start, end = rng[0], rng[1]
            if not isinstance(start, int) or not isinstance(end, int):
                raise TypeError(f"Các biên của range phải là số nguyên int, nhận được: ({type(start)}, {type(end)})")
            if start < 0 or end < 0:
                raise ValueError(f"Các biên của range không được âm, nhận được: ({start}, {end})")
            if start > end:
                raise ValueError(f"Dải SACK không hợp lệ: start ({start}) > end ({end})")
            validated_ranges.append((start, end))

        with self._lock:
            now: float = time.monotonic()
            newly_acked_count: int = 0
            rtt_samples: list[float] = []

            for start, end in validated_ranges:
                range_len = end - start + 1
                # Chống DoS: nếu range quá dài so với số gói đang bay, duyệt trên keys của _unacked
                if range_len <= len(self._unacked):
                    seqs_to_check = [seq for seq in range(start, end + 1) if seq in self._unacked]
                else:
                    seqs_to_check = [seq for seq in list(self._unacked.keys()) if start <= seq <= end]

                for seq in seqs_to_check:
                    pkt = self._unacked.pop(seq, None)
                    if pkt is not None:
                        newly_acked_count += 1
                        if seq > self.max_acked_seq:
                            self.max_acked_seq = seq

                        # Luật Karn (RFC 6298): Chỉ lấy mẫu RTT cho gói không bị retransmit
                        if pkt.retransmit_count == 0:
                            sample_rtt = now - pkt.sent_time
                            rtt_samples.append(sample_rtt)

            # Cập nhật RTT từ các mẫu hợp lệ
            for sample in rtt_samples:
                self.update_rtt(sample)

            if newly_acked_count > 0:
                # Có bước tiến dữ liệu mới (Forward Progress)
                self._consecutive_timeouts = 0
                self.consecutive_dupacks = 0
                self.update_cwnd(is_loss=False, acked_packets=newly_acked_count)
            else:
                # ACK trùng lặp (Duplicate ACK) hoặc ACK cho gói tin đã xóa trước đó
                self.duplicate_acks += 1
                self.consecutive_dupacks += 1

    # -------------------------- Retransmit --------------------------

    def get_retransmits(self) -> list[bytes]:
        """Return raw data of all packets that have exceeded RTO timeout.

        Returns:
            List of bytes that need retransmission.
        """
        with self._lock:
            now: float = time.monotonic()
            retransmits: list[bytes] = []
            expired_packets: list[_InFlightPacket] = []

            # Sắp xếp theo thứ tự số thứ tự tăng dần
            for seq in sorted(self._unacked.keys()):
                pkt = self._unacked[seq]
                elapsed: float = now - pkt.sent_time
                if elapsed >= self.rto:
                    if pkt.retransmit_count >= self.max_retransmits:
                        self.failed_packets.add(pkt.packet_num)
                        continue

                    retransmits.append(pkt.raw_data)
                    pkt.sent_time = now
                    pkt.retransmit_count += 1
                    expired_packets.append(pkt)

            if expired_packets:
                self._consecutive_timeouts += 1

                # ── Exponential Backoff (RFC 6298) ──
                self.rto = min(self.rto * 2.0, MAX_RTO)

                # ── Phòng chống CUBIC Multi-Loss Window Collapse (ADR-009) ──
                # Chỉ giảm cwnd tối đa 1 lần trong mỗi khoảng RTT / loss epoch
                loss_interval: float = max(self.srtt if self.srtt is not None else 0.0, MIN_RTO)
                if now - self._last_loss_time >= loss_interval:
                    self.update_cwnd(is_loss=True)
                    self._last_loss_time = now

            return retransmits

    # -------------------------- RTT (RFC 6298) ----------------------

    def update_rtt(self, sample_rtt: float) -> None:
        """Update SRTT and RTTVAR following RFC 6298.

        Args:
            sample_rtt: Measured RTT sample (seconds).
        """
        alpha: float = 0.125
        beta: float = 0.25

        if self.srtt is None:
            self.srtt = sample_rtt
            self.rttvar = sample_rtt / 2.0
        else:
            self.rttvar = (1.0 - beta) * self.rttvar + beta * abs(
                self.srtt - sample_rtt
            )
            self.srtt = (1.0 - alpha) * self.srtt + alpha * sample_rtt

        self.rto = self.srtt + max(TICK_INTERVAL_CLOCK, 4.0 * self.rttvar)
        self.rto = max(MIN_RTO, min(self.rto, MAX_RTO))

    # -------------------------- CUBIC cwnd --------------------------

    def update_cwnd(self, is_loss: bool) -> None:
        """Update congestion window according to CUBIC algorithm.

        Args:
            is_loss: ``True`` if packet loss detected, ``False`` on successful ACK.
        """
        if not is_loss and acked_packets <= 0:
            # Không có dữ liệu mới được xác nhận (Duplicate/Stale ACK) -> không tăng cwnd
            return

        with self._lock:
            if is_loss:
                # ── Phản ứng mất gói & Fast Convergence (RFC 8312 Mục 4.5 & 4.6) ──
                if self.cwnd < self._w_last_max:
                    # Dung lượng cổ chai đang thu hẹp: giảm trần sâu hơn (0.85 * cwnd)
                    self._w_max = max(self.cwnd * (2.0 - CUBIC_BETA) / 2.0, float(MIN_CWND))
                else:
                    self._w_max = self.cwnd
                self._w_last_max = self.cwnd

                # Giảm cửa sổ multiplicatively
                self.ssthresh = max(self.cwnd * CUBIC_BETA, float(MIN_CWND))
                self.cwnd = max(self.ssthresh, float(MIN_CWND))

                # Đặt lại chu kỳ epoch CUBIC
                self._epoch_start = None
                self._k = 0.0
                return

            # ── 1. Slow Start (RFC 5681 / RFC 8312) ──
            if self.cwnd < self.ssthresh:
                # Tăng tuyến tính theo số gói ACK trong Slow Start
                self.cwnd = min(self.cwnd + float(acked_packets), float(self.ssthresh), float(MAX_CWND))
                if self.cwnd >= self.ssthresh:
                    # Chuyển tiếp sang Congestion Avoidance: reset epoch để khởi động hàm bậc 3
                    self._epoch_start = None
                return

            # ── 2. Congestion Avoidance (CUBIC - RFC 8312 Mục 4.1 - 4.3) ──
            now: float = time.monotonic()
            if self._epoch_start is None:
                self._epoch_start = now
                if self._w_max <= self.cwnd:
                    self._k = 0.0
                else:
                    # K = ((W_max - cwnd) / C)^(1/3)
                    self._k = math.pow((self._w_max - self.cwnd) / CUBIC_C, 1.0 / 3.0)

            t: float = now - self._epoch_start
            rtt: float = self.srtt if (self.srtt is not None and self.srtt > 0.0) else INITIAL_RTO
            t_target: float = t + rtt

            # Tính toán đường cong CUBIC: W_cubic(t + RTT) = C * (t + RTT - K)^3 + W_max
            w_cubic: float = CUBIC_C * math.pow(t_target - self._k, 3.0) + self._w_max

            # Tính toán TCP-Friendly Region (Mục 4.2): W_est
            # Scale hệ số tương đương Reno: 3 * (1 - beta) / (1 + beta) = 3 * 0.3 / 1.7 ~ 0.5294
            tcp_scale: float = (3.0 * (1.0 - CUBIC_BETA)) / (1.0 + CUBIC_BETA)
            w_tcp: float = self._w_max * CUBIC_BETA + tcp_scale * (t_target / rtt)

            # Mục tiêu cửa sổ là max giữa CUBIC và TCP Reno
            w_target: float = max(w_cubic, w_tcp)

            # Cập nhật per-ACK theo Mục 4.3
            if w_target > self.cwnd:
                increment = float(acked_packets) * ((w_target - self.cwnd) / self.cwnd)
            else:
                # Duy trì thăm dò tối thiểu khi w_target <= cwnd
                increment = float(acked_packets) * (1.0 / self.cwnd)

            self.cwnd = max(float(MIN_CWND), min(self.cwnd + increment, float(MAX_CWND)))

    # -------------------------- Query --------------------------

    def can_send(self) -> bool:
        """Check if new packets can be transmitted (cwnd not full).

        Returns:
            ``True`` if in-flight packets < cwnd.
        """
        with self._lock:
            return len(self._unacked) < int(self.cwnd)

    @property
    def in_flight(self) -> int:
        """Count of packets currently awaiting ACK."""
        return len(self._unacked)


# Internal constant for RTO calculation clock granularity
TICK_INTERVAL_CLOCK: float = 0.05
