"""
transport/connection.py
~~~~~~~~~~~~~~~~~~~~~~~
Quản lý vòng đời của một kết nối UDP logic (mô phỏng kết nối
có trạng thái trên nền UDP không trạng thái).

Bao gồm: handshake, mã hóa, gửi/nhận gói tin, heartbeat và
retransmission.
"""

from __future__ import annotations

import json
import logging
import random
import socket
import threading
import time
from typing import Optional, Callable

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from common import packet
from common.constants import (
    PKT_HELLO,
    PKT_HELLO_ACK,
    PKT_DATA,
    PKT_ACK,
    PKT_HEARTBEAT,
    PKT_HEARTBEAT_ACK,
    FLAG_NONE,
    FLAG_EOM,
    FLAG_ENCRYPTED,
    HEARTBEAT_INTERVAL,
    IDLE_TIMEOUT,
    RECV_BUFFER_SIZE,
)
from common.crypto import (
    generate_keypair,
    derive_shared_secret,
    encrypt,
    decrypt,
)
from transport.reliability import ReliabilityManager
from transport.stream import Stream

logger = logging.getLogger(__name__)


class Connection:
    """Kết nối UDP logic giữa hai endpoint.

    Quản lý handshake (trao đổi khóa), mã hóa/giải mã, gửi/nhận
    gói tin, heartbeat keepalive và retransmission.
    """

    def __init__(
        self,
        sock: socket.socket,
        remote_addr: tuple[str, int],
        conn_id: int = 0,
        is_server: bool = False,
        on_app_message: Optional[Callable[[int, bytes], None]] = None,
    ) -> None:
        """Khởi tạo Connection.

        Args:
            sock: UDP socket đã bind sẵn.
            remote_addr: Tuple ``(host, port)`` của bên kia.
            conn_id: Định danh kết nối (uint32). 0 = chưa gán.
            is_server: ``True`` nếu đây là phía server.
            on_app_message: Callback ``(stream_id, data)`` khi nhận
                            message hoàn chỉnh từ một Stream.
        """
        self.sock: socket.socket = sock
        self.remote_addr: tuple[str, int] = remote_addr
        self.conn_id: int = conn_id if conn_id else random.randint(1, 0xFFFFFFFF)
        self.is_server: bool = is_server
        self.on_app_message: Optional[Callable[[int, bytes], None]] = on_app_message

        # ── Crypto ──
        self._private_key: Optional[X25519PrivateKey] = None
        self._public_bytes: Optional[bytes] = None
        self._session_key: Optional[bytes] = None
        self.handshake_complete: threading.Event = threading.Event()

        # ── Sequence numbers ──
        self._next_seq: int = 0
        self._seq_lock: threading.Lock = threading.Lock()

        # ── Streams ──
        self._streams: dict[int, Stream] = {}
        self._streams_lock: threading.Lock = threading.Lock()
        self._next_stream_id: int = 1 if not is_server else 2  # odd/even

        # ── Reliability ──
        self.reliability: ReliabilityManager = ReliabilityManager()

        # ── Timing ──
        self._last_recv_time: float = time.monotonic()
        self._last_heartbeat: float = 0.0

        # ── Trạng thái ──
        self.closed: bool = False

    # ─────────────────────── Sequence number ───────────────────────

    def _alloc_seq(self) -> int:
        """Cấp phát sequence number mới (thread-safe).

        Returns:
            Số thứ tự mới.
        """
        with self._seq_lock:
            seq = self._next_seq
            self._next_seq += 1
            return seq

    # ─────────────────────── Stream management ─────────────────────

    def get_or_create_stream(self, stream_id: int) -> Stream:
        """Lấy hoặc tạo mới Stream theo ID.

        Args:
            stream_id: Mã stream.

        Returns:
            Đối tượng Stream tương ứng.
        """
        with self._streams_lock:
            if stream_id not in self._streams:
                stream = Stream(
                    stream_id=stream_id,
                    send_callback=self._stream_send_callback,
                )
                self._streams[stream_id] = stream
            return self._streams[stream_id]

    def allocate_stream(self) -> Stream:
        """Tạo Stream mới với ID tự động.

        Returns:
            Đối tượng Stream mới.
        """
        with self._streams_lock:
            sid = self._next_stream_id
            self._next_stream_id += 2  # giữ chẵn/lẻ
            stream = Stream(
                stream_id=sid,
                send_callback=self._stream_send_callback,
            )
            self._streams[sid] = stream
            return stream

    def _stream_send_callback(
        self, stream_id: int, offset: int, chunk: bytes, flags: int
    ) -> None:
        """Callback được Stream gọi khi cần gửi chunk.

        Args:
            stream_id: Mã stream.
            offset: Byte offset.
            chunk: Dữ liệu chunk.
            flags: Cờ điều khiển (VD: FLAG_EOM).
        """
        self.send_packet(PKT_DATA, stream_id, chunk, flags=flags, offset=offset)

    # ─────────────────────── Gửi gói tin ───────────────────────

    def send_packet(
        self,
        packet_type: int,
        stream_id: int,
        payload: bytes,
        flags: int = FLAG_NONE,
        offset: int = 0,
    ) -> None:
        """Tạo packet, mã hóa (nếu có key), ghi vào reliability buffer và gửi.

        Args:
            packet_type: Loại gói tin (PKT_DATA, PKT_ACK, …).
            stream_id: Mã stream.
            payload: Dữ liệu ứng dụng.
            flags: Cờ điều khiển.
            offset: Byte offset trong stream.
        """
        seq: int = self._alloc_seq()

        # Mã hóa nếu đã có session key
        actual_payload: bytes = payload
        if self._session_key is not None and packet_type == PKT_DATA:
            actual_payload = encrypt(payload, self._session_key, seq)
            flags |= FLAG_ENCRYPTED

        raw: bytes = packet.pack(
            msg_type=packet_type,
            conn_id=self.conn_id,
            seq_num=seq,
            flags=flags,
            stream_id=stream_id,
            offset=offset,
            payload=actual_payload,
        )

        # Ghi nhận vào reliability nếu cần ACK
        if packet_type == PKT_DATA:
            self.reliability.mark_sent(seq, raw)

        self._raw_send(raw)

    def _raw_send(self, data: bytes) -> None:
        """Gửi dữ liệu thô qua UDP socket.

        Args:
            data: Bytes cần gửi.
        """
        try:
            self.sock.sendto(data, self.remote_addr)
        except OSError as exc:
            logger.warning("Gửi thất bại đến %s: %s", self.remote_addr, exc)

    # ─────────────────────── Nhận gói tin ───────────────────────

    def receive_packet(self, raw_bytes: bytes) -> None:
        """Giải mã và phân loại gói tin đến.

        Args:
            raw_bytes: Dữ liệu thô nhận từ socket.
        """
        self._last_recv_time = time.monotonic()

        try:
            header, payload = packet.unpack(raw_bytes)
        except ValueError as exc:
            logger.debug("Bỏ qua gói lỗi: %s", exc)
            return

        msg_type: int = header.msg_type

        if msg_type == PKT_ACK:
            self._handle_ack(payload)
        elif msg_type == PKT_DATA:
            self._handle_data(header, payload)
        elif msg_type == PKT_HELLO:
            self._handle_hello(header, payload)
        elif msg_type == PKT_HELLO_ACK:
            self._handle_hello_ack(header, payload)
        elif msg_type == PKT_HEARTBEAT:
            self._send_heartbeat_ack()
        elif msg_type == PKT_HEARTBEAT_ACK:
            logger.debug("Heartbeat ACK nhận từ %s", self.remote_addr)
        else:
            logger.debug("Loại gói không xác định: 0x%02X", msg_type)

    def _handle_ack(self, payload: bytes) -> None:
        """Xử lý gói ACK, chuyển tiếp cho ReliabilityManager.

        Args:
            payload: JSON chứa dải SACK.
        """
        try:
            ack_info = json.loads(payload.decode("utf-8"))
            ranges: list[tuple[int, int]] = [
                (r[0], r[1]) for r in ack_info.get("ranges", [])
            ]
            self.reliability.process_sack(ranges)
        except (json.JSONDecodeError, UnicodeDecodeError, KeyError) as exc:
            logger.debug("ACK payload lỗi: %s", exc)

    def _handle_data(self, header: packet.Header, payload: bytes) -> None:
        """Xử lý gói DATA: giải mã, đẩy vào Stream, gửi ACK.

        Args:
            header: Header đã unpack.
            payload: Payload (có thể đã mã hóa).
        """
        # Gửi ACK ngay lập tức
        self._send_ack(header.seq_num)

        # Giải mã nếu cần
        actual_payload: bytes = payload
        if (header.flags & FLAG_ENCRYPTED) and self._session_key is not None:
            try:
                actual_payload = decrypt(
                    payload, self._session_key, header.seq_num
                )
            except Exception as exc:
                logger.warning("Giải mã thất bại seq=%d: %s", header.seq_num, exc)
                return

        is_eom: bool = bool(header.flags & FLAG_EOM)
        stream = self.get_or_create_stream(header.stream_id)
        complete_msg = stream.read_incoming(
            offset=header.offset,
            data=actual_payload,
            is_eom=is_eom,
        )

        if complete_msg is not None and self.on_app_message is not None:
            self.on_app_message(header.stream_id, complete_msg)

    def _send_ack(self, seq_num: int) -> None:
        """Gửi ACK cho một sequence number.

        Args:
            seq_num: Số thứ tự gói cần xác nhận.
        """
        ack_payload: bytes = json.dumps(
            {"ranges": [[seq_num, seq_num]]}
        ).encode("utf-8")
        raw: bytes = packet.pack(
            msg_type=PKT_ACK,
            conn_id=self.conn_id,
            seq_num=0,
            flags=FLAG_NONE,
            stream_id=0,
            offset=0,
            payload=ack_payload,
        )
        self._raw_send(raw)

    # ─────────────────────── Handshake ───────────────────────

    def handle_handshake(self) -> None:
        """Chạy quy trình trao đổi khóa HELLO → HELLO_ACK (phía Client).

        Client gửi HELLO chứa public key, đợi HELLO_ACK từ Server.
        """
        self._private_key, self._public_bytes = generate_keypair()
        hello_payload: bytes = json.dumps({
            "public_key": self._public_bytes.hex(),
            "conn_id": self.conn_id,
        }).encode("utf-8")

        raw: bytes = packet.pack(
            msg_type=PKT_HELLO,
            conn_id=self.conn_id,
            seq_num=0,
            flags=FLAG_NONE,
            stream_id=0,
            offset=0,
            payload=hello_payload,
        )
        self._raw_send(raw)
        logger.info("Gửi HELLO đến %s (conn_id=%d)", self.remote_addr, self.conn_id)

    def _handle_hello(self, header: packet.Header, payload: bytes) -> None:
        """Xử lý HELLO từ Client (phía Server).

        Args:
            header: Header của gói HELLO.
            payload: JSON chứa public key của client.
        """
        try:
            info = json.loads(payload.decode("utf-8"))
            peer_pub_hex: str = info["public_key"]
            peer_pub_bytes: bytes = bytes.fromhex(peer_pub_hex)
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.warning("HELLO payload lỗi: %s", exc)
            return

        # Tạo keypair phía server
        self._private_key, self._public_bytes = generate_keypair()
        self._session_key = derive_shared_secret(self._private_key, peer_pub_bytes)

        # Gửi HELLO_ACK với public key của server
        ack_payload: bytes = json.dumps({
            "public_key": self._public_bytes.hex(),
            "conn_id": self.conn_id,
        }).encode("utf-8")

        raw: bytes = packet.pack(
            msg_type=PKT_HELLO_ACK,
            conn_id=self.conn_id,
            seq_num=0,
            flags=FLAG_NONE,
            stream_id=0,
            offset=0,
            payload=ack_payload,
        )
        self._raw_send(raw)
        self.handshake_complete.set()
        logger.info(
            "Handshake hoàn tất (server side) conn_id=%d", self.conn_id
        )

    def _handle_hello_ack(self, header: packet.Header, payload: bytes) -> None:
        """Xử lý HELLO_ACK từ Server (phía Client).

        Args:
            header: Header gói HELLO_ACK.
            payload: JSON chứa public key server.
        """
        try:
            info = json.loads(payload.decode("utf-8"))
            peer_pub_hex: str = info["public_key"]
            peer_pub_bytes: bytes = bytes.fromhex(peer_pub_hex)
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.warning("HELLO_ACK payload lỗi: %s", exc)
            return

        if self._private_key is None:
            logger.warning("Chưa gửi HELLO mà nhận HELLO_ACK")
            return

        self._session_key = derive_shared_secret(self._private_key, peer_pub_bytes)
        self.handshake_complete.set()
        logger.info(
            "Handshake hoàn tất (client side) conn_id=%d", self.conn_id
        )

    # ─────────────────────── Heartbeat ───────────────────────

    def _send_heartbeat(self) -> None:
        """Gửi gói Heartbeat keepalive."""
        raw: bytes = packet.pack(
            msg_type=PKT_HEARTBEAT,
            conn_id=self.conn_id,
            seq_num=0,
            flags=FLAG_NONE,
            stream_id=0,
            offset=0,
            payload=b"",
        )
        self._raw_send(raw)
        self._last_heartbeat = time.monotonic()

    def _send_heartbeat_ack(self) -> None:
        """Gửi phản hồi Heartbeat."""
        raw: bytes = packet.pack(
            msg_type=PKT_HEARTBEAT_ACK,
            conn_id=self.conn_id,
            seq_num=0,
            flags=FLAG_NONE,
            stream_id=0,
            offset=0,
            payload=b"",
        )
        self._raw_send(raw)

    # ─────────────────────── Tick loop ───────────────────────

    def tick(self) -> None:
        """Kiểm tra timeout, gửi heartbeat, kích hoạt retransmission.

        Hàm này được gọi định kỳ bởi event loop.
        """
        now: float = time.monotonic()

        # Idle timeout
        if now - self._last_recv_time > IDLE_TIMEOUT:
            logger.warning(
                "Connection %d idle quá %ss – đánh dấu đóng",
                self.conn_id,
                IDLE_TIMEOUT,
            )
            self.closed = True
            return

        # Heartbeat
        if now - self._last_heartbeat > HEARTBEAT_INTERVAL:
            self._send_heartbeat()

        # Retransmission
        retransmits: list[bytes] = self.reliability.get_retransmits()
        for raw_pkt in retransmits:
            self._raw_send(raw_pkt)
            logger.debug("Truyền lại gói trên conn %d", self.conn_id)
