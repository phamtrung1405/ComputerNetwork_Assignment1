"""
server/server.py
~~~~~~~~~~~~~~~~
Central server entry point.

Initializes non-blocking raw UDP socket, uses ``selectors`` for the event loop,
manages all client sessions, and dispatches datagrams.
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
    """Central UDP server managing multiple client connections.

    Uses ``selectors`` to multiplex I/O on a single socket.
    Each client is managed by an independent Session.
    """

    def __init__(self) -> None:
        """Initialize server with session management data structures."""
        self._sock: Optional[socket.socket] = None
        self._sel: Optional[selectors.DefaultSelector] = None
        self._running: bool = False

        # -- Session management --
        # conn_id -> Session
        self._sessions: dict[int, Session] = {}
        self._sessions_lock: threading.Lock = threading.Lock()

        # addr -> conn_id (mapping to identify client from address)
        self._addr_to_conn: dict[tuple[str, int], int] = {}

        # username -> conn_id (mapping for unicast routing)
        self._user_to_conn: dict[str, int] = {}

    # ----------------------- Startup -----------------------

    def start_server(
        self,
        host: str = DEFAULT_SERVER_HOST,
        port: int = DEFAULT_SERVER_PORT,
    ) -> None:
        """Initialize non-blocking raw UDP socket and start the event loop.

        Args:
            host: Listening IP address.
            port: Listening UDP port.
        """
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((host, port))
        self._sock.setblocking(False)

        self._sel = selectors.DefaultSelector()
        self._sel.register(self._sock, selectors.EVENT_READ)

        self._running = True
        logger.info("Server listening at %s:%d (UDP)", host, port)

        # Run tick loop in background thread
        tick_thread = threading.Thread(target=self._tick_loop, daemon=True)
        tick_thread.start()

        # Main event loop
        try:
            while self._running:
                events = self._sel.select(timeout=TICK_INTERVAL)
                for key, mask in events:
                    if key.fileobj is self._sock:
                        self._read_datagrams()
        except KeyboardInterrupt:
            logger.info("Server shutting down...")
        finally:
            self._cleanup()

    def _read_datagrams(self) -> None:
        """Read all available datagrams in the socket buffer."""
        while True:
            try:
                data, addr = self._sock.recvfrom(RECV_BUFFER_SIZE)
                self.handle_datagram(data, addr)
            except BlockingIOError:
                break
            except OSError as exc:
                logger.warning("Socket read error: %s", exc)
                break

    # ----------------------- Datagram Handling -----------------------

    def handle_datagram(self, data: bytes, addr: tuple[str, int]) -> None:
        """Extract Connection ID from datagram and forward to the Session.

        Args:
            data: Raw datagram bytes received.
            addr: Source ``(host, port)`` address.
        """
        try:
            header, payload = packet.unpack(data)
        except ValueError:
            logger.debug("Corrupted datagram from %s – discarded", addr)
            return

        conn_id: int = header.conn_id

        with self._sessions_lock:
            if conn_id not in self._sessions:
                # Create new session for newly connected client
                self._create_session(conn_id, addr)
                self._addr_to_conn[addr] = conn_id

            session: Session = self._sessions[conn_id]

        # Update address if client changed ports (NAT rebinding support)
        session.connection.remote_addr = addr
        session.connection.receive_packet(data)

        # Update user mapping if authenticated
        if session.authenticated and session.username:
            self._user_to_conn[session.username] = conn_id

    def _create_session(self, conn_id: int, addr: tuple[str, int]) -> None:
        """Create a new Session for a client.

        Args:
            conn_id: Connection ID.
            addr: Client address tuple.
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
        logger.info("New session: conn_id=%d from %s", conn_id, addr)

    # ----------------------- App message callback ----------------

    def _on_app_message(
        self, conn_id: int, stream_id: int, data: bytes
    ) -> None:
        """Callback invoked when a complete message is received from transport layer.

        Args:
            conn_id: Connection ID.
            stream_id: Stream ID.
            data: Message data bytes.
        """
        with self._sessions_lock:
            session = self._sessions.get(conn_id)
        if session is not None:
            session.process_payload(data)

    # ----------------------- Broadcast & Unicast -----------------

    def _broadcast(
        self,
        sender: str,
        msg: dict,
        exclude: Optional[str] = None,
    ) -> None:
        """Send message to all active authenticated sessions.

        Args:
            sender: Sender username.
            msg: Message dict.
            exclude: Username to exclude (typically the sender).
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
        """Send message directly to a specific target user.

        Args:
            sender: Sender username.
            target_username: Target username.
            msg: Message dict.

        Returns:
            ``True`` if recipient is online and delivered.
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

    # ----------------------- Tick loop -----------------------

    def _tick_loop(self) -> None:
        """Execute tick for all connections in background thread."""
        while self._running:
            time.sleep(TICK_INTERVAL)
            with self._sessions_lock:
                closed_ids: list[int] = []
                for conn_id, session in self._sessions.items():
                    session.connection.tick()
                    if session.connection.closed:
                        closed_ids.append(conn_id)

                for cid in closed_ids:
                    s = self._sessions.pop(cid, None)
                    if s and s.username:
                        self._user_to_conn.pop(s.username, None)
                        logger.info("Session closed: %s (conn_id=%d)", s.username, cid)

    # ----------------------- Cleanup -----------------------

    def _cleanup(self) -> None:
        """Clean up resources on server shutdown."""
        self._running = False
        if self._sel:
            self._sel.close()
        if self._sock:
            self._sock.close()
        logger.info("Server stopped.")


def main() -> None:
    """Main entry point to run server."""
    server = ChatServer()
    server.start_server()


if __name__ == "__main__":
    main()
