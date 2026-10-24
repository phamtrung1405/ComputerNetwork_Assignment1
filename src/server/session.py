"""
server/session.py
~~~~~~~~~~~~~~~~~
Quản lý logic tầng ứng dụng cho từng Client kết nối đến Server.

Mỗi Session tương ứng với một Connection UDP và giữ trạng thái
xác thực, username, và chuyển tiếp tin nhắn.
"""

from __future__ import annotations

import hashlib
import json
import logging
import socket
from typing import Optional, Callable

from transport.connection import Connection
from server.database import db

logger = logging.getLogger(__name__)


class Session:
    """Quản lý phiên làm việc của một client trên server.

    Giữ trạng thái đăng nhập, xử lý payload ứng dụng (AUTH, MSG, FILE)
    và chuyển tiếp tin nhắn giữa các session.
    """

    def __init__(
        self,
        conn_id: int,
        client_addr: tuple[str, int],
        connection: Connection,
        broadcast_fn: Optional[Callable[[str, dict[str, object]], str | None], None] = None,
        unicast_fn: Optional[Callable[[str, str, dict[str, object]], bool]] = None,
    ) -> None:
        """Khởi tạo Session.

        Args:
            conn_id: Định danh kết nối.
            client_addr: Địa chỉ ``(host, port)`` của client.
            connection: Đối tượng Connection tương ứng.
            broadcast_fn: Hàm callback để gửi broadcast
                          ``(sender_username, msg_dict, exclude_username) -> None``.
            unicast_fn: Hàm callback để gửi unicast
                        ``(sender, target, msg_dict) -> bool``.
        """
        self.conn_id: int = conn_id
        self.client_addr: tuple[str, int] = client_addr
        self.connection: Connection = connection
        self.username: Optional[str] = None
        self.authenticated: bool = False
        self._broadcast_fn: Optional[Callable] = broadcast_fn
        self._unicast_fn: Optional[Callable] = unicast_fn

    # ─────────────────────── Xử lý payload ───────────────────────

    def process_payload(self, app_msg: bytes) -> None:
        """Phân tích JSON payload và quyết định xử lý.

        Các loại payload hỗ trợ:
        - ``AUTH``: Yêu cầu đăng nhập.
        - ``MSG``: Tin nhắn chat (broadcast hoặc unicast).
        - ``FILE``: Thông báo truyền file.

        Args:
            app_msg: Dữ liệu ứng dụng (bytes) – JSON encoded.
        """
        try:
            msg: dict = json.loads(app_msg.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.warning("Payload JSON lỗi từ %s: %s", self.client_addr, exc)
            return

        msg_type: str = msg.get("type", "")

        if msg_type == "AUTH":
            self._process_auth(msg)
        elif msg_type == "MSG":
            self._process_msg(msg)
        elif msg_type == "FILE":
            self._process_file(msg)
        elif msg_type == "FILE_DATA":
            self._process_file_data(msg)
        else:
            logger.debug("Loại message không xác định: %s", msg_type)

    # ─────────────────────── Auth ───────────────────────

    def _process_auth(self, msg: dict) -> None:
        """Xử lý yêu cầu xác thực.

        Args:
            msg: Dict chứa ``username`` và ``password_hash``.
        """
        username: str = msg.get("username", "")
        password_hash: str = msg.get("password_hash", "")
        success: bool = self.verify_auth(username, password_hash)

        response: dict = {
            "type": "AUTH_RESP",
            "success": success,
            "username": username,
        }
        if success:
            response["message"] = f"Chào mừng {username}!"
            # Thông báo broadcast
            if self._broadcast_fn is not None:
                join_msg: dict = {
                    "type": "SYSTEM",
                    "message": f"{username} đã tham gia phòng chat.",
                }
                self._broadcast_fn(username, join_msg, username)
        else:
            response["message"] = "Tên đăng nhập hoặc mật khẩu không đúng."

        self._send_app_message(response)

    def verify_auth(self, username: str, password_hash: str) -> bool:
        """So sánh thông tin xác thực với cơ sở dữ liệu SQLite.

        Args:
            username: Tên người dùng.
            password_hash: SHA-256 hash của mật khẩu.

        Returns:
            ``True`` nếu xác thực thành công.
        """
        if db.verify_user(username, password_hash):
            self.username = username
            self.authenticated = True
            logger.info("Xác thực thành công: %s", username)
            return True
        logger.warning("Xác thực thất bại: %s", username)
        return False

    # ─────────────────────── Chat ───────────────────────

    def _process_msg(self, msg: dict) -> None:
        """Xử lý tin nhắn chat.

        Args:
            msg: Dict chứa ``target`` (``"all"`` hoặc username) và ``text``.
        """
        if not self.authenticated:
            self._send_app_message({
                "type": "ERROR",
                "message": "Bạn chưa đăng nhập.",
            })
            return

        target: str = msg.get("target", "all")
        text: str = msg.get("text", "")

        chat_msg: dict = {
            "type": "MSG",
            "from": self.username,
            "text": text,
            "timestamp": __import__("time").time(),
        }

        if target == "all":
            self.route_broadcast(chat_msg)
        else:
            self.route_unicast(target, chat_msg)

    def _process_file(self, msg: dict) -> None:
        """Xử lý thông báo bắt đầu truyền file.

        Args:
            msg: Dict chứa ``filename``, ``size``, ``sha256``, ``target``.
        """
        if not self.authenticated:
            self._send_app_message({
                "type": "ERROR",
                "message": "Bạn chưa đăng nhập.",
            })
            return

        file_info: dict = {
            "type": "FILE_START",
            "from": self.username,
            "filename": msg.get("filename", "unknown"),
            "size": msg.get("size", 0),
            "sha256": msg.get("sha256", ""),
            "stream_id": msg.get("stream_id", 0),
        }
        target: str = msg.get("target", "all")
        if target == "all":
            self.route_broadcast(file_info)
        else:
            self.route_unicast(target, file_info)

    def _process_file_data(self, msg: dict) -> None:
        """Chuyển tiếp dữ liệu file chunk.

        Args:
            msg: Dict chứa dữ liệu chunk file.
        """
        target: str = msg.get("target", "all")
        if target == "all":
            self.route_broadcast(msg)
        else:
            self.route_unicast(target, msg)

    # ─────────────────────── Routing ───────────────────────

    def route_broadcast(self, msg: dict) -> None:
        """Chuyển tiếp tin nhắn đến tất cả Session đang active.

        Args:
            msg: Dict message cần gửi.
        """
        if self._broadcast_fn is not None:
            self._broadcast_fn(
                self.username if self.username else "",
                msg,
                self.username,
            )

    def route_unicast(self, target_username: str, msg: dict) -> None:
        """Định tuyến dữ liệu trực tiếp đến người nhận.

        Args:
            target_username: Tên người nhận.
            msg: Dict message cần gửi.
        """
        if self._unicast_fn is not None:
            success: bool = self._unicast_fn(
                self.username if self.username else "",
                target_username,
                msg,
            )
            if not success:
                self._send_app_message({
                    "type": "ERROR",
                    "message": f"Người dùng '{target_username}' không trực tuyến.",
                })

    # ─────────────────────── Tiện ích ───────────────────────

    def _send_app_message(self, msg: dict) -> None:
        """Gửi một JSON message đến client qua Stream 0.

        Args:
            msg: Dict cần gửi.
        """
        stream = self.connection.get_or_create_stream(0)
        data: bytes = json.dumps(msg, ensure_ascii=False).encode("utf-8")
        stream.write(data)
