"""
client/client.py
~~~~~~~~~~~~~~~~
Client launcher point. Manages UDP connection to the server,
sends/receives messages, transfers files, and provides API for the HTTP UI.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import socket
import tempfile
import threading
import time
from typing import Optional
from urllib.parse import quote

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
    """UDP chat client connecting to the central server.

    Provides API to send messages, transfer files, and receive data
    from the server. Communicates with HTTP UI via an event queue.
    """

    def __init__(
        self,
        server_ip: str = DEFAULT_SERVER_HOST,
        server_port: int = DEFAULT_SERVER_PORT,
    ) -> None:
        """Initialize raw UDP socket and create Connection.

        Args:
            server_ip: Server IP address.
            server_port: Server UDP port.
        """
        self._server_addr: tuple[str, int] = (server_ip, server_port)
        self._sock: socket.socket = socket.socket(
            socket.AF_INET, socket.SOCK_DGRAM
        )
        self._sock.setblocking(False)
        self._sock.bind(("0.0.0.0", 0))  # Random available port

        self._connection: Connection = Connection(
            sock=self._sock,
            remote_addr=self._server_addr,
            is_server=False,
            on_app_message=self._on_app_message,
        )

        self._running: bool = False
        self._username: Optional[str] = None
        self._authenticated: bool = False

        # Event queue for UI
        self.event_queue: queue.Queue[dict] = queue.Queue()

        # Track file transfers
        self._file_transfers: dict[int, dict] = {}
        # Track incoming file downloads
        self._incoming_files: dict[int, dict] = {}
        self._download_dir: str = os.path.join(tempfile.gettempdir(), "streamchat_downloads")
        os.makedirs(self._download_dir, exist_ok=True)
        # Track active online users
        self.online_users: list[str] = []

    # ----------------------- Connection -----------------------

    def connect(self, username: str, password: str) -> bool:
        """Initiate handshake, send AUTH_REQ, and maintain event loop.

        Args:
            username: User login name.
            password: User password (will be hashed with SHA-256).

        Returns:
            ``True`` when connection has been started (auth result arrives via event).
        """
        self._username = username
        if not self._running:
            self._running = True

            # Begin handshake
            self._connection.handle_handshake()

            # Run network poll in background thread
            poll_thread = threading.Thread(target=self.poll_network, daemon=True)
            poll_thread.start()

            # Run tick loop
            tick_thread = threading.Thread(target=self._tick_loop, daemon=True)
            tick_thread.start()

        # Wait for handshake completion (up to 5 seconds)
        if not self._connection.handshake_complete.wait(timeout=5.0):
            logger.error("Handshake timeout – not sending AUTH (no session key)")
            return False

        # Send AUTH
        password_hash: str = hashlib.sha256(password.encode("utf-8")).hexdigest()
        auth_msg: dict = {
            "type": "AUTH",
            "username": username,
            "password_hash": password_hash,
        }
        self._send_json(auth_msg)
        return True

    # ----------------------- Chat -----------------------

    def send_chat(self, target: str, text: str) -> None:
        """Send a text message over Stream 0.

        Args:
            target: ``"all"`` for broadcast or username for unicast.
            text: Message text content.
        """
        msg: dict = {
            "type": "MSG",
            "target": target,
            "text": text,
        }
        self._send_json(msg)

    # ----------------------- File Transfer -----------------------

    def start_file_transfer(
        self, file_path: str, target: str = "all"
    ) -> Optional[int]:
        """Read file, calculate SHA-256, allocate new Stream ID, and transmit.

        Args:
            file_path: File path on disk.
            target: Recipient (``"all"`` or username).

        Returns:
            Allocated Stream ID, or ``None`` if file does not exist.
        """
        if not os.path.isfile(file_path):
            logger.error("File does not exist: %s", file_path)
            return None

        file_size: int = os.path.getsize(file_path)
        filename: str = os.path.basename(file_path)

        # Compute SHA-256
        sha256_hash = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                sha256_hash.update(chunk)
        file_hash: str = sha256_hash.hexdigest()

        # Allocate new stream
        stream = self._connection.allocate_stream()
        stream_id: int = stream.stream_id

        # Send FILE metadata over Stream 0
        file_msg: dict = {
            "type": "FILE",
            "filename": filename,
            "size": file_size,
            "sha256": file_hash,
            "stream_id": stream_id,
            "target": target,
        }
        self._send_json(file_msg)

        # Transfer file data in a dedicated thread
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
            "file_path": file_path,
        }
        self._incoming_files[stream_id] = {
            "filename": filename,
            "size": file_size,
            "sha256": file_hash,
            "from": self._username,
            "target": target,
            "received": file_size,
            "chunks": [],
            "file_path": file_path,
            "status": "completed",
            "download_url": f"/api/download?stream_id={stream_id}&filename={quote(filename)}",
        }

        return stream_id

    def _transfer_file_data(
        self, file_path: str, stream, target: str
    ) -> None:
        """Read and stream file data over specified Stream.

        Args:
            file_path: File path.
            stream: Stream object.
            target: Recipient.
        """
        try:
            with open(file_path, "rb") as f:
                while True:
                    chunk: bytes = f.read(4096)
                    if not chunk:
                        break
                    stream.write(chunk)

                    # Update progress
                    if stream.stream_id in self._file_transfers:
                        self._file_transfers[stream.stream_id]["sent"] += len(chunk)

                    # Simple rate limiting: check can_send with low latency sleep
                    while not self._connection.reliability.can_send():
                        time.sleep(0.001)

            if stream.stream_id in self._file_transfers:
                self._file_transfers[stream.stream_id]["status"] = "completed"

            self.event_queue.put({
                "type": "FILE_COMPLETE",
                "stream_id": stream.stream_id,
                "filename": os.path.basename(file_path),
            })
            logger.info("File transfer completed: %s", file_path)
        except OSError as exc:
            logger.error("File transfer error: %s", exc)
            if stream.stream_id in self._file_transfers:
                self._file_transfers[stream.stream_id]["status"] = "error"

    # ----------------------- Network I/O -----------------------

    def poll_network(self) -> None:
        """Continuously poll raw UDP socket, unpack, and process incoming packets.

        Runs in a dedicated thread. Reads datagrams and passes to Connection.
        """
        logger.info("Network polling started")
        while self._running:
            try:
                data, addr = self._sock.recvfrom(RECV_BUFFER_SIZE)
                self._connection.receive_packet(data)
            except BlockingIOError:
                time.sleep(0.005)
            except OSError as exc:
                if self._running:
                    logger.debug("Socket read error: %s", exc)
                time.sleep(0.01)

    def _tick_loop(self) -> None:
        """Run tick on the connection in a background thread."""
        while self._running:
            time.sleep(TICK_INTERVAL)
            self._connection.tick()
            if self._connection.closed:
                self._running = False
                self.event_queue.put({"type": "DISCONNECTED"})
                break

    # ----------------------- App message callback ----------------

    def _on_app_message(self, stream_id: int, data: bytes) -> None:
        """Callback when receiving a complete message from server.

        Args:
            stream_id: Stream ID.
            data: Message data bytes.
        """
        # If this is a chunk on an active file stream, assemble it
        if stream_id != 0 and stream_id in self._incoming_files:
            self._handle_incoming_file_chunk(stream_id, data)
            return

        try:
            msg: dict = json.loads(data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.debug("Non-JSON message on stream %d", stream_id)
            self.event_queue.put({
                "type": "RAW_DATA",
                "stream_id": stream_id,
                "data": data.hex(),
            })
            return

        # Handle AUTH_RESP
        if msg.get("type") == "AUTH_RESP":
            self._authenticated = msg.get("success", False)
            if self._authenticated:
                logger.info("Authentication succeeded: %s", self._username)
            else:
                logger.warning("Authentication failed")
        elif msg.get("type") == "LOGOUT_RESP":
            self._authenticated = False
            self._username = None
            self.online_users.clear()
        elif msg.get("type") == "KICKED":
            self._authenticated = False
            self._username = None
            self._running = False
            self.online_users.clear()
            if self._connection:
                self._connection.closed = True
            try:
                self._sock.close()
            except OSError:
                pass
            # Recreate clean socket & connection so future logins work cleanly
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setblocking(False)
            self._sock.bind(("0.0.0.0", 0))
            self._connection = Connection(
                sock=self._sock,
                remote_addr=self._server_addr,
                is_server=False,
                on_app_message=self._on_app_message,
            )
        elif msg.get("type") == "USER_LIST":
            self.online_users = list(msg.get("users", []))
        elif msg.get("type") in ("FILE", "FILE_READY"):
            sid = msg.get("stream_id")
            fname = msg.get("filename", "")
            fsize = msg.get("size", 0)
            fhash = msg.get("sha256", "")
            from_user = msg.get("from", "")
            target = msg.get("target", "all")
            dl_url = f"/api/download?stream_id={sid}&filename={quote(fname)}"
            msg["download_url"] = dl_url

            if sid is not None and sid not in self._incoming_files:
                self._incoming_files[sid] = {
                    "filename": fname,
                    "size": fsize,
                    "sha256": fhash,
                    "from": from_user,
                    "target": target,
                    "received": 0,
                    "chunks": [],
                    "file_path": None,
                    "status": "receiving",
                    "download_url": dl_url,
                }
                # Handle 0-byte file immediately
                if fsize == 0:
                    safe_name = os.path.basename(fname) or "empty_file"
                    saved_path = os.path.join(self._download_dir, f"{sid}_{safe_name}")
                    with open(saved_path, "wb") as f:
                        pass
                    self._incoming_files[sid]["file_path"] = saved_path
                    self._incoming_files[sid]["status"] = "completed"
                    msg["completed"] = True

        self.event_queue.put(msg)

    def _handle_incoming_file_chunk(self, stream_id: int, data: bytes) -> None:
        """Accumulate incoming raw file stream chunks and save to disk upon completion."""
        file_info = self._incoming_files.get(stream_id)
        if not file_info:
            return

        file_info["chunks"].append(data)
        file_info["received"] += len(data)

        # Notify progress if size is known
        if file_info["size"] > 0:
            progress_pct = int(min(100, (file_info["received"] / file_info["size"]) * 100))
            self.event_queue.put({
                "type": "FILE_PROGRESS",
                "stream_id": stream_id,
                "filename": file_info["filename"],
                "size": file_info["size"],
                "received": file_info["received"],
                "progress": progress_pct,
            })

        if file_info["received"] >= file_info["size"]:
            complete_bytes = b"".join(file_info["chunks"])
            file_info["chunks"].clear()

            calc_hash = hashlib.sha256(complete_bytes).hexdigest().lower()
            expected_hash = file_info.get("sha256", "").lower()
            if expected_hash and calc_hash != expected_hash:
                logger.error("Corrupted file on stream %d: hash mismatch", stream_id)
                file_info["status"] = "corrupted"
                self.event_queue.put({
                    "type": "FILE_ERROR",
                    "stream_id": stream_id,
                    "filename": file_info["filename"],
                    "message": "File integrity verification failed.",
                })
                return

            safe_name = os.path.basename(file_info["filename"]) or "downloaded_file"
            saved_path = os.path.join(self._download_dir, f"{stream_id}_{safe_name}")
            with open(saved_path, "wb") as f:
                f.write(complete_bytes)

            file_info["file_path"] = saved_path
            file_info["status"] = "completed"
            logger.info("File successfully received and saved: %s (%d bytes)", saved_path, len(complete_bytes))

            dl_url = f"/api/download?stream_id={stream_id}&filename={quote(safe_name)}"
            file_info["download_url"] = dl_url

            self.event_queue.put({
                "type": "FILE_READY",
                "stream_id": stream_id,
                "filename": file_info["filename"],
                "size": file_info["size"],
                "from": file_info.get("from", ""),
                "target": file_info.get("target", "all"),
                "download_url": dl_url,
                "completed": True,
            })

    def get_download_file(self, stream_id: int) -> Optional[tuple[str, str]]:
        """Retrieve (filename, file_path) for an assembled or sent file."""
        if stream_id in self._incoming_files:
            info = self._incoming_files[stream_id]
            fpath = info.get("file_path")
            if fpath and os.path.isfile(fpath):
                return (info.get("filename", os.path.basename(fpath)), fpath)
        if stream_id in self._file_transfers:
            info = self._file_transfers[stream_id]
            fpath = info.get("file_path")
            if fpath and os.path.isfile(fpath):
                return (info.get("filename", os.path.basename(fpath)), fpath)
        return None

    # ----------------------- Utilities -----------------------

    def _send_json(self, msg: dict) -> None:
        """Send a JSON message over Stream 0.

        Args:
            msg: Dict payload to send.
        """
        # Encryption happens in Connection._transmit_data only once the session key
        # exists; refuse to send application data in plaintext before that.
        if not self._connection.handshake_complete.is_set():
            logger.error("Refusing to send %s: handshake not complete", msg.get("type"))
            return
        stream = self._connection.get_or_create_stream(0)
        data: bytes = json.dumps(msg, ensure_ascii=False).encode("utf-8")
        stream.write(data)

    def get_file_progress(self) -> dict[int, dict]:
        """Get current file transfer progress.

        Returns:
            Dict ``stream_id -> {filename, size, sent, status}``.
        """
        return dict(self._file_transfers)

    def disconnect(self) -> None:
        """Disconnect and clean up resources."""
        self._running = False
        try:
            self._sock.close()
        except OSError:
            logger.debug("Socket already closed")
        logger.info("Client disconnected")

    def logout(self) -> None:
        """Log out from the server without closing the reusable UDP connection."""
        if not self._authenticated:
            return

        self._send_json({"type": "LOGOUT"})
        self._authenticated = False
        self._username = None
        self.online_users.clear()
        logger.info("Client logged out")

    def request_user_list(self) -> None:
        """Request updated user list from server over Stream 0."""
        if self._authenticated and self._connection and not self._connection.closed:
            self._send_json({"type": "USER_LIST_REQ"})

    @property
    def is_authenticated(self) -> bool:
        """Current authentication status."""
        return self._authenticated

    @property
    def username(self) -> Optional[str]:
        """Current username."""
        return self._username
