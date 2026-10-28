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
from typing import Optional, Any

from common import packet
from common.constants import (
    DEFAULT_SERVER_HOST,
    DEFAULT_SERVER_PORT,
    PKT_CLOSE,
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

    Uses ``selectors`` to multiplex I/O on a single non-blocking socket.
    Each client is managed by an independent Session with thread-safe routing.
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
            logger.info("Server shutting down by user interrupt...")
        except Exception as exc:
            logger.error("Unexpected error in server event loop: %s", exc)
        finally:
            self._cleanup()

    def _read_datagrams(self) -> None:
        """Read all available datagrams in the socket buffer until exhausted."""
        if self._sock is None:
            return

        while True:
            try:
                data, addr = self._sock.recvfrom(RECV_BUFFER_SIZE)
                try:
                    self.handle_datagram(data, addr)
                except Exception as exc:
                    logger.warning("Error processing datagram from %s: %s", addr, exc)
            except BlockingIOError:
                break
            except OSError as exc:
                if self._running:
                    logger.warning("Socket read error: %s", exc)
                break

    # ----------------------- Datagram Handling -----------------------

    def handle_datagram(self, data: bytes, addr: tuple[str, int]) -> None:
        """Extract Connection ID from datagram and forward to the Session.

        Supports NAT Rebinding by updating the client address if port/IP changes.

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
            session = self._sessions.get(conn_id)
            if session is None:
                # Create new session for newly connected client
                self._create_session(conn_id, addr)
                session = self._sessions.get(conn_id)
                if session is None:
                    return
                mapped_conn_id = self._addr_to_conn.get(addr)
                if mapped_conn_id is None or mapped_conn_id == conn_id:
                    self._addr_to_conn[addr] = conn_id

            old_addr = session.client_addr

            if old_addr == addr:
                session.connection.remote_addr = addr

        # A changed source address is accepted only with valid AEAD-authenticated
        # DATA. Do not let an unauthenticated packet redirect future server traffic.
        if old_addr != addr:
            if not session.connection.verify_migration_datagram(data):
                logger.warning(
                    "Rejected NAT rebinding for conn_id=%d from %s",
                    conn_id,
                    addr,
                )
                return

            with self._sessions_lock:
                if self._sessions.get(conn_id) is not session:
                    return
                if session.client_addr != old_addr and session.client_addr != addr:
                    logger.debug(
                        "Rejected concurrent NAT rebinding for conn_id=%d from %s",
                        conn_id,
                        addr,
                    )
                    return

                mapped_conn_id = self._addr_to_conn.get(addr)
                if mapped_conn_id is not None and mapped_conn_id != conn_id:
                    logger.warning(
                        "Rejected NAT rebinding for conn_id=%d: address %s belongs to conn_id=%d",
                        conn_id,
                        addr,
                        mapped_conn_id,
                    )
                    return

                if session.client_addr != addr:
                    if self._addr_to_conn.get(old_addr) == conn_id:
                        self._addr_to_conn.pop(old_addr, None)
                    self._addr_to_conn[addr] = conn_id
                    session.client_addr = addr
                    session.connection.remote_addr = addr
                    logger.info(
                        "NAT Rebinding verified for conn_id=%d: %s -> %s",
                        conn_id,
                        old_addr,
                        addr,
                    )

        # Forward datagram into transport connection
        try:
            session.connection.receive_packet(data)
        except Exception as exc:
            logger.warning("Error in receive_packet for conn_id=%d: %s", conn_id, exc)

        # Detect explicit close packet
        if header.msg_type == PKT_CLOSE:
            logger.info("Client requested disconnect: conn_id=%d", conn_id)
            session.connection.closed = True

        # Synchronize user mapping if authenticated
        with self._sessions_lock:
            if session.authenticated and session.username:
                self._user_to_conn[session.username] = conn_id

    def _create_session(self, conn_id: int, addr: tuple[str, int]) -> None:
        """Create a new Session for a client.

        Args:
            conn_id: Connection ID.
            addr: Client address tuple.
        """
        sock = self._sock
        if sock is None:
            # Fallback for testing when start_server() hasn't bound a socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setblocking(False)
            self._sock = sock

        connection = Connection(
            sock=sock,
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
            on_auth_success=self._on_auth_success,
            on_auth_logout=self._on_auth_logout,
            file_chunk_fn=self._route_file_chunk,
        )

        self._sessions[conn_id] = session
        logger.info("New session created: conn_id=%d from %s", conn_id, addr)

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
            if stream_id == 0:
                session.process_payload(data)
            else:
                session.process_file_chunk(stream_id, data)

    def _route_file_chunk(
        self,
        sender: str,
        target_username: str,
        stream_id: int,
        data: bytes,
    ) -> bool:
        """Forward a validated raw file chunk to authenticated recipient streams."""
        with self._sessions_lock:
            if target_username == "all":
                targets = [
                    session
                    for session in self._sessions.values()
                    if session.authenticated
                    and session.username
                    and session.username != sender
                    and not session.connection.closed
                ]
            else:
                conn_id = self._user_to_conn.get(target_username)
                session = self._sessions.get(conn_id) if conn_id is not None else None
                if (
                    session is None
                    or not session.authenticated
                    or session.username != target_username
                    or session.connection.closed
                ):
                    return False
                targets = [session]

        for session in targets:
            try:
                session.connection.get_or_create_stream(stream_id).write(data)
            except Exception:
                logger.exception(
                    "Failed to forward file chunk to user '%s' (conn_id=%d)",
                    session.username,
                    session.conn_id,
                )
                if target_username != "all":
                    return False
        return True

    def _on_auth_success(self, conn_id: int, username: str) -> None:
        """Callback invoked when a session successfully authenticates.

        Deduplicates previous logins under the same username and broadcasts
        the refreshed user list to all online peers.

        Args:
            conn_id: Connection ID of newly authenticated user.
            username: Authenticated username.
        """
        with self._sessions_lock:
            old_conn_id = self._user_to_conn.get(username)
            if old_conn_id is not None and old_conn_id != conn_id:
                logger.info(
                    "User '%s' reconnected from new conn_id=%d. Terminating old session %d",
                    username,
                    conn_id,
                    old_conn_id,
                )
                old_session = self._sessions.get(old_conn_id)
                if old_session is not None:
                    old_session.send_kicked("Your account logged in from another location.")
                    old_session.authenticated = False
                    old_session.username = None
                    old_session.connection.closed = True

            self._user_to_conn[username] = conn_id

        # Broadcast live user list to all online clients
        self.broadcast_user_list()

    def _on_auth_logout(self, conn_id: int, username: str) -> None:
        """Remove the user's online mapping and notify remaining authenticated peers."""
        with self._sessions_lock:
            if self._user_to_conn.get(username) == conn_id:
                self._user_to_conn.pop(username, None)

        self._broadcast(
            "SYSTEM",
            {"type": "SYSTEM", "message": f"User @{username} has left the chat room."},
        )
        self.broadcast_user_list()

    # ----------------------- Active User List --------------------

    def get_online_users(self) -> list[str]:
        """Return a sorted list of unique online authenticated usernames."""
        with self._sessions_lock:
            online: set[str] = set()
            for session in self._sessions.values():
                if session.authenticated and session.username and not session.connection.closed:
                    online.add(session.username)
            return sorted(online)

    def broadcast_user_list(self) -> None:
        """Broadcast updated online user list to all active authenticated sessions."""
        online_users = self.get_online_users()
        msg = {
            "type": "USER_LIST",
            "users": online_users,
        }
        self._broadcast("SYSTEM", msg)

    # ----------------------- Broadcast & Unicast -----------------

    def _broadcast(
        self,
        sender: str,
        msg: dict[str, Any],
        exclude: Optional[str] = None,
    ) -> None:
        """Send message to all active authenticated sessions.

        Args:
            sender: Sender username.
            msg: Message dict.
            exclude: Username to exclude (typically the sender).
        """
        try:
            data: bytes = json.dumps(msg, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            logger.error("Broadcast serialization error: %s", exc)
            return

        with self._sessions_lock:
            targets = [
                session
                for session in self._sessions.values()
                if session.authenticated
                and session.username
                and not session.connection.closed
                and (not exclude or session.username != exclude)
            ]

        for session in targets:
            try:
                stream = session.connection.get_or_create_stream(0)
                stream.write(data)
            except Exception as exc:
                logger.warning(
                    "Failed to broadcast to user '%s' (conn_id=%d): %s",
                    session.username,
                    session.conn_id,
                    exc,
                )

    def _unicast(
        self, sender: str, target_username: str, msg: dict[str, Any]
    ) -> bool:
        """Send message directly to a specific target user.

        Ensures message isolation: the message is routed exclusively to
        the recipient's stream.

        Args:
            sender: Sender username.
            target_username: Target username.
            msg: Message dict.

        Returns:
            ``True`` if recipient is online and delivered, ``False`` otherwise.
        """
        target_username = target_username.strip().lower()

        with self._sessions_lock:
            conn_id = self._user_to_conn.get(target_username)
            if conn_id is None:
                return False

            session = self._sessions.get(conn_id)
            if (
                session is None
                or not session.authenticated
                or session.username != target_username
                or session.connection.closed
            ):
                return False

            try:
                data: bytes = json.dumps(msg, ensure_ascii=False).encode("utf-8")
                stream = session.connection.get_or_create_stream(0)
                stream.write(data)
                return True
            except Exception as exc:
                logger.error(
                    "Failed to unicast to '%s' (conn_id=%d): %s",
                    target_username,
                    conn_id,
                    exc,
                )
                return False

    # ----------------------- Tick loop -----------------------

    def _tick_loop(self) -> None:
        """Execute tick for all connections in background thread.

        Monitors connection health, triggers keepalive/retransmissions,
        and cleans up dead/timed-out sessions gracefully without server crash.
        """
        while self._running:
            time.sleep(TICK_INTERVAL)
            departed_users: list[str] = []

            with self._sessions_lock:
                active_sessions = list(self._sessions.items())

            closed_sessions: list[tuple[int, Session]] = []
            for conn_id, session in active_sessions:
                try:
                    session.connection.tick()
                except Exception:
                    logger.exception(
                        "Exception in connection tick for conn_id=%d",
                        conn_id,
                    )

                if session.connection.closed:
                    closed_sessions.append((conn_id, session))

            with self._sessions_lock:
                for cid, session in closed_sessions:
                    if self._sessions.get(cid) is not session or not session.connection.closed:
                        continue

                    self._sessions.pop(cid)
                    if self._addr_to_conn.get(session.client_addr) == cid:
                        self._addr_to_conn.pop(session.client_addr, None)

                    if session.username:
                        if self._user_to_conn.get(session.username) == cid:
                            self._user_to_conn.pop(session.username, None)
                        if session.authenticated:
                            departed_users.append(session.username)
                        logger.info(
                            "Session closed: %s (conn_id=%d)",
                            session.username,
                            cid,
                        )

            # Broadcast notifications for departed users outside lock
            for username in departed_users:
                leave_msg = {
                    "type": "SYSTEM",
                    "message": f"User @{username} has left the chat room.",
                }
                self._broadcast("SYSTEM", leave_msg)

            if departed_users:
                self.broadcast_user_list()

    # ----------------------- Cleanup -----------------------

    def _cleanup(self) -> None:
        """Clean up resources on server shutdown."""
        self._running = False
        with self._sessions_lock:
            for s in self._sessions.values():
                s.connection.closed = True
            self._sessions.clear()
            self._addr_to_conn.clear()
            self._user_to_conn.clear()

        if self._sel:
            try:
                self._sel.close()
            except Exception:
                pass
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
        logger.info("Server stopped cleanly.")


def main() -> None:
    """Main entry point to run server."""
    server = ChatServer()
    server.start_server()


if __name__ == "__main__":
    main()
