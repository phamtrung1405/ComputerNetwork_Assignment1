"""
tests/test_connection_loopback.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Kiểm thử tích hợp Connection (M1) <-> crypto / reliability / stream (M2) qua socket giả
(không dùng UDP thật). Hợp đồng: docs/project/M1_M2_INTEGRATION_CONTRACT.md (K1-K11).

Trạng thái hợp đồng:
- ``Connection.INTEGRATION_CONTRACT_VERSION >= 1`` bật các kiểm tra K1-K11.
- Trên phiên bản cũ chưa bind, một số kiểm tra được đánh dấu ``expectedFailure`` và bài truyền file lớn bị ``skip``.

Biến môi trường:
- ``STREAMCHAT_SRC_DIR``: trỏ tới thư mục ``src`` khác (ví dụ bản đã vá) để kiểm thử.
- ``STREAMCHAT_BIGFILE_MB``: dung lượng bài kiểm tra file lớn (mặc định 10).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import sys
import threading
import time
import unittest
from pathlib import Path
from typing import Callable, Optional

SRC_DIR = Path(os.environ.get("STREAMCHAT_SRC_DIR") or Path(__file__).resolve().parent.parent / "src")
if str(SRC_DIR) in sys.path:
    sys.path.remove(str(SRC_DIR))
sys.path.insert(0, str(SRC_DIR))

from common import packet
from common.constants import FLAG_EOM, HEADER_SIZE, MAX_UDP_PAYLOAD, PKT_ACK, PKT_DATA, PKT_HELLO
from common.crypto import reset_nonce_tracker
from transport.connection import Connection

BOUND: bool = getattr(Connection, "INTEGRATION_CONTRACT_VERSION", 0) >= 1
bind_required = (lambda f: f) if BOUND else unittest.expectedFailure
requires_bound = unittest.skipUnless(BOUND, "Chờ M1 áp patch connection.py (INTEGRATION_CONTRACT_VERSION >= 1)")

CONN_ID = 1234
BIGFILE_MB = int(os.environ.get("STREAMCHAT_BIGFILE_MB", "10"))


# ─────────────────────────── Mạng giả ───────────────────────────

class _FakeSock:
    def __init__(self, net: "LossyNet", name: str) -> None:
        self._net, self._name = net, name

    def sendto(self, data: bytes, addr: tuple) -> None:
        self._net._enqueue(self._name, bytes(data))


class LossyNet:
    """Hai hàng đợi datagram (c2s, s2c) có thể mất/trùng/đảo thứ tự, chạy thủ công hoặc bằng thread."""

    def __init__(self, loss: float = 0.0, dup: float = 0.0, reorder: bool = False, seed: int = 42) -> None:
        self.loss, self.dup, self.reorder = loss, dup, reorder
        self.impaired: bool = False  # bật sau handshake
        self.rng = random.Random(seed)
        self._q: dict[str, list[bytes]] = {"c2s": [], "s2c": []}
        self._lock = threading.Lock()
        self.cli: Optional[Connection] = None
        self.srv: Optional[Connection] = None
        self.dropped = 0
        self.duplicated = 0
        self.errors: list[BaseException] = []
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def socket(self, name: str) -> _FakeSock:
        return _FakeSock(self, name)

    def _enqueue(self, name: str, data: bytes) -> None:
        with self._lock:
            self._q[name].append(data)

    def pending(self, name: str) -> list[bytes]:
        with self._lock:
            return list(self._q[name])

    def clear(self, name: str) -> None:
        with self._lock:
            self._q[name].clear()

    def take(self, name: str) -> list[bytes]:
        with self._lock:
            batch, self._q[name] = self._q[name], []
            return batch

    def _impair(self, batch: list[bytes]) -> list[bytes]:
        if not self.impaired:
            return batch
        out: list[bytes] = []
        for d in batch:
            if self.rng.random() < self.loss:
                self.dropped += 1
                continue
            out.append(d)
            if self.rng.random() < self.dup:
                out.append(d)
                self.duplicated += 1
        if self.reorder:
            self.rng.shuffle(out)
        return out

    def step(self) -> int:
        moved = 0
        for name, dst in (("c2s", self.srv), ("s2c", self.cli)):
            batch = self.take(name)
            moved += len(batch)
            for d in self._impair(batch):
                dst.receive_packet(d)
        return moved

    def drain(self) -> None:
        """Chế độ thủ công: chuyển datagram cho đến khi hai hàng đợi rỗng."""
        while self.step():
            pass

    # ── chế độ thread ──
    def start(self) -> None:
        def pump() -> None:
            try:
                while not self._stop.is_set():
                    if not self.step():
                        time.sleep(0.0005)
            except BaseException as exc:  # noqa: BLE001
                self.errors.append(exc)

        def ticker() -> None:
            try:
                while not self._stop.is_set():
                    self.cli.tick()
                    self.srv.tick()
                    time.sleep(0.02)
            except BaseException as exc:  # noqa: BLE001
                self.errors.append(exc)

        for fn in (pump, ticker):
            t = threading.Thread(target=fn, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=5)


def make_pair(
    *,
    loss: float = 0.0,
    dup: float = 0.0,
    reorder: bool = False,
    seed: int = 42,
    on_server_message: Optional[Callable[[int, bytes], None]] = None,
    handshake: bool = True,
) -> tuple[LossyNet, Connection, Connection]:
    reset_nonce_tracker()
    net = LossyNet(loss, dup, reorder, seed)
    cli = Connection(net.socket("c2s"), ("srv", 1), conn_id=CONN_ID, is_server=False)
    srv = Connection(
        net.socket("s2c"), ("cli", 1), conn_id=CONN_ID, is_server=True, on_app_message=on_server_message
    )
    net.cli, net.srv = cli, srv
    if handshake:
        cli.handle_handshake()
        net.drain()
        assert cli.handshake_complete.is_set() and srv.handshake_complete.is_set(), "handshake thất bại"
    net.impaired = True
    return net, cli, srv


# ─────────────────────────── Hợp đồng K1-K11 ───────────────────────────

class TestEncryptionBinding(unittest.TestCase):
    """K1-K5: khóa hai chiều, nonce độc lập, header là AAD, loại DATA thô, không ACK gói hỏng."""

    def test_wire_size_never_exceeds_mtu(self) -> None:
        """Gói DATA mã hóa đầy đủ (chunk 1362 B) dài đúng 1400 B."""
        net, cli, _ = make_pair()
        cli.get_or_create_stream(1).write(os.urandom(1362))
        raw = net.pending("c2s")[0]
        self.assertLessEqual(len(raw), MAX_UDP_PAYLOAD)

    # C-1 đã được M1 sửa trên origin/main (khóa hai chiều + is_server), nên không còn bind_required.
    def test_directions_do_not_share_keystream(self) -> None:
        """C-1: cùng plaintext, cùng seq=0 ở hai chiều phải cho ciphertext khác nhau."""
        net, cli, srv = make_pair()
        payload = b"SAME-PLAINTEXT-IN-BOTH-DIRECTIONS"
        cli.get_or_create_stream(1).write(payload)
        srv.get_or_create_stream(2).write(payload)
        h1, ct1 = packet.unpack(net.pending("c2s")[0])
        h2, ct2 = packet.unpack(net.pending("s2c")[0])
        self.assertEqual((h1.seq_num, h2.seq_num), (0, 0))
        self.assertNotEqual(ct1, ct2)

    @bind_required
    def test_tampered_header_stream_id_rejected(self) -> None:
        """C-2: đổi stream_id trên đường truyền phải bị AEAD từ chối, không giao, không ACK."""
        delivered: list = []
        net, cli, srv = make_pair(on_server_message=lambda sid, d: delivered.append((sid, d)))
        cli.get_or_create_stream(1).write(b"hello")
        raw = bytearray(net.pending("c2s")[0])
        net.clear("c2s")
        raw[10:12] = (7).to_bytes(2, "big")  # stream_id nằm ở byte 10..11
        srv.receive_packet(bytes(raw))
        self.assertEqual(delivered, [])
        self.assertEqual(net.pending("s2c"), [])

    @bind_required
    def test_tampered_header_offset_rejected(self) -> None:
        """C-2: đổi offset cũng bị từ chối."""
        delivered: list = []
        net, cli, srv = make_pair(on_server_message=lambda sid, d: delivered.append((sid, d)))
        cli.get_or_create_stream(1).write(b"hello")
        raw = bytearray(net.pending("c2s")[0])
        net.clear("c2s")
        raw[12:16] = (9).to_bytes(4, "big")  # offset ở byte 12..15
        srv.receive_packet(bytes(raw))
        self.assertEqual(delivered, [])
        self.assertEqual(net.pending("s2c"), [])

    @bind_required
    def test_plaintext_data_rejected_after_handshake(self) -> None:
        """C-3: DATA không mang FLAG_ENCRYPTED sau handshake bị loại bỏ."""
        delivered: list = []
        net, _, srv = make_pair(on_server_message=lambda sid, d: delivered.append((sid, d)))
        forged = packet.pack(PKT_DATA, CONN_ID, 999, FLAG_EOM, 0, 0, b'{"type":"MSG","text":"forged"}')
        srv.receive_packet(forged)
        self.assertEqual(delivered, [])
        self.assertEqual(net.pending("s2c"), [])

    @bind_required
    def test_corrupted_data_is_not_acked(self) -> None:
        """H-1: gói bị sửa payload không được ACK; bên gửi vẫn giữ gói trong in-flight."""
        delivered: list = []
        net, cli, srv = make_pair(on_server_message=lambda sid, d: delivered.append((sid, d)))
        cli.get_or_create_stream(1).write(b"hello world")
        raw = bytearray(net.pending("c2s")[0])
        net.clear("c2s")
        raw[HEADER_SIZE + 2] ^= 0xFF
        srv.receive_packet(bytes(raw))
        self.assertEqual(delivered, [])
        self.assertEqual(net.pending("s2c"), [])
        self.assertEqual(cli.reliability.in_flight, 1)


class TestRobustnessBinding(unittest.TestCase):
    """K8-K11: dữ liệu sai của peer, bỏ cuộc truyền lại, HELLO lặp/mất."""

    @bind_required
    def test_malformed_ack_does_not_escape_receive_packet(self) -> None:
        """H-6: ACK sai định dạng bị bỏ qua, không văng ngoại lệ khỏi receive_packet."""
        _, cli, _ = make_pair()
        cases = [
            {"ranges": [[-1, 2]]}, {"ranges": [[5, 1]]}, {"ranges": [[1]]}, {"ranges": [["a", 2]]},
            {"ranges": [[1.5, 2]]}, {"ranges": 5}, None,
        ]
        escaped = []
        for obj in cases:
            raw = packet.pack(PKT_ACK, CONN_ID, 0, 0, 0, 0, json.dumps(obj).encode())
            try:
                cli.receive_packet(raw)
            except Exception as exc:  # noqa: BLE001
                escaped.append((obj, type(exc).__name__))
        self.assertEqual(escaped, [])

    @bind_required
    def test_giving_up_on_a_packet_closes_the_connection(self) -> None:
        """H-5/K10: gói vượt max_retransmits làm Connection.closed = True."""
        net, cli, _ = make_pair()
        cli.reliability.max_retransmits = 1
        cli.get_or_create_stream(1).write(b"x")
        net.clear("c2s")  # mất hoàn toàn
        for _ in range(3):
            for pkt in cli.reliability._unacked.values():
                pkt.sent_time -= 1000.0
            cli.tick()
        self.assertTrue(cli.closed)

    @bind_required
    def test_duplicate_hello_does_not_change_session_key(self) -> None:
        """M-6/K11: HELLO lặp (HELLO_ACK có thể đã mất) không được sinh lại khóa ở server."""
        net, cli, srv = make_pair()
        key_before = srv._session_key
        dup_hello = packet.pack(
            PKT_HELLO, CONN_ID, 0, 0, 0, 0,
            json.dumps({"public_key": cli._public_bytes.hex(), "conn_id": CONN_ID}).encode(),
        )
        srv.receive_packet(dup_hello)
        self.assertEqual(srv._session_key, key_before)

    @bind_required
    def test_lost_hello_is_retransmitted(self) -> None:
        """T-M1-04/K11: HELLO bị mất thì client gửi lại khi tick và handshake vẫn hoàn tất."""
        net, cli, srv = make_pair(handshake=False)
        cli.handle_handshake()
        net.clear("c2s")  # HELLO đầu bị mất
        cli._hello_sent_time -= 10.0
        cli.tick()
        net.drain()
        self.assertTrue(cli.handshake_complete.is_set())
        self.assertTrue(srv.handshake_complete.is_set())


class TestDeliveryAndFlowControlBinding(unittest.TestCase):
    """K6-K7: giao mọi message hoàn chỉnh; cửa sổ tắc nghẽn được tôn trọng."""

    @bind_required
    def test_all_messages_completed_together_are_delivered(self) -> None:
        """C-4: M1, M2 đến trước M0 -> khi M0 đến cả ba phải tới app theo đúng thứ tự, không rò rỉ RAM."""
        got: list = []
        net, cli, srv = make_pair(on_server_message=lambda sid, d: got.append(d))
        st = cli.get_or_create_stream(1)
        msgs = [b"M0-first", b"M1-second", b"M2-third"]
        for m in msgs:
            st.write(m)
        pk = net.pending("c2s")
        net.clear("c2s")
        for d in (pk[1], pk[2], pk[0]):
            srv.receive_packet(d)
        self.assertEqual(got, msgs)
        self.assertEqual(srv.get_or_create_stream(1)._complete_messages, [])

    @bind_required
    def test_in_flight_is_bounded_by_cwnd(self) -> None:
        """H-2: một write lớn không đẩy hơn int(cwnd) gói ra mạng; phần còn lại vẫn được giao đủ."""
        got: list = []
        net, cli, srv = make_pair(on_server_message=lambda sid, d: got.append(d))
        data = os.urandom(200 * 1362)
        cli.get_or_create_stream(1).write(data)
        self.assertLessEqual(cli.reliability.in_flight, int(cli.reliability.cwnd))
        net.drain()
        self.assertEqual(got, [data])
        self.assertEqual(cli.reliability.in_flight, 0)


# ─────────────────────────── Chấp nhận: truyền file + SHA-256 ───────────────────────────

SCENARIOS = [
    ("clean", dict()),
    ("loss5", dict(loss=0.05)),
    ("loss10", dict(loss=0.10)),
    ("reorder_dup", dict(dup=0.03, reorder=True)),
    ("loss5_reorder_dup", dict(loss=0.05, dup=0.03, reorder=True)),
]


def run_transfer(size: int, mode: str, timeout: float = 180.0, **net_kwargs) -> dict:
    """Gửi ``size`` byte ngẫu nhiên từ client sang server và trả kết quả đo được.

    mode="blocks": các khối 4096 B, mỗi khối là một message (đúng cách client.py gửi file).
    mode="single": toàn bộ là một message.
    """
    got: list[bytes] = []
    lock = threading.Lock()

    def on_msg(sid: int, d: bytes) -> None:
        with lock:
            got.append(d)

    net, cli, srv = make_pair(on_server_message=on_msg, **net_kwargs)
    data = os.urandom(size)
    expected_msgs = 1 if mode == "single" else math.ceil(size / 4096)
    stream = cli.allocate_stream()
    net.start()
    t0 = time.monotonic()
    try:
        if mode == "single":
            stream.write(data)
        else:
            for i in range(0, size, 4096):
                stream.write(data[i:i + 4096])
        deadline = t0 + timeout
        while time.monotonic() < deadline:
            with lock:
                done = len(got) >= expected_msgs
            if done and cli.reliability.in_flight == 0:
                break
            if cli.closed:
                break
            time.sleep(0.02)
    finally:
        net.stop()
    with lock:
        rx = b"".join(got)
        count = len(got)
    return {
        "expected_msgs": expected_msgs, "msgs": count, "bytes": len(rx), "size": size,
        "sha_ok": hashlib.sha256(rx).hexdigest() == hashlib.sha256(data).hexdigest(),
        "in_flight": cli.reliability.in_flight, "failed": sorted(cli.reliability.failed_packets),
        "dropped": net.dropped, "duplicated": net.duplicated, "errors": net.errors,
        "closed": cli.closed, "elapsed": time.monotonic() - t0,
        "retained": sum(len(m) for m in srv.get_or_create_stream(stream.stream_id)._complete_messages),
    }


@requires_bound
class TestFileTransferSha256(unittest.TestCase):
    """Truyền file ngẫu nhiên qua Connection<->Connection; SHA-256 phải khớp trong mọi kịch bản mạng."""

    def _check(self, r: dict) -> None:
        self.assertEqual(r["errors"], [])
        self.assertFalse(r["closed"])
        self.assertEqual(r["failed"], [])
        self.assertEqual(r["in_flight"], 0)
        self.assertEqual(r["msgs"], r["expected_msgs"])
        self.assertEqual(r["bytes"], r["size"])
        self.assertTrue(r["sha_ok"], "SHA-256 không khớp")
        self.assertEqual(r["retained"], 0, "Stream còn giữ message đã giao trong RAM")

    def test_matrix_2mb(self) -> None:
        """Ma trận 5 kịch bản mạng x 2 cách gửi (khối 4096 B / một message) với 2 MB."""
        for name, kw in SCENARIOS:
            for mode in ("blocks", "single"):
                with self.subTest(scenario=name, mode=mode):
                    r = run_transfer(2 * 1024 * 1024, mode, **kw)
                    if name != "clean":
                        self.assertGreater(r["dropped"] + r["duplicated"], 0, "kịch bản không gây nhiễu thực tế")
                    self._check(r)

    def test_bigfile_blocks_loss5_reorder_dup(self) -> None:
        """File lớn (mặc định 10 MB) dạng khối 4096 B, mất 5% + đảo thứ tự + trùng 3%."""
        r = run_transfer(BIGFILE_MB * 1024 * 1024, "blocks", loss=0.05, dup=0.03, reorder=True)
        self.assertGreater(r["dropped"], 0)
        self._check(r)

    def test_bigfile_single_loss10(self) -> None:
        """File lớn dạng một message (<= 16 MB bộ đệm), mất 10%."""
        size = min(BIGFILE_MB, 12) * 1024 * 1024
        r = run_transfer(size, "single", loss=0.10)
        self.assertGreater(r["dropped"], 0)
        self._check(r)


if __name__ == "__main__":
    unittest.main()
