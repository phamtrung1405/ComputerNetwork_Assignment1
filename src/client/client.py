"""
client/client.py
~~~~~~~~~~~~~~~~
Điểm khởi chạy máy khách. Quản lý kết nối UDP đến server,
gửi/nhận tin nhắn, truyền file, và cung cấp API cho HTTP UI.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import threading
import time
import queue
from typing import Optional

from common.constants import (
    DEFAULT_SERVER_HOST,
    DEFAULT_SERVER_PORT,
    RECV_BUFFER_SIZE,
    TICK_INTERVAL,
)
from transport.connection import Connection

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [CLIENT] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


class ChatClient:
    """Máy khách UDP kết nối đến server chat.

    Cung cấp API để gửi tin nhắn, truyền file, và nhận dữ liệu
    từ server. Giao tiếp với HTTP UI qua hàng đợi (queue).
    """

    def __init__(
        self,
        server_ip: str = DEFAULT_SERVER_HOST,
        server_port: int = DEFAULT_SERVER_PORT,
    ) -> None:
        """Khởi tạo raw UDP socket và tạo Connection.

        Args:
            server_ip: Địa chỉ IP của server.
            server_port: Cổng UDP của server.
        """
        self._server_addr: tuple[str, int] = (server_ip, server_port)
        self._sock: socket.socket = socket.socket(
            socket.AF_INET, socket.SOCK_DGRAM
        )
        self._sock.setblocking(False)
        self._sock.bind(("0.0.0.0", 0))  # Port ngẫu nhiên

        self._connection: Connection = Connection(
            sock=self._sock,
            remote_addr=self._server_addr,
            is_server=False,
            on_app_message=self._on_app_message,
        )

        self._running: bool = False
        self._username: Optional[str] = None
        self._authenticated: bool = False

        # Hàng đợi sự kiện cho UI
        self.event_queue: queue.Queue[dict] = queue.Queue()

        # Theo dõi file transfer
        self._file_transfers: dict[int, dict] = {}

    # ─────────────────────── Kết nối ───────────────────────

    def connect(self, username: str, password: str) -> bool:
        """Kích hoạt handshake, gửi AUTH_REQ và duy trì event loop.

        Args:
            username: Tên đăng nhập.
            password: Mật khẩu (sẽ được hash SHA-256).

        Returns:
            ``True`` khi đã bắt đầu kết nối (kết quả auth đến qua event).
        """
        self._username = username
        self._running = True

        # Bắt đầu handshake
        self._connection.handle_handshake()

        # Chạy network poll trong thread riêng
        poll_thread = threading.Thread(target=self.poll_network, daemon=True)
        poll_thread.start()

        # Chạy tick loop
        tick_thread = threading.Thread(target=self._tick_loop, daemon=True)
        tick_thread.start()

        # Đợi handshake hoàn tất (tối đa 5 giây)
        if not self._connection.handshake_complete.wait(timeout=5.0):
            logger.warning("Handshake timeout – gửi auth không mã hóa")

        # Gửi AUTH
        password_hash: str = hashlib.sha256(password.encode("utf-8")).hexdigest()
        auth_msg: dict = {
            "type": "AUTH",
            "username": username,
            "password_hash": password_hash,
        }
        self._send_json(auth_msg)
        return True

    # ─────────────────────── Chat ───────────────────────

    def send_chat(self, target: str, text: str) -> None:
        """Gửi tin nhắn văn bản qua Stream 0.

        Args:
            target: ``"all"`` cho broadcast hoặc username cho unicast.
            text: Nội dung tin nhắn.
        """
        msg: dict = {
            "type": "MSG",
            "target": target,
            "text": text,
        }
        self._send_json(msg)

    # ─────────────────────── File Transfer ───────────────────────

    def start_file_transfer(
        self, file_path: str, target: str = "all"
    ) -> Optional[int]:
        """Đọc file, tính SHA-256, yêu cầu Stream ID mới và truyền.

        Args:
            file_path: Đường dẫn file trên đĩa.
            target: Người nhận (``"all"`` hoặc username).

        Returns:
            Stream ID được cấp, hoặc ``None`` nếu file không tồn tại.
        """
        if not os.path.isfile(file_path):
            logger.error("File không tồn tại: %s", file_path)
            return None

        file_size: int = os.path.getsize(file_path)
        filename: str = os.path.basename(file_path)

        # Tính SHA-256
        sha256_hash = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                sha256_hash.update(chunk)
        file_hash: str = sha256_hash.hexdigest()

        # Cấp stream mới
        stream = self._connection.allocate_stream()
        stream_id: int = stream.stream_id

        # Gửi FILE metadata qua stream 0
        file_msg: dict = {
            "type": "FILE",
            "filename": filename,
            "size": file_size,
            "sha256": file_hash,
            "stream_id": stream_id,
            "target": target,
        }
        self._send_json(file_msg)

        # Truyền file data trong thread riêng
        transfer_thread = threading.Thread(
            target=self._transfer_file_data,
            args=(file_path, stream, target),
            daemon=True,
        )
        transfer_thread.start()

        self._file_transfers[stream_id] = {
            "filename": filename,
            "size": file_size,
            "sent": 0,
            "status": "transferring",
        }

        return stream_id

    def _transfer_file_data(
        self, file_path: str, stream, target: str
    ) -> None:
        """Đọc và gửi dữ liệu file qua Stream chỉ định.

        Args:
            file_path: Đường dẫn file.
            stream: Đối tượng Stream.
            target: Người nhận.
        """
        try:
            with open(file_path, "rb") as f:
                while True:
                    chunk: bytes = f.read(4096)
                    if not chunk:
                        break
                    stream.write(chunk)

                    # Cập nhật tiến trình
                    if stream.stream_id in self._file_transfers:
                        self._file_transfers[stream.stream_id]["sent"] += len(chunk)

                    # Rate limiting đơn giản: kiểm tra can_send với độ trễ thấp
                    while not self._connection.reliability.can_send():
                        time.sleep(0.001)

            if stream.stream_id in self._file_transfers:
                self._file_transfers[stream.stream_id]["status"] = "completed"

            self.event_queue.put({
                "type": "FILE_COMPLETE",
                "stream_id": stream.stream_id,
                "filename": os.path.basename(file_path),
            })
            logger.info("File transfer hoàn tất: %s", file_path)
        except OSError as exc:
            logger.error("Lỗi truyền file: %s", exc)
            if stream.stream_id in self._file_transfers:
                self._file_transfers[stream.stream_id]["status"] = "error"

    # ─────────────────────── Network I/O ───────────────────────

    def poll_network(self) -> None:
        """Vòng lặp liên tục đọc raw socket UDP, bóc tách và xử lý.

        Chạy trong thread riêng. Đọc datagram, chuyển cho Connection
        xử lý, và cập nhật trạng thái nội bộ.
        """
        logger.info("Network polling bắt đầu")
        while self._running:
            try:
                data, addr = self._sock.recvfrom(RECV_BUFFER_SIZE)
                self._connection.receive_packet(data)
            except BlockingIOError:
                time.sleep(0.005)
            except OSError as exc:
                if self._running:
                    logger.debug("Lỗi đọc socket: %s", exc)
                time.sleep(0.01)

    def _tick_loop(self) -> None:
        """Chạy tick cho connection trong background thread."""
        while self._running:
            time.sleep(TICK_INTERVAL)
            self._connection.tick()
            if self._connection.closed:
                self._running = False
                self.event_queue.put({"type": "DISCONNECTED"})
                break

    # ─────────────────────── App message callback ────────────────

    def _on_app_message(self, stream_id: int, data: bytes) -> None:
        """Callback khi nhận message hoàn chỉnh từ server.

        Args:
            stream_id: Stream ID.
            data: Message data.
        """
        try:
            msg: dict = json.loads(data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.debug("Message không phải JSON trên stream %d", stream_id)
            self.event_queue.put({
                "type": "RAW_DATA",
                "stream_id": stream_id,
                "data": data.hex(),
            })
            return

        # Xử lý AUTH_RESP
        if msg.get("type") == "AUTH_RESP":
            self._authenticated = msg.get("success", False)
            if self._authenticated:
                logger.info("Đăng nhập thành công: %s", self._username)
            else:
                logger.warning("Đăng nhập thất bại")

        self.event_queue.put(msg)

    # ─────────────────────── Tiện ích ───────────────────────

    def _send_json(self, msg: dict) -> None:
        """Gửi một JSON message qua Stream 0.

        Args:
            msg: Dict cần gửi.
        """
        stream = self._connection.get_or_create_stream(0)
        data: bytes = json.dumps(msg, ensure_ascii=False).encode("utf-8")
        stream.write(data)

    def get_file_progress(self) -> dict[int, dict]:
        """Lấy tiến trình truyền file hiện tại.

        Returns:
            Dict ``stream_id → {filename, size, sent, status}``.
        """
        return dict(self._file_transfers)

    def disconnect(self) -> None:
        """Ngắt kết nối và dọn dẹp."""
        self._running = False
        try:
            self._sock.close()
        except OSError:
            logger.debug("Socket đã đóng")
        logger.info("Client đã ngắt kết nối")

    @property
    def is_authenticated(self) -> bool:
        """Trạng thái xác thực hiện tại."""
        return self._authenticated

    @property
    def username(self) -> Optional[str]:
        """Username hiện tại."""
        return self._username
