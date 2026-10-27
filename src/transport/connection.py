"""
transport/connection.py
~~~~~~~~~~~~~~~~~~~~~~~
Manages the lifecycle of a logical UDP connection (simulating a
stateful connection on top of stateless UDP).

Includes: handshake, encryption, packet sending/receiving, heartbeat, and
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
    """Logical UDP connection between two endpoints.

    Manages handshake (key exchange), encryption/decryption, sending/receiving
    packets, heartbeat keepalive, and retransmission.
    """

    def __init__(
        self,
        sock: socket.socket,
        remote_addr: tuple[str, int],
        conn_id: int = 0,
        is_server: bool = False,
        on_app_message: Optional[Callable[[int, bytes], None]] = None,
    ) -> None:
        """Initialize Connection.

        Args:
            sock: Pre-bound UDP socket.
            remote_addr: Tuple ``(host, port)`` of remote peer.
            conn_id: Connection identifier (uint32). 0 = unassigned.
            is_server: ``True`` if server side.
            on_app_message: Callback ``(stream_id, data)`` when receiving
                            a complete message from a Stream.
        """
        self.sock: socket.socket = sock
        self.remote_addr: tuple[str, int] = remote_addr
        self.conn_id: int = conn_id if conn_id else random.randint(1, 0xFFFFFFFF)
        self.is_server: bool = is_server
        self.on_app_message: Optional[Callable[[int, bytes], None]] = on_app_message

        # Crypto
        self._private_key: Optional[X25519PrivateKey] = None
        self._public_bytes: Optional[bytes] = None
        self._session_key: Optional[bytes] = None
        self.handshake_complete: threading.Event = threading.Event()

        # Sequence numbers
        self._next_seq: int = 0
        self._seq_lock: threading.Lock = threading.Lock()

        # Streams
        self._streams: dict[int, Stream] = {}
        self._streams_lock: threading.Lock = threading.Lock()
        self._next_stream_id: int = 1 if not is_server else 2  # odd/even

        # Reliability
        self.reliability: ReliabilityManager = ReliabilityManager()

        # Timing
        self._last_recv_time: float = time.monotonic()
        self._last_heartbeat: float = 0.0

        # Status
        self.closed: bool = False

    # ─────────────────────── Sequence number ───────────────────────

    def _alloc_seq(self) -> int:
        """Allocate a new sequence number (thread-safe).

        Returns:
            Newly allocated sequence number.
        """
        with self._seq_lock:
            seq = self._next_seq
            self._next_seq += 1
            return seq

    # ─────────────────────── Stream management ───────────────────────

    def get_or_create_stream(self, stream_id: int) -> Stream:
        """Get or create a Stream by ID.

        Args:
            stream_id: Stream identifier.

        Returns:
            Corresponding Stream object.
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
        """Create a new Stream with an auto-assigned ID.

        Returns:
            Newly created Stream object.
        """
        with self._streams_lock:
            sid = self._next_stream_id
            self._next_stream_id += 2  # maintain odd/even parity
            stream = Stream(
                stream_id=sid,
                send_callback=self._stream_send_callback,
            )
            self._streams[sid] = stream
            return stream

    def _stream_send_callback(
        self, stream_id: int, offset: int, chunk: bytes, flags: int
    ) -> None:
        """Callback invoked by Stream when a chunk needs to be sent.

        Args:
            stream_id: Stream identifier.
            offset: Byte offset.
            chunk: Chunk data.
            flags: Control flags (e.g. FLAG_EOM).
        """
        self.send_packet(PKT_DATA, stream_id, chunk, flags=flags, offset=offset)

    # ─────────────────────── SEND PACKET ───────────────────────

    def send_packet(
        self,
        packet_type: int,
        stream_id: int,
        payload: bytes,
        flags: int = FLAG_NONE,
        offset: int = 0,
    ) -> None:
        """Create packet, encrypt (if key available), buffer for reliability, and send.

        Args:
            packet_type: Packet type (PKT_DATA, PKT_ACK, ...).
            stream_id: Stream identifier.
            payload: Application data payload.
            flags: Control flags.
            offset: Byte offset within stream.
        """
        seq: int = self._alloc_seq()

        # Encrypt if session key established
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

        # Buffer in reliability manager if ACK required
        if packet_type == PKT_DATA:
            self.reliability.mark_sent(seq, raw)

        self._raw_send(raw)

    def _raw_send(self, data: bytes) -> None:
        """Send raw bytes over UDP socket.

        Args:
            data: Bytes to send.
        """
        try:
            self.sock.sendto(data, self.remote_addr)
        except OSError as exc:
            logger.warning("Failed to send to %s: %s", self.remote_addr, exc)

    # ─────────────────────── RECEIVE PACKET ───────────────────────

    def receive_packet(self, raw_bytes: bytes) -> None:
        """Decode and dispatch incoming packet.

        Args:
            raw_bytes: Raw data received from socket.
        """
        self._last_recv_time = time.monotonic()

        try:
            header, payload = packet.unpack(raw_bytes)
        except ValueError as exc:
            logger.debug("Discarding malformed packet: %s", exc)
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
            logger.debug("Heartbeat ACK received from %s", self.remote_addr)
        else:
            logger.debug("Unknown packet type: 0x%02X", msg_type)

    def _handle_ack(self, payload: bytes) -> None:
        """Handle incoming ACK packet and forward to ReliabilityManager.

        Args:
            payload: JSON containing SACK ranges.
        """
        try:
            ack_info = json.loads(payload.decode("utf-8"))
            ranges: list[tuple[int, int]] = [
                (r[0], r[1]) for r in ack_info.get("ranges", [])
            ]
            self.reliability.process_sack(ranges)
        except (json.JSONDecodeError, UnicodeDecodeError, KeyError) as exc:
            logger.debug("Malformed ACK payload: %s", exc)

    def _handle_data(self, header: packet.Header, payload: bytes) -> None:
        """Handle DATA packet: decrypt, feed to Stream, send ACK.

        Args:
            header: Unpacked header.
            payload: Payload (potentially encrypted).
        """
        # Send immediate ACK
        self._send_ack(header.seq_num)

        # Decrypt if needed
        actual_payload: bytes = payload
        if (header.flags & FLAG_ENCRYPTED) and self._session_key is not None:
            try:
                actual_payload = decrypt(
                    payload, self._session_key, header.seq_num
                )
            except Exception as exc:
                logger.warning("Decryption failed for seq=%d: %s", header.seq_num, exc)
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
        """Send ACK for a given sequence number.

        Args:
            seq_num: Packet sequence number to acknowledge.
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

    # ─────────────────────── HANDSHAKE ───────────────────────

    def handle_handshake(self) -> None:
        """Run HELLO -> HELLO_ACK key exchange flow (Client side).

        Client sends HELLO with public key, waits for HELLO_ACK from Server.
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
        logger.info("Sent HELLO to %s (conn_id=%d)", self.remote_addr, self.conn_id)

    def _handle_hello(self, header: packet.Header, payload: bytes) -> None:
        """Handle HELLO from Client (Server side).

        Args:
            header: HELLO packet header.
            payload: JSON containing client public key.
        """
        try:
            info = json.loads(payload.decode("utf-8"))
            peer_pub_hex: str = info["public_key"]
            peer_pub_bytes: bytes = bytes.fromhex(peer_pub_hex)
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.warning("Malformed HELLO payload: %s", exc)
            return

        # Generate server-side keypair
        self._private_key, self._public_bytes = generate_keypair()
        self._session_key = derive_shared_secret(self._private_key, peer_pub_bytes)

        # Send HELLO_ACK with server public key
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
            "Handshake complete (server side) conn_id=%d", self.conn_id
        )

    def _handle_hello_ack(self, header: packet.Header, payload: bytes) -> None:
        """Handle HELLO_ACK from Server (Client side).

        Args:
            header: HELLO_ACK packet header.
            payload: JSON containing server public key.
        """
        try:
            info = json.loads(payload.decode("utf-8"))
            peer_pub_hex: str = info["public_key"]
            peer_pub_bytes: bytes = bytes.fromhex(peer_pub_hex)
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.warning("Malformed HELLO_ACK payload: %s", exc)
            return

        if self._private_key is None:
            logger.warning("Received HELLO_ACK without sending HELLO")
            return

        self._session_key = derive_shared_secret(self._private_key, peer_pub_bytes)
        self.handshake_complete.set()
        logger.info(
            "Handshake complete (client side) conn_id=%d", self.conn_id
        )

    # ─────────────────────── Heartbeat ───────────────────────

    def _send_heartbeat(self) -> None:
        """Send Heartbeat keepalive packet."""
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
        """Send Heartbeat response ACK."""
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
        """Check timeouts, send heartbeat, and trigger retransmission.

        This method is invoked periodically by the event loop.
        """
        now: float = time.monotonic()

        # Idle timeout
        if now - self._last_recv_time > IDLE_TIMEOUT:
            logger.warning(
                "Connection %d idle for > %ss – marking closed",
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
            logger.debug("Retransmitting packet on conn %d", self.conn_id)
