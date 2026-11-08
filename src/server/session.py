"""
server/session.py
~~~~~~~~~~~~~~~~~
Application layer session management for each client connected to the server.

Each Session corresponds to a UDP Connection and maintains authentication
state, username, and message routing.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Optional, Callable, Any

from transport.connection import Connection
from server.database import db

logger = logging.getLogger(__name__)

MAX_APP_MESSAGE_BYTES = 64 * 1024
MAX_USERNAME_LENGTH = 32
MAX_CHAT_TEXT_LENGTH = 10_000
MAX_FILENAME_LENGTH = 255
MAX_DOWNLOAD_URL_LENGTH = 2_048
MAX_FILE_CHUNK_LENGTH = 48 * 1024
MAX_FILE_SIZE = (1 << 63) - 1
MAX_STREAM_ID = (1 << 16) - 1


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
        broadcast_fn: Optional[Callable[[str, dict[str, Any], Optional[str]], None]] = None,
        unicast_fn: Optional[Callable[[str, str, dict[str, Any]], bool]] = None,
        on_auth_success: Optional[Callable[[int, str], None]] = None,
        on_auth_logout: Optional[Callable[[int, str], None]] = None,
        file_chunk_fn: Optional[Callable[[str, str, int, bytes], bool]] = None,
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
            on_auth_success: Callback invoked when authentication succeeds
                             ``(conn_id, username) -> None``.
            on_auth_logout: Callback invoked when an authenticated user logs out
                            ``(conn_id, username) -> None``.
            file_chunk_fn: Callback for authenticated raw file chunks
                           ``(sender, target, stream_id, data) -> bool``.
        """
        self.conn_id: int = conn_id
        self.client_addr: tuple[str, int] = client_addr
        self.connection: Connection = connection
        self.username: Optional[str] = None
        self.authenticated: bool = False
        self._broadcast_fn: Optional[Callable[[str, dict[str, Any], Optional[str]], None]] = broadcast_fn
        self._unicast_fn: Optional[Callable[[str, str, dict[str, Any]], bool]] = unicast_fn
        self._on_auth_success: Optional[Callable[[int, str], None]] = on_auth_success
        self._on_auth_logout: Optional[Callable[[int, str], None]] = on_auth_logout
        self._file_chunk_fn: Optional[Callable[[str, str, int, bytes], bool]] = file_chunk_fn
        self._file_transfers: dict[int, tuple[str, int, int]] = {}

    # ----------------------- Payload processing -----------------------

    def process_payload(self, app_msg: bytes) -> None:
        """Parse JSON payload and dispatch handling.

        Supported payload types:
        - ``AUTH``: Login request.
        - ``MSG``: Chat message (broadcast or unicast).
        - ``FILE`` / ``FILE_READY`` / ``FILE_START``: File transfer metadata.
        - ``FILE_DATA``: File chunk data.

        Args:
            app_msg: Application payload bytes (JSON encoded).
        """
        if not isinstance(app_msg, bytes):
            self._reject_payload("Invalid payload encoding.")
            return
        if len(app_msg) > MAX_APP_MESSAGE_BYTES:
            self._reject_payload("Application message is too large.")
            return

        try:
            msg: Any = json.loads(app_msg.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.warning("Corrupted JSON payload from %s: %s", self.client_addr, exc)
            self._send_app_message({
                "type": "ERROR",
                "message": "Invalid JSON payload.",
            })
            return

        if not isinstance(msg, dict):
            self._reject_payload("Application message must be a JSON object.")
            return

        msg_type = msg.get("type")
        if not isinstance(msg_type, str):
            self._reject_payload("Message type must be a string.")
            return

        if msg_type not in ("AUTH", "LOGOUT") and (not self.authenticated or not self.username):
            self._send_app_message({
                "type": "ERROR",
                "message": "You are not logged in.",
            })
            return

        if msg_type == "AUTH":
            self._process_auth(msg)
        elif msg_type == "LOGOUT":
            self._process_logout()
        elif msg_type == "MSG":
            self._process_msg(msg)
        elif msg_type in ("FILE", "FILE_START", "FILE_READY"):
            self._process_file(msg)
        elif msg_type == "FILE_DATA":
            self._process_file_data(msg)
        else:
            logger.debug("Unknown message type: %s from %s", msg_type, self.client_addr)

    # ----------------------- Auth -----------------------

    def _process_auth(self, msg: dict) -> None:
        """Process authentication request.

        Args:
            msg: Dict containing ``username`` and ``password_hash``.
        """
        username = msg.get("username")
        password_hash: str = msg.get("password_hash", "")
        if (
            not isinstance(username, str)
            or not username.strip()
            or len(username.strip()) > MAX_USERNAME_LENGTH
            or not isinstance(password_hash, str)
            or len(password_hash) != 64
            or any(char not in "0123456789abcdefABCDEF" for char in password_hash)
        ):
            self._reject_payload("Invalid username or password hash.")
            return

        username = username.strip().lower()
        password_hash = password_hash.lower()
        was_authenticated = self.authenticated
        success: bool = self.verify_auth(username, password_hash)
        if was_authenticated and not success:
            self._send_app_message({
                "type": "ERROR",
                "message": "Log out before signing into a different account.",
            })
            return

        response: dict = {
            "type": "AUTH_RESP",
            "success": success,
            "username": username,
        }

        if success:
            response["message"] = f"Welcome {username}!"
            self._send_app_message(response)

            if not was_authenticated:
                # Trigger server-level deduplication before announcing the new login.
                if self._on_auth_success is not None:
                    self._on_auth_success(self.conn_id, username)

                if self._broadcast_fn is not None:
                    join_msg: dict = {
                        "type": "SYSTEM",
                        "message": f"User @{username} has joined the chat room.",
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
        if (
            not isinstance(username, str)
            or not isinstance(password_hash, str)
            or not username
            or len(username) > MAX_USERNAME_LENGTH
            or len(password_hash) != 64
            or any(char not in "0123456789abcdef" for char in password_hash.lower())
        ):
            return False

        username = username.strip().lower()
        password_hash = password_hash.lower()
        if not username:
            logger.warning("Authentication failed: %s (conn_id=%d)", username, self.conn_id)
            return False
        if self.authenticated and self.username != username:
            logger.warning(
                "Rejected account switch on authenticated session (conn_id=%d)",
                self.conn_id,
            )
            return False

        if db.verify_user(username, password_hash):
            if not self.authenticated:
                self.username = username
                self.authenticated = True
            logger.info("Authentication succeeded: %s (conn_id=%d)", username, self.conn_id)
            return True
        logger.warning("Authentication failed: %s (conn_id=%d)", username, self.conn_id)
        return False

    def _process_logout(self) -> None:
        """Log out the current user while keeping the transport connection open."""
        username = self.username
        was_authenticated = self.authenticated and username is not None
        self.authenticated = False
        self.username = None

        self._send_app_message({
            "type": "LOGOUT_RESP",
            "success": True,
        })

        if was_authenticated and username is not None and self._on_auth_logout is not None:
            self._on_auth_logout(self.conn_id, username)

    # ----------------------- Chat -----------------------

    def _process_msg(self, msg: dict) -> None:
        """Process chat message.

        Args:
            msg: Dict containing ``target`` (``"all"`` or username) and ``text``.
        """
        if not self.authenticated or not self.username:
            self._send_app_message({
                "type": "ERROR",
                "message": "You are not logged in.",
            })
            return

        target = self._validated_target(msg)
        text = msg.get("text")
        if target is None:
            return
        if not isinstance(text, str) or len(text) > MAX_CHAT_TEXT_LENGTH or not text.strip():
            self._reject_payload("Chat text must be a non-empty string within the size limit.")
            return

        chat_msg: dict = {
            "type": "MSG",
            "from": self.username,
            "text": text,
            "target": target,
            "timestamp": time.time(),
        }

        if target == "all":
            self.route_broadcast(chat_msg)
        else:
            self.route_unicast(target, chat_msg)

    # ----------------------- File Transfer -----------------------

    def _process_file(self, msg: dict) -> None:
        """Process file transfer start/ready notification.

        Args:
            msg: Dict containing ``filename``, ``size``, ``sha256``, ``target``.
        """
        if not self.authenticated or not self.username:
            self._send_app_message({
                "type": "ERROR",
                "message": "You are not logged in.",
            })
            return

        target = self._validated_target(msg)
        filename = msg.get("filename")
        size = msg.get("size")
        sha256 = msg.get("sha256")
        stream_id = msg.get("stream_id")
        download_url = msg.get("download_url", "")
        if target is None:
            return
        if (
            not isinstance(filename, str)
            or not filename.strip()
            or len(filename) > MAX_FILENAME_LENGTH
            or any(ord(char) < 32 or ord(char) == 127 for char in filename)
            or "/" in filename
            or "\\" in filename
        ):
            self._reject_payload("Filename is invalid or too long.")
            return
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or size > MAX_FILE_SIZE
        ):
            self._reject_payload("File size must be a non-negative integer within the limit.")
            return
        if (
            not isinstance(sha256, str)
            or len(sha256) != 64
            or any(char not in "0123456789abcdefABCDEF" for char in sha256)
        ):
            self._reject_payload("SHA-256 must contain exactly 64 hexadecimal characters.")
            return
        if (
            not isinstance(stream_id, int)
            or isinstance(stream_id, bool)
            or not 1 <= stream_id <= MAX_STREAM_ID
        ):
            self._reject_payload("File stream ID is invalid.")
            return
        if stream_id in self._file_transfers:
            self._reject_payload("File stream ID is already in use.")
            return
        if not isinstance(download_url, str) or len(download_url) > MAX_DOWNLOAD_URL_LENGTH:
            self._reject_payload("Download URL must be a string within the size limit.")
            return

        file_info: dict = {
            "type": "FILE_READY",
            "from": self.username,
            "filename": filename.strip(),
            "size": size,
            "sha256": sha256.lower(),
            "stream_id": stream_id,
            "target": target,
            "download_url": download_url,
            "timestamp": time.time(),
        }

        if target == "all":
            self.route_broadcast(file_info)
            if size > 0:
                self._file_transfers[stream_id] = (target, size, 0)
        else:
            if self.route_unicast(target, file_info) and size > 0:
                self._file_transfers[stream_id] = (target, size, 0)

    def _process_file_data(self, msg: dict) -> None:
        """Forward file chunk data to target.

        Args:
            msg: Dict containing ``stream_id``, ``offset``, ``data``, and ``target``.
        """
        if not self.authenticated or not self.username:
            self._send_app_message({
                "type": "ERROR",
                "message": "You are not logged in.",
            })
            return

        target = self._validated_target(msg)
        if target is None:
            return
        stream_id = msg.get("stream_id")
        offset = msg.get("offset")
        chunk = msg.get("data")
        if (
            not isinstance(stream_id, int)
            or isinstance(stream_id, bool)
            or not 1 <= stream_id <= MAX_STREAM_ID
        ):
            self._reject_payload("File stream ID is invalid.")
            return
        if (
            not isinstance(offset, int)
            or isinstance(offset, bool)
            or not 0 <= offset <= MAX_FILE_SIZE
        ):
            self._reject_payload("File chunk offset is invalid.")
            return
        if (
            not isinstance(chunk, str)
            or not chunk
            or len(chunk.encode("utf-8")) > MAX_FILE_CHUNK_LENGTH
        ):
            self._reject_payload("File chunk data is invalid or too large.")
            return

        forwarded_msg = dict(msg)
        forwarded_msg["from"] = self.username
        forwarded_msg["target"] = target

        if target == "all":
            self.route_broadcast(forwarded_msg)
        else:
            self.route_unicast(target, forwarded_msg)

    def process_file_chunk(self, stream_id: int, data: bytes) -> None:
        """Route a raw transport-stream chunk for previously validated metadata."""
        if not self.authenticated or not self.username:
            self._send_app_message({
                "type": "ERROR",
                "message": "You are not logged in.",
            })
            return
        if (
            not isinstance(stream_id, int)
            or isinstance(stream_id, bool)
            or not 1 <= stream_id <= MAX_STREAM_ID
            or not isinstance(data, bytes)
            or not data
            or len(data) > MAX_FILE_CHUNK_LENGTH
        ):
            self._reject_payload("File chunk is invalid or too large.")
            return

        transfer = self._file_transfers.get(stream_id)
        if transfer is None:
            self._reject_payload("File chunk has no validated metadata.")
            return

        target, expected_size, received_size = transfer
        if received_size + len(data) > expected_size:
            self._file_transfers.pop(stream_id, None)
            self._reject_payload("File chunks exceed the declared file size.")
            return

        if self._file_chunk_fn is None or not self._file_chunk_fn(
            self.username, target, stream_id, data
        ):
            self._file_transfers.pop(stream_id, None)
            if target != "all":
                self._send_app_message({
                    "type": "ERROR",
                    "message": f"User '{target}' is offline or does not exist.",
                })
            return

        received_size += len(data)
        if received_size == expected_size:
            self._file_transfers.pop(stream_id, None)
        else:
            self._file_transfers[stream_id] = (
                target,
                expected_size,
                received_size,
            )

    def _validated_target(self, msg: dict[str, Any]) -> Optional[str]:
        """Validate and normalize a routing target from an application message."""
        target = msg.get("target", "all")
        if not isinstance(target, str):
            self._reject_payload("Target must be a string.")
            return None

        target = target.strip().lower() or "all"
        if len(target) > MAX_USERNAME_LENGTH:
            self._reject_payload("Target is too long.")
            return None
        return target

    def _reject_payload(self, reason: str) -> None:
        """Log and report a malformed or invalid application request."""
        logger.warning("Rejected application payload from %s: %s", self.client_addr, reason)
        self._send_app_message({
            "type": "ERROR",
            "message": reason,
        })

    # ----------------------- Routing -----------------------

    def route_broadcast(self, msg: dict) -> bool:
        """Forward message to all active sessions.

        Args:
            msg: Dict message to broadcast.
        """
        if not self.authenticated or not self.username:
            self._send_app_message({
                "type": "ERROR",
                "message": "You are not logged in.",
            })
            return False

        if self._broadcast_fn is not None:
            self._broadcast_fn(
                self.username,
                msg,
                self.username,
            )
            return True
        return False

    def route_unicast(self, target_username: str, msg: dict) -> bool:
        """Route message directly to recipient.

        Args:
            target_username: Recipient username.
            msg: Dict message to send.
        """
        if not self.authenticated or not self.username:
            self._send_app_message({
                "type": "ERROR",
                "message": "You are not logged in.",
            })
            return False
        if not isinstance(target_username, str):
            self._reject_payload("Target must be a string.")
            return False

        target_username = target_username.strip().lower()

        # If sending to self, echo back
        if target_username == self.username:
            self._send_app_message(msg)
            return True

        if self._unicast_fn is not None:
            success: bool = self._unicast_fn(
                self.username,
                target_username,
                msg,
            )
            if not success:
                self._send_app_message({
                    "type": "ERROR",
                    "message": f"User '{target_username}' is offline or does not exist.",
                })
            return success
        else:
            self._send_app_message({
                "type": "ERROR",
                "message": f"User '{target_username}' is offline or does not exist.",
            })
            return False

    def send_kicked(self, reason: str = "Logged in from another location.") -> None:
        """Send KICKED message to this session."""
        self._send_app_message({
            "type": "KICKED",
            "message": reason,
        })

    # ----------------------- Utilities -----------------------

    def _send_app_message(self, msg: dict) -> None:
        """Send a JSON message to client over Stream 0.

        Args:
            msg: Dict payload to send.
        """
        try:
            stream = self.connection.get_or_create_stream(0)
            data: bytes = json.dumps(msg, ensure_ascii=False).encode("utf-8")
            stream.write(data)
        except Exception as exc:
            logger.error(
                "Failed to send app message to conn_id=%d: %s",
                self.conn_id,
                exc,
            )
