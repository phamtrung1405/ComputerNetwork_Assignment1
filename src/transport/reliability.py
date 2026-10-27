r"""
transport/reliability.py
~~~~~~~~~~~~~~~~~~~~~~~~
Cơ chế Sliding Window, Selective ACK (SACK), RTT estimation (RFC 6298),
Exponential Backoff và thuật toán kiểm soát tắc nghẽn CUBIC (RFC 8312).

Chịu trách nhiệm:
- Lưu vết các gói tin đang bay (in-flight packets) trong bộ đệm `_unacked`.
- Xử lý các dải xác nhận lựa chọn SACK, phát hiện và loại bỏ các gói tin đã nhận thành công.
- Xử lý an toàn các ACK trùng lặp (duplicate ACKs), ACK cũ (stale) và chống tấn công làm treo CPU (DoS range).
- Áp dụng luật Karn (RFC 6298): không lấy mẫu RTT cho các gói tin đã từng được truyền lại.
- Lựa chọn các gói tin quá hạn RTO cần truyền lại (`get_retransmits`).
- Áp dụng Exponential Backoff nhân đôi RTO khi xảy ra timeout liên tiếp.
- Ngăn ngừa hiện tượng sụp đổ cửa sổ CUBIC khi mất nhiều gói tin trong 1 tick (ADR-009).
- Ngắt kết nối các gói tin vượt quá số lần truyền lại tối đa (Circuit Breaker: `max_retransmits`).
- Thuật toán CUBIC tiêu chuẩn (RFC 8312): Slow Start, Fast Convergence, Concave Region,
  Plateau Saturation tại K, Convex Probing và TCP-Friendly Region.

Kiến trúc & Giới hạn thích ứng so với RFC tiêu chuẩn:
- RFC 6298: Chu kỳ đồng hồ tối thiểu được thích ứng theo chu kỳ tick loop của STREAMCHAT
  (50ms qua `TICK_INTERVAL_CLOCK = 0.05s`). Do giao thức UDP 22-byte không có trường TCP Timestamps
  (RFC 7323), luật Karn được áp dụng bắt buộc để tránh nhập nhằng RTT.
- RFC 8312: Cửa sổ tắc nghẽn (`cwnd`) được tính theo đơn vị số gói tin (datagrams $\le 1362$B)
  thay vì byte-counting. Chưa hỗ trợ Delayed ACKs (receiver gửi 1 ACK/gói) và SACK loss detection
  3-dupACK (hiện phụ thuộc vào RTO timer).
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
    """Thông tin một gói tin đang bay (chờ ACK từ đối tác).

    Attributes:
        packet_num: Số thứ tự gói tin (Sequence Number).
        raw_data: Dữ liệu thô nhị phân của gói tin (bao gồm 22B header + payload).
        sent_time: Thời điểm gói tin được gửi đi gần nhất (monotonic timestamp).
        retransmit_count: Số lần gói tin đã được gửi lại (0 = gửi lần đầu).
    """

    packet_num: int
    raw_data: bytes
    sent_time: float
    retransmit_count: int = 0


class ReliabilityManager:
    """Quản lý Sliding Window, SACK, RTT và CUBIC Congestion Control.

    Đảm bảo tính nhất quán của trạng thái truyền tải, an toàn đa luồng (thread-safe),
    tuân thủ nghiêm ngặt các RFC tiêu chuẩn mạng (RFC 6298, RFC 8312) và kiến trúc STREAMCHAT.
    """

    def __init__(self, max_retransmits: int = DEFAULT_MAX_RETRANSMITS) -> None:
        """Khởi tạo cửa sổ tắc nghẽn, bộ đệm chờ ACK và các thông số RTT/CUBIC.

        Args:
            max_retransmits: Số lần truyền lại tối đa cho mỗi gói tin (mặc định 8).
        """
        # ── Khóa đồng bộ đa luồng ──
        self._lock: threading.RLock = threading.RLock()

        # ── Congestion Control (CUBIC - RFC 8312) ──
        self.cwnd: float = float(INITIAL_CWND)
        self.ssthresh: float = float(SSTHRESH_INITIAL)
        self._w_max: float = float(INITIAL_CWND)
        self._w_last_max: float = float(INITIAL_CWND)  # Phục vụ Fast Convergence (RFC 8312 Section 4.6)
        self._epoch_start: Optional[float] = None
        self._k: float = 0.0
        self._last_loss_time: float = 0.0  # Thời điểm xảy ra sự kiện mất gói gần nhất (ADR-009)

        # ── RTT estimation (RFC 6298) ──
        self.srtt: Optional[float] = None
        self.rttvar: Optional[float] = None
        self.rto: float = INITIAL_RTO
        self._consecutive_timeouts: int = 0  # Đếm số lần timeout liên tiếp phục vụ Exponential Backoff

        # ── Bộ đệm gói tin chưa xác nhận ──
        self._unacked: dict[int, _InFlightPacket] = {}

        # ── Thống kê và kiểm soát lỗi ──
        self.max_retransmits: int = max_retransmits
        self.failed_packets: set[int] = set()  # Các sequence number vượt quá ngưỡng retransmit tối đa
        self.duplicate_acks: int = 0  # Tổng số ACK trùng lặp / stale nhận được
        self.consecutive_dupacks: int = 0  # Số ACK trùng lặp liên tiếp chưa có dữ liệu mới
        self.max_acked_seq: int = -1  # Số thứ tự cao nhất từng được xác nhận

    # ────────────────────────── Ghi nhận gửi ──────────────────────────

    def mark_sent(self, packet_num: int, packet_data: bytes) -> None:
        """Lưu gói tin vừa gửi vào bộ đệm cùng timestamp hiện tại.

        Args:
            packet_num: Số thứ tự gói tin (phải là số nguyên không âm).
            packet_data: Dữ liệu nhị phân thô của gói tin đã đóng gói sẵn.

        Raises:
            ValueError: Nếu `packet_num` không phải số nguyên không âm.
            TypeError: Nếu `packet_num` sai kiểu hoặc `packet_data` không phải bytes.
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

    # ────────────────────────── Xử lý SACK ──────────────────────────

    def process_sack(self, ack_ranges: list[tuple[int, int]]) -> None:
        """Xử lý dải Selective ACK (SACK), loại bỏ các gói tin đã được nhận thành công.

        Thuật toán xử lý:
        1. Kiểm tra tính hợp lệ của tham số đầu vào (cấu trúc danh sách, kiểu dữ liệu, biên số âm, biên ngược).
        2. Duyệt qua từng dải SACK `(start, end)`. Áp dụng cơ chế chống DoS: nếu khoảng `end - start`
           quá lớn, chỉ đối soát trên tập khóa hiện có của `_unacked` thay vì lặp qua toàn bộ dải.
        3. Với mỗi gói tin được xác nhận lần đầu (nằm trong `_unacked`):
           - Xóa gói tin khỏi `_unacked`.
           - Cập nhật số thứ tự cao nhất đã ACK (`max_acked_seq`).
           - Áp dụng Luật Karn (RFC 6298): CHỈ đo mẫu RTT nếu gói tin đó CHƯA TỪNG bị truyền lại (`retransmit_count == 0`).
        4. Cập nhật cửa sổ tắc nghẽn:
           - Nếu có ít nhất 1 gói tin mới được xác nhận (`newly_acked_count > 0`): Đặt lại bộ đếm timeout
             và duplicate ACKs liên tiếp, đồng thời gọi `update_cwnd(is_loss=False, acked_packets=newly_acked_count)`.
           - Nếu không có gói tin nào mới (ACK trùng lặp hoặc ACK cũ): Tăng biến đếm `duplicate_acks`
             và `consecutive_dupacks`, tuyệt đối KHÔNG tăng `cwnd` để tránh phình ảo cửa sổ.

        Args:
            ack_ranges: Danh sách các khoảng `(start, end)` đã được bên nhận xác nhận (inclusive cả 2 đầu).

        Raises:
            TypeError: Nếu `ack_ranges` không phải list/tuple, hoặc các phần tử/biên sai kiểu dữ liệu.
            ValueError: Nếu phần tử không đủ 2 phần tử, biên âm hoặc `start > end`.
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

    # ────────────────────────── Retransmit ──────────────────────────

    def get_retransmits(self) -> list[bytes]:
        """Trả về danh sách raw bytes của các gói tin chưa được ACK đã vượt quá thời gian RTO.

        Cơ chế:
        1. Duyệt qua các gói tin chưa xác nhận trong `_unacked` theo thứ tự sequence number tăng dần.
        2. Lọc các gói có `elapsed = now - pkt.sent_time >= self.rto`.
        3. Kiểm tra Circuit Breaker: nếu gói đã gửi lại đạt `max_retransmits`, đánh dấu vào
           `failed_packets` và ngừng gửi lại để tránh treo tài nguyên đường truyền.
        4. Cập nhật `sent_time = now` và tăng `retransmit_count += 1`.
        5. Khi phát hiện gói quá hạn:
           - Nhân đôi RTO (Exponential Backoff, RFC 6298), tối đa `MAX_RTO`.
           - Phòng chống sụp đổ cửa sổ CUBIC (ADR-009): Chỉ gọi `update_cwnd(is_loss=True)` tối đa 1 lần
             trong mỗi chu kỳ RTT (loss epoch), ngăn chặn `cwnd` bị giảm lũy thừa $0.7^N$ khi mất nhiều gói.

        Returns:
            list[bytes]: Danh sách dữ liệu gói tin cần truyền lại ngay lập tức.
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

    # ────────────────────────── RTT (RFC 6298) ──────────────────────

    def update_rtt(self, sample_rtt: float) -> None:
        r"""Cập nhật Smoothed RTT (SRTT) và RTT Variation (RTTVAR) theo RFC 6298.

        Đặc tả thuật toán RFC 6298:
        1. Đo mẫu đầu tiên (First RTT measurement $R$ - Mục 2.2):
           - $SRTT \\leftarrow R$
           - $RTTVAR \\leftarrow R / 2$
           - $RTO \\leftarrow SRTT + \\max(G, 4 \\times RTTVAR)$
        2. Đo các mẫu tiếp theo (Subsequent RTT measurements $R'$ - Mục 2.3):
           - Lưu ý thứ tự: $RTTVAR$ được tính dựa trên $SRTT$ cũ trước khi cập nhật $SRTT$:
             $RTTVAR \\leftarrow (1 - \\beta) \\times RTTVAR + \\beta \\times |SRTT - R'|$  (\\beta = 0.25)
             $SRTT \\leftarrow (1 - \\alpha) \\times SRTT + \\alpha \\times R'$            (\\alpha = 0.125)
           - $RTO \\leftarrow SRTT + \\max(G, 4 \\times RTTVAR)$
        3. Ép dải RTO (Mục 2.4 & 2.5):
           - $RTO = \\max(MIN\\_RTO, \\min(RTO, MAX\\_RTO))$
        4. Xóa trạng thái Exponential Backoff:
           - Khi nhận được mẫu RTT hợp lệ cho dữ liệu mới, bộ đếm timeout liên tiếp
             `_consecutive_timeouts` được đặt lại về 0 và RTO được tính trực tiếp từ SRTT/RTTVAR.

        Args:
            sample_rtt: Thời gian khứ hồi đo được tính bằng giây (phải là số thực >= 0.0).

        Raises:
            TypeError: Nếu `sample_rtt` không phải số int hoặc float.
            ValueError: Nếu `sample_rtt` âm.
        """
        if not isinstance(sample_rtt, (int, float)):
            raise TypeError(f"sample_rtt phải là số (int hoặc float), nhận được: {type(sample_rtt)}")
        if sample_rtt < 0.0:
            raise ValueError(f"sample_rtt không được âm, nhận được: {sample_rtt}")

        alpha: float = 0.125  # 1/8 theo RFC 6298
        beta: float = 0.25    # 1/4 theo RFC 6298

        with self._lock:
            sample = float(sample_rtt)
            if self.srtt is None:
                # Mục 2.2: Mẫu RTT đầu tiên
                self.srtt = sample
                self.rttvar = sample / 2.0
            else:
                # Mục 2.3: Các mẫu RTT kế tiếp (tính RTTVAR trước bằng SRTT hiện tại)
                self.rttvar = (1.0 - beta) * self.rttvar + beta * abs(self.srtt - sample)
                self.srtt = (1.0 - alpha) * self.srtt + alpha * sample

            # Mục 2.4 & 2.5: Tính toán RTO có tính đến clock granularity G và ép giới hạn
            calculated_rto = self.srtt + max(TICK_INTERVAL_CLOCK, 4.0 * self.rttvar)
            self.rto = max(MIN_RTO, min(calculated_rto, MAX_RTO))
            self._consecutive_timeouts = 0

    # ────────────────────────── CUBIC cwnd (RFC 8312) ──────────────────

    def update_cwnd(self, is_loss: bool, acked_packets: int = 1) -> None:
        r"""Cập nhật kích thước cửa sổ tắc nghẽn theo thuật toán CUBIC (RFC 8312).

        Các trạng thái và giai đoạn thuật toán:
        1. Phản ứng khi Mất gói (Loss Event - RFC 8312 Mục 4.5 & 4.6):
           - Áp dụng Fast Convergence (Mục 4.6): Nếu mất gói ở mức $cwnd < W_{last\\_max}$, mạng
             đang giảm dung lượng, ta giảm $W_{max}$ sâu hơn ($0.85 \\times cwnd$) để nhường đường.
             Ngược lại, lưu $W_{max} = cwnd$.
           - Giảm cửa sổ multiplicatively: $ssthresh = \\max(cwnd \\times \\beta_{cubic}, MIN\\_CWND)$
             với $\\beta_{cubic} = 0.7$.
           - Đặt $cwnd = ssthresh$.
           - Đặt lại epoch bắt đầu: `_epoch_start = None`, `_k = 0.0`.

        2. Chế độ Khởi động Chậm (Slow Start):
           - Khi $cwnd < ssthresh$, với mỗi gói tin được xác nhận, cửa sổ tăng 1 gói:
             $cwnd = \\min(cwnd + acked\\_packets, ssthresh, MAX\\_CWND)$.
           - Khi vượt qua hoặc chạm $ssthresh$, chuyển trạng thái sang Congestion Avoidance.

        3. Chế độ Tránh Tắc nghẽn CUBIC (Congestion Avoidance - RFC 8312 Mục 4.1, 4.2 & 4.3):
           - Tính điểm gốc $K$:
             $K = \\sqrt[3]{\\frac{W_{max} - W_{min}}{C}} = \\sqrt[3]{\\frac{W_{max} \\times (1 - \\beta)}{C}}$
             ($K$ là khoảng thời gian hàm bậc 3 cần để tăng từ $W_{max} \\times \\beta$ về $W_{max}$).
           - Với $t = now - epoch\\_start$, tính toán cửa sổ mục tiêu tại $(t + RTT)$:
             $W_{cubic}(t + RTT) = C \\times (t + RTT - K)^3 + W_{max}$.
           - Bảo vệ vùng thân thiện với TCP (TCP-Friendly Region - Mục 4.2):
             $W_{est}(t + RTT) = W_{max} \\times \\beta + \\left(\\frac{3 \\times (1 - \\beta)}{1 + \\beta}\\right) \\times \\frac{t + RTT}{RTT}$.
             $W_{target} = \\max(W_{cubic}, W_{est})$.
           - Tăng trưởng từng ACK (Mục 4.3):
             Mỗi gói tin xác nhận làm tăng cửa sổ một lượng tỷ lệ:
             $\\Delta cwnd = acked\\_packets \\times \\frac{W_{target} - cwnd}{cwnd}$ (nếu $W_{target} > cwnd$),
             hoặc tối thiểu $acked\\_packets / cwnd$ để duy trì thăm dò băng thông liên tục.

        Args:
            is_loss: `True` nếu phát hiện mất gói (timeout / loss event), `False` khi nhận ACK bình thường.
            acked_packets: Số lượng gói tin mới được xác nhận trong lượt ACK này (mặc định 1).
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

    # ────────────────────────── Truy vấn ──────────────────────────

    def can_send(self) -> bool:
        """Kiểm tra xem cửa sổ trượt còn chỗ trống để gửi thêm gói mới hay không.

        Returns:
            bool: `True` nếu số gói đang bay < phần nguyên của `cwnd`.
        """
        with self._lock:
            return len(self._unacked) < int(self.cwnd)

    @property
    def in_flight(self) -> int:
        """Số lượng gói tin đang bay chưa nhận được ACK."""
        with self._lock:
            return len(self._unacked)
