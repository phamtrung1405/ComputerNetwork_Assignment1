"""
transport/reliability.py
~~~~~~~~~~~~~~~~~~~~~~~~
Cơ chế Sliding Window, RTT estimation (RFC 6298) và thuật toán
tắc nghẽn CUBIC gộp chung trong một lớp duy nhất.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
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


@dataclass
class _InFlightPacket:
    """Thông tin một gói tin đang chờ ACK."""

    packet_num: int
    raw_data: bytes
    sent_time: float
    retransmit_count: int = 0


class ReliabilityManager:
    """Quản lý Sliding Window, RTT và CUBIC congestion control."""

    def __init__(self) -> None:
        """Khởi tạo cửa sổ tắc nghẽn, bộ đệm chờ ACK và các biến RTT."""
        # ── Congestion control ──
        self.cwnd: float = float(INITIAL_CWND)
        self.ssthresh: float = float(SSTHRESH_INITIAL)
        self._w_max: float = float(INITIAL_CWND)
        self._epoch_start: Optional[float] = None
        self._k: float = 0.0

        # ── RTT estimation (RFC 6298) ──
        self.srtt: Optional[float] = None
        self.rttvar: Optional[float] = None
        self.rto: float = INITIAL_RTO

        # ── Bộ đệm gói tin chưa xác nhận ──
        self._unacked: dict[int, _InFlightPacket] = {}

    # ────────────────────────── Ghi nhận gửi ──────────────────────────

    def mark_sent(self, packet_num: int, packet_data: bytes) -> None:
        """Lưu gói tin vừa gửi vào bộ đệm cùng timestamp.

        Args:
            packet_num: Số thứ tự gói tin.
            packet_data: Dữ liệu thô đã đóng gói sẵn.
        """
        self._unacked[packet_num] = _InFlightPacket(
            packet_num=packet_num,
            raw_data=packet_data,
            sent_time=time.monotonic(),
        )

    # ────────────────────────── Xử lý SACK ──────────────────────────

    def process_sack(self, ack_ranges: list[tuple[int, int]]) -> None:
        """Xử lý dải Selective ACK, xóa các gói đã nhận.

        Args:
            ack_ranges: Danh sách các khoảng ``(start, end)`` đã được bên
                        nhận xác nhận (inclusive cả hai đầu).
        """
        for start, end in ack_ranges:
            for seq in range(start, end + 1):
                pkt = self._unacked.pop(seq, None)
                if pkt is not None and pkt.retransmit_count == 0:
                    sample_rtt: float = time.monotonic() - pkt.sent_time
                    self.update_rtt(sample_rtt)
        self.update_cwnd(is_loss=False)

    # ────────────────────────── Retransmit ──────────────────────────

    def get_retransmits(self) -> list[bytes]:
        """Trả về danh sách raw data của các gói đã vượt RTO.

        Returns:
            Danh sách bytes cần truyền lại.
        """
        now: float = time.monotonic()
        retransmits: list[bytes] = []
        for pkt in list(self._unacked.values()):
            elapsed: float = now - pkt.sent_time
            if elapsed >= self.rto:
                retransmits.append(pkt.raw_data)
                pkt.sent_time = now
                pkt.retransmit_count += 1
                self.update_cwnd(is_loss=True)
        return retransmits

    # ────────────────────────── RTT (RFC 6298) ──────────────────────

    def update_rtt(self, sample_rtt: float) -> None:
        """Cập nhật SRTT và RTTVAR theo RFC 6298.

        Args:
            sample_rtt: RTT đo được (giây).
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

    # ────────────────────────── CUBIC cwnd ──────────────────────────

    def update_cwnd(self, is_loss: bool) -> None:
        """Cập nhật cửa sổ tắc nghẽn theo CUBIC.

        Args:
            is_loss: ``True`` nếu phát hiện mất gói, ``False`` khi ổn định.
        """
        if is_loss:
            self._w_max = self.cwnd
            self.ssthresh = max(self.cwnd * CUBIC_BETA, float(MIN_CWND))
            self.cwnd = max(self.ssthresh, float(MIN_CWND))
            self._epoch_start = None
            return

        # Slow start
        if self.cwnd < self.ssthresh:
            self.cwnd = min(self.cwnd + 1.0, float(MAX_CWND))
            return

        # Congestion avoidance – CUBIC
        now: float = time.monotonic()
        if self._epoch_start is None:
            self._epoch_start = now
            if self._w_max <= self.cwnd:
                self._k = 0.0
            else:
                self._k = math.pow(
                    (self._w_max - self.cwnd) / CUBIC_C, 1.0 / 3.0
                )

        t: float = now - self._epoch_start
        w_cubic: float = CUBIC_C * math.pow(t - self._k, 3.0) + self._w_max
        w_cubic = max(w_cubic, float(MIN_CWND))
        w_cubic = min(w_cubic, float(MAX_CWND))
        self.cwnd = w_cubic

    # ────────────────────────── Truy vấn ──────────────────────────

    def can_send(self) -> bool:
        """Kiểm tra xem có thể gửi thêm gói mới không (cwnd chưa đầy).

        Returns:
            ``True`` nếu số gói đang bay < cwnd.
        """
        return len(self._unacked) < int(self.cwnd)

    @property
    def in_flight(self) -> int:
        """Số gói tin đang chờ ACK."""
        return len(self._unacked)


# Hằng dùng nội bộ cho tính RTO
TICK_INTERVAL_CLOCK: float = 0.05
