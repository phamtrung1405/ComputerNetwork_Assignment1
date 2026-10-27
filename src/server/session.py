"""
server/session.py
~~~~~~~~~~~~~~~~~
Application layer session management for each client connected to the server.

Each Session corresponds to a UDP Connection and maintains authentication
state, username, and message routing.
"""

from __future__ import annotations

import hashlib
import json
import logging
import socket
import time
from typing import Optional, Callable

from transport.connection import Connection
from server.database import db

logger = logging.getLogger(__name__)


class Session:
    """Manages the session state of a client on the server.

    Maintains login state, processes application payloads (AUTH, MSG, FILE),
    and routes messages between sessions.
    """

    def __init__(
        self,
        conn_id: int,
        client_addr: tuple[str, int],
        connection: Connection,
        broadcast_fn: Optional[Callable[[str, dict[str, object], Optional[str]], None]] = None,
        unicast_fn: Optional[Callable[[str, str, dict[str, object]], bool]] = None,
    ) -> None:
        """Initialize Session.

        Args:
            conn_id: Connection identifier.
            client_addr: Address ``(host, port)`` of the client.
            connection: Associated Connection instance.
            broadcast_fn: Callback to broadcast
                          ``(sender_username, msg_dict, exclude_username) -> None``.
            unicast_fn: Callback for unicast
                        ``(sender, target, msg_dict) -> bool``.
        """
        self.conn_id: int = conn_id
        self.client_addr: tuple[str, int] = client_addr
        self.connection: Connection = connection
        self.username: Optional[str] = None
        self.authenticated: bool = False
        self._broadcast_fn: Optional[Callable] = broadcast_fn
        self._unicast_fn: Optional[Callable] = unicast_fn

    # ----------------------- Payload processing -----------------------

    def process_payload(self, app_msg: bytes) -> None:
        """Parse JSON payload and dispatch handling.

        Supported payload types:
        - ``AUTH``: Login request.
        - ``MSG``: Chat message (broadcast or unicast).
        - ``FILE``: File transfer metadata.
        - ``FILE_DATA``: File chunk data.

        Args:
            app_msg: Application payload bytes (JSON encoded).
        """
        try:
            msg: dict = json.loads(app_msg.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.warning("Corrupted JSON payload from %s: %s", self.client_addr, exc)
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
            logger.debug("Unknown message type: %s", msg_type)

    # ----------------------- Auth -----------------------

    def _process_auth(self, msg: dict) -> None:
        """Process authentication request.

        Args:
            msg: Dict containing ``username`` and ``password_hash``.
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
            response["message"] = f"Welcome {username}!"
            # Broadcast join notice
            if self._broadcast_fn is not None:
                join_msg: dict = {
                    "type": "SYSTEM",
                    "message": f"{username} joined the chat room.",
                }
                self._broadcast_fn(username, join_msg, username)
        else:
            response["message"] = "Invalid username or password."

        self._send_app_message(response)

    def verify_auth(self, username: str, password_hash: str) -> bool:
        """Verify credentials against SQLite database.

        Args:
            username: Username string.
            password_hash: SHA-256 hash of password.

        Returns:
            ``True`` if authentication succeeds.
        """
        if db.verify_user(username, password_hash):
            self.username = username
            self.authenticated = True
            logger.info("Authentication succeeded: %s", username)
            return True
        logger.warning("Authentication failed: %s", username)
        return False

    # ----------------------- Chat -----------------------

    def _process_msg(self, msg: dict) -> None:
        """Process chat message.

        Args:
            msg: Dict containing ``target`` (``"all"`` or username) and ``text``.
        """
        if not self.authenticated:
            self._send_app_message({
                "type": "ERROR",
                "message": "You are not logged in.",
            })
            return

        target: str = msg.get("target", "all")
        text: str = msg.get("text", "")

        chat_msg: dict = {
            "type": "MSG",
            "from": self.username,
            "text": text,
            "timestamp": time.time(),
        }

        if target == "all":
            self.route_broadcast(chat_msg)
        else:
            self.route_unicast(target, chat_msg)

    def _process_file(self, msg: dict) -> None:
        """Process file transfer start notification.

        Args:
            msg: Dict containing ``filename``, ``size``, ``sha256``, ``target``.
        """
        if not self.authenticated:
            self._send_app_message({
                "type": "ERROR",
                "message": "You are not logged in.",
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
        """Forward file chunk data.

        Args:
            msg: Dict containing file chunk data.
        """
        target: str = msg.get("target", "all")
        if target == "all":
            self.route_broadcast(msg)
        else:
            self.route_unicast(target, msg)

    # ----------------------- Routing -----------------------

    def route_broadcast(self, msg: dict) -> None:
        """Forward message to all active sessions.

        Args:
            msg: Dict message to broadcast.
        """
        if self._broadcast_fn is not None:
            self._broadcast_fn(
                self.username if self.username else "",
                msg,
                self.username,
            )

    def route_unicast(self, target_username: str, msg: dict) -> None:
        """Route message directly to recipient.

        Args:
            target_username: Recipient username.
            msg: Dict message to send.
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
                    "message": f"User '{target_username}' is offline.",
                })

    # ----------------------- Utilities -----------------------

    def _send_app_message(self, msg: dict) -> None:
        """Send a JSON message to client over Stream 0.

        Args:
            msg: Dict payload to send.
        """
        stream = self.connection.get_or_create_stream(0)
        data: bytes = json.dumps(msg, ensure_ascii=False).encode("utf-8")
        stream.write(data)
