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
from collections import deque
import random
import socket
import threading
import time
from typing import Optional, Callable

from cryptography.exceptions import InvalidTag
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
    HEADER_SIZE,
    GCM_TAG_SIZE,
)
from common.crypto import (
    generate_keypair,
    derive_session_keys,
    derive_shared_secret,
    encrypt,
    decrypt,
    sign_server_hello,
    verify_server_hello,
)
from transport.reliability import ReliabilityManager
from transport.stream import Stream

logger = logging.getLogger(__name__)

# Resend HELLO if no HELLO_ACK arrives (seconds) and the maximum number of retries (K11)
HELLO_RETRY_INTERVAL: float = 1.0
HELLO_MAX_RETRIES: int = 5


class Connection:
    """Logical UDP connection between two endpoints.

    Manages handshake (key exchange), encryption/decryption, sending/receiving
    packets, heartbeat keepalive, and retransmission.
    """

    # M1<->M2 integration contract version (docs/project/M1_M2_INTEGRATION_CONTRACT.md).
    # tests/test_connection_loopback.py reads this to know the contract is implemented.
    INTEGRATION_CONTRACT_VERSION: int = 1

    def __init__(
        self,
        sock: socket.socket,
        remote_addr: tuple[str, int],
        conn_id: int = 0,
        is_server: bool = False,
        on_app_message: Optional[Callable[[int, bytes], None]] = None,
        server_identity_key: Optional[Any] = None,
        pinned_server_key: Optional[bytes] = None,
    ) -> None:
        """Initialize Connection.

        Args:
            sock: Pre-bound UDP socket.
            remote_addr: Tuple ``(host, port)`` of remote peer.
            conn_id: Connection identifier (uint32). 0 = unassigned.
            is_server: ``True`` if server side.
            on_app_message: Callback ``(stream_id, data)`` when receiving
                            a complete message from a Stream.
            server_identity_key: Server-side Ed25519PrivateKey for signing HELLO_ACK.
            pinned_server_key: Client-side pinned 32-byte Ed25519 public key of server.
        """
        self.sock: socket.socket = sock
        self.remote_addr: tuple[str, int] = remote_addr
        self.conn_id: int = conn_id if conn_id else random.randint(1, 0xFFFFFFFF)
        self.is_server: bool = is_server
        self.on_app_message: Optional[Callable[[int, bytes], None]] = on_app_message
        self.server_identity_key: Optional[Any] = server_identity_key
        self.pinned_server_key: Optional[bytes] = pinned_server_key

        # Crypto
        self._private_key: Optional[X25519PrivateKey] = None
        self._public_bytes: Optional[bytes] = None
        self._session_key: Optional[bytes] = None  # Kept for compatibility: not None means keys exist
        self._send_key: Optional[bytes] = None     # Write key of our own role (K1)
        self._recv_key: Optional[bytes] = None     # Write key of the peer's role (K1)
        self._hello_raw: Optional[bytes] = None    # Client: HELLO kept for retransmission (K11)
        self._hello_ack_raw: Optional[bytes] = None  # Server: HELLO_ACK resent on duplicate HELLO (K11)
        self._hello_sent_time: float = 0.0
        self._hello_retries: int = 0
        self.handshake_complete: threading.Event = threading.Event()

        # Send queue gated by the congestion window (K7)
        self._tx_queue: deque = deque()
        self._tx_lock: threading.RLock = threading.RLock()

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
        if packet_type == PKT_DATA:
            # K7: DATA goes through a queue and only reaches the socket while the
            # congestion window has room. Never blocks the calling thread.
            with self._tx_lock:
                self._tx_queue.append((stream_id, bytes(payload), flags, offset))
            self._flush_send_queue()
            return

        seq: int = self._alloc_seq()
        raw: bytes = packet.pack(
            msg_type=packet_type,
            conn_id=self.conn_id,
            seq_num=seq,
            flags=flags,
            stream_id=stream_id,
            offset=offset,
            payload=payload,
        )
        self._raw_send(raw)

    def _flush_send_queue(self) -> None:
        """Send queued DATA while ``reliability.can_send()`` is True (K7).

        Called after enqueue, after processing an ACK and from ``tick()``. It does not
        block, so it is safe from the receive loop (the server uses one thread for both).
        """
        with self._tx_lock:
            while self._tx_queue and self.reliability.can_send():
                stream_id, payload, flags, offset = self._tx_queue.popleft()
                self._transmit_data(stream_id, payload, flags, offset)

    def _transmit_data(self, stream_id: int, payload: bytes, flags: int, offset: int) -> None:
        """Allocate seq, encrypt (K1-K3), record in reliability and send one DATA packet."""
        seq: int = self._alloc_seq()

        if self._send_key is not None:
            flags |= FLAG_ENCRYPTED
            # Header is the AAD: payload_len must equal the ciphertext length (plaintext + tag)
            header_aad: bytes = packet.pack(
                msg_type=PKT_DATA,
                conn_id=self.conn_id,
                seq_num=seq,
                flags=flags,
                stream_id=stream_id,
                offset=offset,
                payload=bytes(len(payload) + GCM_TAG_SIZE),
            )[:HEADER_SIZE]
            ciphertext: bytes = encrypt(
                payload,
                self._send_key,
                seq,
                associated_data=header_aad,
                is_server=self.is_server,
            )
            raw: bytes = header_aad + ciphertext
        else:
            raw = packet.pack(
                msg_type=PKT_DATA,
                conn_id=self.conn_id,
                seq_num=seq,
                flags=flags,
                stream_id=stream_id,
                offset=offset,
                payload=payload,
            )

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
            self._handle_data(header, payload, raw_bytes[:HEADER_SIZE])
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

    def verify_migration_datagram(self, raw_bytes: bytes) -> bool:
        """Verify an encrypted DATA packet before accepting a new peer address.

        Unauthenticated ACK, heartbeat, or handshake packets are not sufficient
        proof that a datagram from a changed address belongs to this connection.
        """
        # Directional receive key after a handshake; _session_key only if keys were
        # injected without one (legacy/tests).
        verify_key: Optional[bytes] = (
            self._recv_key if self._recv_key is not None else self._session_key
        )
        if verify_key is None:
            return False

        try:
            header, payload = packet.unpack(raw_bytes)
            if (
                header.conn_id != self.conn_id
                or header.msg_type != PKT_DATA
                or not (header.flags & FLAG_ENCRYPTED)
                or header.payload_len != len(raw_bytes) - HEADER_SIZE
            ):
                return False

            decrypt(
                payload,
                verify_key,
                header.seq_num,
                associated_data=raw_bytes[:HEADER_SIZE],
                is_server=not self.is_server,
            )
        except (InvalidTag, TypeError, ValueError) as exc:
            logger.debug(
                "Rejected address migration proof for conn_id=%d: %s",
                self.conn_id,
                exc,
            )
            return False

        return True

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
        except (
            json.JSONDecodeError, UnicodeDecodeError, KeyError,
            ValueError, TypeError, IndexError, AttributeError,
        ) as exc:
            # K8: bad peer data must not raise out of receive_packet
            logger.debug("Malformed ACK payload: %s", exc)
            return

        # Window space was released: push queued DATA (K7)
        self._flush_send_queue()

    def _handle_data(self, header: packet.Header, payload: bytes, header_aad: bytes) -> None:
        """Handle DATA packet: authenticate+decrypt, feed Stream, then ACK, then deliver.

        Args:
            header: Unpacked header.
            payload: Payload (encrypted after the handshake).
            header_aad: The 22 header bytes exactly as received, used as AAD (K3).
        """
        encrypted: bool = bool(header.flags & FLAG_ENCRYPTED)

        if self._recv_key is not None:
            # K4: after the handshake only encrypted DATA is accepted
            if not encrypted:
                logger.warning("Dropping unencrypted DATA seq=%d after handshake", header.seq_num)
                return
            try:
                actual_payload: bytes = decrypt(
                    payload,
                    self._recv_key,
                    header.seq_num,
                    associated_data=header_aad,
                    is_server=not self.is_server,
                )
            except (InvalidTag, ValueError, TypeError) as exc:
                # K5: a packet that fails authentication is not ACKed
                logger.warning("Decryption failed for seq=%d: %s", header.seq_num, exc)
                return
        else:
            if encrypted:
                logger.warning("Encrypted DATA seq=%d but no key yet", header.seq_num)
                return
            actual_payload = payload

        is_eom: bool = bool(header.flags & FLAG_EOM)
        stream = self.get_or_create_stream(header.stream_id)
        try:
            stream.read_incoming(
                offset=header.offset,
                data=actual_payload,
                is_eom=is_eom,
            )
        except ValueError as exc:
            # K9: Stream refused the chunk (overflow, overlap) -> no ACK so the sender retransmits
            logger.warning(
                "Stream %d rejected chunk seq=%d: %s", header.stream_id, header.seq_num, exc
            )
            return

        # K5: ACK only after the packet is authenticated and accepted by the Stream
        self._send_ack(header.seq_num)

        # K6: drain every complete message (several can complete at once on out-of-order arrival)
        for msg in stream.get_complete_messages():
            if self.on_app_message is not None:
                self.on_app_message(header.stream_id, msg)

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
        self._hello_raw = raw
        self._hello_sent_time = time.monotonic()
        self._hello_retries = 0
        self._raw_send(raw)
        logger.info("Sent HELLO to %s (conn_id=%d)", self.remote_addr, self.conn_id)

    def _install_keys(self, peer_pub_bytes: bytes) -> None:
        """Derive directional keys and pick the send/receive key by role (K1)."""
        client_key, server_key = derive_session_keys(self._private_key, peer_pub_bytes)
        self._send_key = server_key if self.is_server else client_key
        self._recv_key = client_key if self.is_server else server_key
        # Shared (non-directional) secret, identical on both sides; kept for compatibility
        # and diagnostics only. Traffic is protected by _send_key / _recv_key.
        self._session_key = derive_shared_secret(self._private_key, peer_pub_bytes)

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

        # K11: duplicate HELLO (HELLO_ACK may have been lost) -> resend the same HELLO_ACK
        if self._hello_ack_raw is not None:
            self._raw_send(self._hello_ack_raw)
            return

        # Generate server-side keypair
        self._private_key, self._public_bytes = generate_keypair()
        try:
            self._install_keys(peer_pub_bytes)
        except (ValueError, TypeError) as exc:
            logger.warning("Invalid HELLO public key: %s", exc)
            return

        # Send HELLO_ACK with server public key, optionally signed by identity key
        ack_data: dict[str, Any] = {
            "public_key": self._public_bytes.hex(),
            "conn_id": self.conn_id,
        }
        if self.server_identity_key is not None:
            signature: bytes = sign_server_hello(
                self.server_identity_key, self.conn_id, self._public_bytes
            )
            ack_data["signature"] = signature.hex()

        ack_payload: bytes = json.dumps(ack_data).encode("utf-8")

        raw: bytes = packet.pack(
            msg_type=PKT_HELLO_ACK,
            conn_id=self.conn_id,
            seq_num=0,
            flags=FLAG_NONE,
            stream_id=0,
            offset=0,
            payload=ack_payload,
        )
        self._hello_ack_raw = raw
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

        # Verify Server Signature if pinning is configured
        if self.pinned_server_key is not None:
            sig_hex = info.get("signature")
            if not sig_hex:
                logger.error(
                    "MITM Alert: Server did not provide signature for pinned key on conn_id=%d",
                    self.conn_id,
                )
                return
            try:
                sig_bytes = bytes.fromhex(sig_hex)
            except ValueError:
                logger.error("MITM Alert: Invalid signature hex on conn_id=%d", self.conn_id)
                return

            if not verify_server_hello(self.pinned_server_key, self.conn_id, peer_pub_bytes, sig_bytes):
                logger.error(
                    "MITM Alert: Server signature verification failed for pinned key on conn_id=%d!",
                    self.conn_id,
                )
                return

        if self._private_key is None:
            logger.warning("Received HELLO_ACK without sending HELLO")
            return

        if self.handshake_complete.is_set():
            return  # K11: duplicate HELLO_ACK, keys already installed

        try:
            self._install_keys(peer_pub_bytes)
        except (ValueError, TypeError) as exc:
            logger.warning("Invalid HELLO_ACK public key: %s", exc)
            return
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

        # K11: client resends HELLO while no HELLO_ACK has arrived
        if (
            self._hello_raw is not None
            and not self.handshake_complete.is_set()
            and self._hello_retries < HELLO_MAX_RETRIES
            and now - self._hello_sent_time >= HELLO_RETRY_INTERVAL
        ):
            self._hello_retries += 1
            self._hello_sent_time = now
            self._raw_send(self._hello_raw)

        # Retransmission
        retransmits: list[bytes] = self.reliability.get_retransmits()
        for raw_pkt in retransmits:
            self._raw_send(raw_pkt)
            logger.debug("Retransmitting packet on conn %d", self.conn_id)

        # K10: a packet exceeded max_retransmits -> the peer is unreachable, close
        failed = self.reliability.take_failed()
        if failed:
            logger.warning(
                "Connection %d: %d packet(s) exceeded max retransmits – marking closed",
                self.conn_id,
                len(failed),
            )
            self.closed = True
            return

        # K7: the window may have opened (RTO or fast retransmit freed packets)
        self._flush_send_queue()
