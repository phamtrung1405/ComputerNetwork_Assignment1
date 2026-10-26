"""
server/server.py
~~~~~~~~~~~~~~~~
Điểm khởi chạy của máy chủ trung tâm.

Khởi tạo raw UDP socket non-blocking, sử dụng ``selectors``
cho event loop. Quản lý tất cả sessions và chuyển tiếp datagram.
"""

from __future__ import annotations

import json
import logging
import selectors
import socket
import threading
import time
from typing import Optional

from common import packet
from common.constants import (
    DEFAULT_SERVER_HOST,
    DEFAULT_SERVER_PORT,
    RECV_BUFFER_SIZE,
    TICK_INTERVAL,
)
from transport.connection import Connection
from server.session import Session

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [SERVER] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


class ChatServer:
    """Máy chủ UDP trung tâm quản lý nhiều client.

    Sử dụng ``selectors`` để multiplexing I/O trên một socket duy nhất.
    Mỗi client được quản lý bởi một Session riêng.
    """

    def __init__(self) -> None:
        """Khởi tạo server với các cấu trúc dữ liệu quản lý session."""
        self._sock: Optional[socket.socket] = None
        self._sel: Optional[selectors.DefaultSelector] = None
        self._running: bool = False

        # ── Session management ──
        # conn_id → Session
        self._sessions: dict[int, Session] = {}
        self._sessions_lock: threading.Lock = threading.Lock()

        # addr → conn_id (mapping để nhận diện client từ địa chỉ)
        self._addr_to_conn: dict[tuple[str, int], int] = {}

        # username → conn_id (mapping để unicast)
        self._user_to_conn: dict[str, int] = {}

    # ─────────────────────── Khởi chạy ───────────────────────

    def start_server(
        self,
        host: str = DEFAULT_SERVER_HOST,
        port: int = DEFAULT_SERVER_PORT,
    ) -> None:
        """Khởi tạo raw UDP socket non-blocking và bắt đầu event loop.

        Args:
            host: Địa chỉ IP lắng nghe.
            port: Cổng UDP.
        """
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((host, port))
        self._sock.setblocking(False)

        self._sel = selectors.DefaultSelector()
        self._sel.register(self._sock, selectors.EVENT_READ)

        self._running = True
        logger.info("Server đang lắng nghe tại %s:%d (UDP)", host, port)

        # Chạy tick loop trong thread riêng
        tick_thread = threading.Thread(target=self._tick_loop, daemon=True)
        tick_thread.start()

        # Event loop chính
        try:
            while self._running:
                events = self._sel.select(timeout=TICK_INTERVAL)
                for key, mask in events:
                    if key.fileobj is self._sock:
                        self._read_datagrams()
        except KeyboardInterrupt:
            logger.info("Server đang tắt...")
        finally:
            self._cleanup()

    def _read_datagrams(self) -> None:
        """Đọc tất cả datagram có sẵn trong buffer."""
        while True:
            try:
                data, addr = self._sock.recvfrom(RECV_BUFFER_SIZE)
                self.handle_datagram(data, addr)
            except BlockingIOError:
                break
            except OSError as exc:
                logger.warning("Lỗi đọc socket: %s", exc)
                break

    # ─────────────────────── Xử lý datagram ───────────────────────

    def handle_datagram(self, data: bytes, addr: tuple[str, int]) -> None:
        """Phân tích Connection ID từ datagram và chuyển tiếp vào Session.

        Args:
            data: Dữ liệu thô nhận được.
            addr: Địa chỉ ``(host, port)`` nguồn.
        """
        try:
            header, payload = packet.unpack(data)
        except ValueError:
            logger.debug("Datagram lỗi từ %s – bỏ qua", addr)
            return

        conn_id: int = header.conn_id

        with self._sessions_lock:
            if conn_id not in self._sessions:
                # Tạo session mới cho client mới
                self._create_session(conn_id, addr)
                self._addr_to_conn[addr] = conn_id

            session: Session = self._sessions[conn_id]

        # Cập nhật địa chỉ nếu client thay đổi port (NAT rebinding)
        session.connection.remote_addr = addr
        session.connection.receive_packet(data)

        # Cập nhật user mapping nếu đã xác thực
        if session.authenticated and session.username:
            was_in = session.username in self._user_to_conn
            self._user_to_conn[session.username] = conn_id
            if not was_in:
                self._broadcast_user_list()

    def _create_session(self, conn_id: int, addr: tuple[str, int]) -> None:
        """Tạo một Session mới cho client.

        Args:
            conn_id: Connection ID.
            addr: Địa chỉ client.
        """
        if self._sock is None:
            return

        connection = Connection(
            sock=self._sock,
            remote_addr=addr,
            conn_id=conn_id,
            is_server=True,
            on_app_message=lambda sid, data: self._on_app_message(conn_id, sid, data),
        )

        session = Session(
            conn_id=conn_id,
            client_addr=addr,
            connection=connection,
            broadcast_fn=self._broadcast,
            unicast_fn=self._unicast,
        )

        self._sessions[conn_id] = session
        logger.info("Session mới: conn_id=%d từ %s", conn_id, addr)

    # ─────────────────────── App message callback ────────────────

    def _on_app_message(
        self, conn_id: int, stream_id: int, data: bytes
    ) -> None:
        """Callback khi nhận được message hoàn chỉnh từ transport layer.

        Args:
            conn_id: Connection ID.
            stream_id: Stream ID.
            data: Message data bytes.
        """
        with self._sessions_lock:
            session = self._sessions.get(conn_id)
        if session is not None:
            session.process_payload(data)

    # ─────────────────────── Broadcast & Unicast ─────────────────

    def _broadcast(
        self,
        sender: str,
        msg: dict,
        exclude: Optional[str] = None,
    ) -> None:
        """Gửi message đến tất cả session đang active.

        Args:
            sender: Username người gửi.
            msg: Dict message.
            exclude: Username cần loại trừ (thường là chính người gửi).
        """
        data: bytes = json.dumps(msg, ensure_ascii=False).encode("utf-8")
        with self._sessions_lock:
            for session in self._sessions.values():
                if not session.authenticated:
                    continue
                if exclude and session.username == exclude:
                    continue
                stream = session.connection.get_or_create_stream(0)
                stream.write(data)

    def _unicast(
        self, sender: str, target_username: str, msg: dict
    ) -> bool:
        """Gửi message trực tiếp đến một user cụ thể.

        Args:
            sender: Username người gửi.
            target_username: Username người nhận.
            msg: Dict message.

        Returns:
            ``True`` nếu người nhận đang online.
        """
        conn_id = self._user_to_conn.get(target_username)
        if conn_id is None:
            return False

        with self._sessions_lock:
            session = self._sessions.get(conn_id)

        if session is None or not session.authenticated:
            return False

        data: bytes = json.dumps(msg, ensure_ascii=False).encode("utf-8")
        stream = session.connection.get_or_create_stream(0)
        stream.write(data)
        return True

    # ─────────────────────── Tick loop ───────────────────────

    def _tick_loop(self) -> None:
        """Chạy tick cho tất cả connection trong background thread."""
        while self._running:
            time.sleep(TICK_INTERVAL)
            with self._sessions_lock:
                closed_ids: list[int] = []
                for conn_id, session in self._sessions.items():
                    session.connection.tick()
                    if session.connection.closed:
                        closed_ids.append(conn_id)

                need_userlist_update = False
                for cid in closed_ids:
                    s = self._sessions.pop(cid, None)
                    if s and s.username:
                        self._user_to_conn.pop(s.username, None)
                        logger.info("Session đóng: %s (conn_id=%d)", s.username, cid)
                        need_userlist_update = True

                if need_userlist_update:
                    self._broadcast_user_list()

    def _broadcast_user_list(self) -> None:
        """Gửi danh sách các người dùng trực tuyến đến tất cả client."""
        with self._sessions_lock:
            users = sorted(list(self._user_to_conn.keys()))
        msg = {
            "type": "USER_LIST",
            "users": users,
        }
        self._broadcast("server", msg, exclude=None)

    # ─────────────────────── Cleanup ───────────────────────

    def _cleanup(self) -> None:
        """Dọn dẹp tài nguyên khi server tắt."""
        self._running = False
        if self._sel:
            self._sel.close()
        if self._sock:
            self._sock.close()
        logger.info("Server đã tắt.")


def main() -> None:
    """Hàm entry point để chạy server."""
    server = ChatServer()
    server.start_server()


if __name__ == "__main__":
    main()
