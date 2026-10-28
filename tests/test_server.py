"""
tests/test_server.py
~~~~~~~~~~~~~~~~~~~~
Unit & Integration tests for Server and Session logic (M3 - Toàn).

Verifies:
1. Non-blocking UDP server initialization.
2. User authentication against database (valid vs invalid).
3. Broadcast message distribution across authenticated sessions.
4. Unicast message routing and privacy (A -> B never leaks to C).
5. Offline unicast error reporting.
6. NAT Rebinding handling (updating client address mapping on port change).
7. Active peer discovery: USER_LIST broadcast on login and disconnect.
8. Concurrent multi-client load (10-20 simultaneous sessions).
9. Graceful client crash and disconnect handling via _tick_loop.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from common import packet
from common.constants import (
    FLAG_EOM,
    FLAG_ENCRYPTED,
    FLAG_NONE,
    GCM_TAG_SIZE,
    PKT_CLOSE,
    PKT_DATA,
)
from common.crypto import encrypt
from server.database import db
from server.server import ChatServer
from server.session import Session


class MockConnection:
    """Mock Connection object for deterministic testing of Session and Server."""

    def __init__(self, conn_id: int, remote_addr: tuple[str, int]) -> None:
        self.conn_id = conn_id
        self.remote_addr = remote_addr
        self.closed = False
        self.written_streams: dict[int, list[bytes]] = {}
        self.ticks = 0

    def get_or_create_stream(self, stream_id: int) -> MockStream:
        return MockStream(self, stream_id)

    def tick(self) -> None:
        self.ticks += 1

    def receive_packet(self, data: bytes) -> None:
        pass


class MockStream:
    """Mock Stream object capturing written data."""

    def __init__(self, connection: MockConnection, stream_id: int) -> None:
        self.connection = connection
        self.stream_id = stream_id

    def write(self, data: bytes) -> None:
        if self.stream_id not in self.connection.written_streams:
            self.connection.written_streams[self.stream_id] = []
        self.connection.written_streams[self.stream_id].append(data)


class TestSessionLogic(unittest.TestCase):
    """Test suite for Session (session.py)."""

    def setUp(self) -> None:
        self.conn = MockConnection(conn_id=101, remote_addr=("127.0.0.1", 50000))
        self.broadcast_log: list[tuple[str, dict, str | None]] = []
        self.unicast_log: list[tuple[str, str, dict]] = []
        self.auth_success_log: list[tuple[int, str]] = []
        self.auth_logout_log: list[tuple[int, str]] = []

        self.session = Session(
            conn_id=101,
            client_addr=("127.0.0.1", 50000),
            connection=self.conn,
            broadcast_fn=lambda s, m, ex: self.broadcast_log.append((s, m, ex)),
            unicast_fn=lambda s, t, m: (self.unicast_log.append((s, t, m)) or True),
            on_auth_success=lambda cid, u: self.auth_success_log.append((cid, u)),
            on_auth_logout=lambda cid, u: self.auth_logout_log.append((cid, u)),
        )

    def test_unauthenticated_msg_rejected(self) -> None:
        """Unauthenticated clients must receive an ERROR payload when sending messages."""
        payload = json.dumps({"type": "MSG", "target": "all", "text": "hello"}).encode("utf-8")
        self.session.process_payload(payload)

        written = self.conn.written_streams.get(0, [])
        self.assertEqual(len(written), 1)
        resp = json.loads(written[0].decode("utf-8"))
        self.assertEqual(resp.get("type"), "ERROR")
        self.assertIn("not logged in", resp.get("message", "").lower())

    def test_non_object_json_is_rejected_without_crashing(self) -> None:
        for payload in (b"[]", b'"message"', b"null", b"42"):
            with self.subTest(payload=payload):
                self.session.process_payload(payload)

        written = self.conn.written_streams.get(0, [])
        self.assertEqual(len(written), 4)
        self.assertTrue(all(json.loads(item)["type"] == "ERROR" for item in written))

    def test_malformed_json_is_rejected(self) -> None:
        self.session.process_payload(b'{"type":')

        response = json.loads(self.conn.written_streams[0][0].decode("utf-8"))
        self.assertEqual(response["type"], "ERROR")
        self.assertEqual(response["message"], "Invalid JSON payload.")

    def test_invalid_chat_fields_are_rejected_without_routing(self) -> None:
        self.session.authenticated = True
        self.session.username = "alice"

        for message in (
            {"type": "MSG", "target": [], "text": "hello"},
            {"type": "MSG", "target": "bob", "text": 123},
            {"type": "MSG", "target": "bob", "text": "x" * 10_001},
        ):
            with self.subTest(message=message):
                self.session.process_payload(json.dumps(message).encode("utf-8"))

        self.assertEqual(self.broadcast_log, [])
        self.assertEqual(self.unicast_log, [])
        self.assertEqual(len(self.conn.written_streams.get(0, [])), 3)

    def test_unauthenticated_file_data_is_rejected(self) -> None:
        payload = json.dumps({
            "type": "FILE_DATA",
            "target": "all",
            "data": "private file bytes",
        }).encode("utf-8")
        self.session.process_payload(payload)

        self.assertEqual(self.broadcast_log, [])
        self.assertEqual(self.unicast_log, [])
        response = json.loads(self.conn.written_streams[0][0].decode("utf-8"))
        self.assertEqual(response["type"], "ERROR")
        self.assertIn("not logged in", response["message"].lower())

    def test_file_chunk_metadata_is_validated_before_routing(self) -> None:
        self.session.authenticated = True
        self.session.username = "alice"

        invalid_chunks = (
            {"stream_id": True, "offset": 0, "data": "YQ=="},
            {"stream_id": 1, "offset": -1, "data": "YQ=="},
            {"stream_id": 1, "offset": 0, "data": ""},
            {"stream_id": 1, "offset": 0, "data": "x" * (48 * 1024 + 1)},
        )
        for chunk in invalid_chunks:
            with self.subTest(chunk_fields=chunk.keys()):
                message = {"type": "FILE_DATA", "target": "bob", **chunk}
                self.session.process_payload(json.dumps(message).encode("utf-8"))

        self.assertEqual(self.broadcast_log, [])
        self.assertEqual(self.unicast_log, [])
        self.assertEqual(len(self.conn.written_streams[0]), len(invalid_chunks))

    def test_direct_routes_reject_unauthenticated_session(self) -> None:
        self.session.route_broadcast({"type": "MSG", "text": "not allowed"})
        self.session.route_unicast("bob", {"type": "MSG", "text": "not allowed"})

        self.assertEqual(self.broadcast_log, [])
        self.assertEqual(self.unicast_log, [])
        responses = [json.loads(item)["type"] for item in self.conn.written_streams[0]]
        self.assertEqual(responses, ["ERROR", "ERROR"])

    def test_invalid_file_metadata_is_rejected_without_routing(self) -> None:
        self.session.authenticated = True
        self.session.username = "alice"
        payload = json.dumps({
            "type": "FILE",
            "target": "all",
            "filename": "../secret.txt",
            "size": 10,
            "sha256": "a" * 64,
            "stream_id": 1,
        }).encode("utf-8")
        self.session.process_payload(payload)

        self.assertEqual(self.broadcast_log, [])
        self.assertEqual(self.unicast_log, [])
        response = json.loads(self.conn.written_streams[0][0].decode("utf-8"))
        self.assertEqual(response["type"], "ERROR")
        self.assertIn("filename", response["message"].lower())

    def test_valid_file_metadata_is_routed(self) -> None:
        self.session.authenticated = True
        self.session.username = "alice"
        payload = json.dumps({
            "type": "FILE",
            "target": "bob",
            "filename": "report.pdf",
            "size": 1024,
            "sha256": "A" * 64,
            "stream_id": 7,
        }).encode("utf-8")
        self.session.process_payload(payload)

        self.assertEqual(len(self.unicast_log), 1)
        sender, target, routed_msg = self.unicast_log[0]
        self.assertEqual((sender, target), ("alice", "bob"))
        self.assertEqual(routed_msg["filename"], "report.pdf")
        self.assertEqual(routed_msg["size"], 1024)
        self.assertEqual(routed_msg["sha256"], "a" * 64)
        self.assertEqual(routed_msg["stream_id"], 7)

    def test_offline_file_recipient_returns_clear_error(self) -> None:
        self.session.authenticated = True
        self.session.username = "alice"
        self.session._unicast_fn = lambda sender, target, msg: False
        self.session.process_payload(json.dumps({
            "type": "FILE",
            "target": "nobody",
            "filename": "report.pdf",
            "size": 1,
            "sha256": "a" * 64,
            "stream_id": 7,
        }).encode("utf-8"))

        response = json.loads(self.conn.written_streams[0][0].decode("utf-8"))
        self.assertEqual(response["type"], "ERROR")
        self.assertIn("offline or does not exist", response["message"])

    def test_successful_auth(self) -> None:
        """Valid credentials authenticate user, notify via AUTH_RESP, and trigger callbacks."""
        valid_hash = hashlib.sha256(b"password123").hexdigest()
        payload = json.dumps({
            "type": "AUTH",
            "username": "alice",
            "password_hash": valid_hash,
        }).encode("utf-8")

        self.session.process_payload(payload)

        self.assertTrue(self.session.authenticated)
        self.assertEqual(self.session.username, "alice")

        # Verify AUTH_RESP was written to stream 0
        written = self.conn.written_streams.get(0, [])
        self.assertEqual(len(written), 1)
        resp = json.loads(written[0].decode("utf-8"))
        self.assertEqual(resp.get("type"), "AUTH_RESP")
        self.assertTrue(resp.get("success"))

        # Verify broadcast join message was triggered
        self.assertEqual(len(self.broadcast_log), 1)
        self.assertEqual(self.broadcast_log[0][1].get("type"), "SYSTEM")

        # Verify auth success callback was fired
        self.assertEqual(len(self.auth_success_log), 1)
        self.assertEqual(self.auth_success_log[0], (101, "alice"))

    def test_auth_matches_client_sha256_and_normalizes_case(self) -> None:
        password_hash = hashlib.sha256(b"password123").hexdigest().upper()
        payload = json.dumps({
            "type": "AUTH",
            "username": "ALICE",
            "password_hash": password_hash,
        }).encode("utf-8")
        self.session.process_payload(payload)

        self.assertTrue(self.session.authenticated)
        self.assertEqual(self.session.username, "alice")

    def test_repeated_auth_on_same_session_is_idempotent(self) -> None:
        password_hash = hashlib.sha256(b"password123").hexdigest()
        payload = json.dumps({
            "type": "AUTH",
            "username": "alice",
            "password_hash": password_hash,
        }).encode("utf-8")
        self.session.process_payload(payload)
        self.session.process_payload(payload)

        self.assertEqual(self.auth_success_log, [(101, "alice")])
        self.assertEqual(len(self.broadcast_log), 1)
        responses = [json.loads(item)["type"] for item in self.conn.written_streams[0]]
        self.assertEqual(responses, ["AUTH_RESP", "AUTH_RESP"])

    def test_authenticated_session_cannot_switch_accounts_without_logout(self) -> None:
        self.session.authenticated = True
        self.session.username = "alice"
        payload = json.dumps({
            "type": "AUTH",
            "username": "bob",
            "password_hash": hashlib.sha256(b"secret456").hexdigest(),
        }).encode("utf-8")
        self.session.process_payload(payload)

        self.assertTrue(self.session.authenticated)
        self.assertEqual(self.session.username, "alice")
        response = json.loads(self.conn.written_streams[0][0].decode("utf-8"))
        self.assertEqual(response["type"], "ERROR")
        self.assertIn("log out", response["message"].lower())

    def test_logout_clears_session_and_notifies_server(self) -> None:
        self.session.authenticated = True
        self.session.username = "alice"
        self.session.process_payload(b'{"type":"LOGOUT"}')

        self.assertFalse(self.session.authenticated)
        self.assertIsNone(self.session.username)
        self.assertEqual(self.auth_logout_log, [(101, "alice")])
        response = json.loads(self.conn.written_streams[0][0].decode("utf-8"))
        self.assertEqual(response["type"], "LOGOUT_RESP")
        self.assertTrue(response["success"])

    def test_invalid_auth(self) -> None:
        """Wrong credentials must fail authentication and return success=False."""
        payload = json.dumps({
            "type": "AUTH",
            "username": "alice",
            "password_hash": "wrongpasswordhash",
        }).encode("utf-8")

        self.session.process_payload(payload)

        self.assertFalse(self.session.authenticated)
        self.assertIsNone(self.session.username)

        written = self.conn.written_streams.get(0, [])
        self.assertEqual(len(written), 1)
        resp = json.loads(written[0].decode("utf-8"))
        self.assertFalse(resp.get("success"))

    def test_authenticated_broadcast_routing(self) -> None:
        """Authenticated chat message to 'all' routes via broadcast_fn."""
        self.session.authenticated = True
        self.session.username = "alice"

        payload = json.dumps({
            "type": "MSG",
            "target": "all",
            "text": "Hello world!",
        }).encode("utf-8")
        self.session.process_payload(payload)

        self.assertEqual(len(self.broadcast_log), 1)
        sender, msg, exclude = self.broadcast_log[0]
        self.assertEqual(sender, "alice")
        self.assertEqual(msg.get("text"), "Hello world!")
        self.assertEqual(exclude, "alice")

    def test_authenticated_unicast_routing(self) -> None:
        """Authenticated chat message to a specific user routes via unicast_fn."""
        self.session.authenticated = True
        self.session.username = "alice"

        payload = json.dumps({
            "type": "MSG",
            "target": "bob",
            "text": "Secret note",
        }).encode("utf-8")
        self.session.process_payload(payload)

        self.assertEqual(len(self.unicast_log), 1)
        sender, target, msg = self.unicast_log[0]
        self.assertEqual(sender, "alice")
        self.assertEqual(target, "bob")
        self.assertEqual(msg.get("text"), "Secret note")

    def test_unicast_offline_user_returns_error(self) -> None:
        """Unicasting to an offline user returns an ERROR message back to sender."""
        self.session.authenticated = True
        self.session.username = "alice"

        # Override unicast_fn to simulate offline user
        self.session._unicast_fn = lambda s, t, m: False

        payload = json.dumps({
            "type": "MSG",
            "target": "offline_user",
            "text": "Are you there?",
        }).encode("utf-8")
        self.session.process_payload(payload)

        written = self.conn.written_streams.get(0, [])
        self.assertEqual(len(written), 1)
        resp = json.loads(written[0].decode("utf-8"))
        self.assertEqual(resp.get("type"), "ERROR")
        self.assertIn("offline", resp.get("message", "").lower())


class TestChatServerLogic(unittest.TestCase):
    """Test suite for ChatServer (server.py)."""

    def setUp(self) -> None:
        self.server = ChatServer()

    def tearDown(self) -> None:
        self.server._cleanup()

    def test_broadcast_sends_without_holding_sessions_lock(self) -> None:
        conn = MockConnection(1, ("127.0.0.1", 5001))
        session = Session(1, conn.remote_addr, conn, self.server._broadcast, self.server._unicast)
        session.authenticated = True
        session.username = "alice"

        lock_available_during_send: list[bool] = []

        class LockCheckingStream:
            def write(inner_self, data: bytes) -> None:
                acquired = self.server._sessions_lock.acquire(blocking=False)
                lock_available_during_send.append(acquired)
                if acquired:
                    self.server._sessions_lock.release()

        stream = LockCheckingStream()
        conn.get_or_create_stream = lambda stream_id: stream
        with self.server._sessions_lock:
            self.server._sessions[1] = session

        self.server._broadcast("SYSTEM", {"type": "SYSTEM", "message": "test"})

        self.assertEqual(lock_available_during_send, [True])

    def test_unicast_privacy_isolation(self) -> None:
        """Crucial requirement: Alice sends to Bob; Charlie MUST NEVER receive it."""
        # Setup 3 sessions
        conn_alice = MockConnection(1, ("127.0.0.1", 5001))
        conn_bob = MockConnection(2, ("127.0.0.1", 5002))
        conn_charlie = MockConnection(3, ("127.0.0.1", 5003))

        sess_alice = Session(1, ("127.0.0.1", 5001), conn_alice, self.server._broadcast, self.server._unicast)
        sess_alice.authenticated = True
        sess_alice.username = "alice"

        sess_bob = Session(2, ("127.0.0.1", 5002), conn_bob, self.server._broadcast, self.server._unicast)
        sess_bob.authenticated = True
        sess_bob.username = "bob"

        sess_charlie = Session(3, ("127.0.0.1", 5003), conn_charlie, self.server._broadcast, self.server._unicast)
        sess_charlie.authenticated = True
        sess_charlie.username = "charlie"

        with self.server._sessions_lock:
            self.server._sessions[1] = sess_alice
            self.server._sessions[2] = sess_bob
            self.server._sessions[3] = sess_charlie
            self.server._user_to_conn["alice"] = 1
            self.server._user_to_conn["bob"] = 2
            self.server._user_to_conn["charlie"] = 3

        # Alice sends private message to Bob
        secret_msg = {"type": "MSG", "from": "alice", "text": "Top Secret for Bob"}
        delivered = self.server._unicast("alice", "bob", secret_msg)

        self.assertTrue(delivered)

        # Bob must have received the message
        bob_received = conn_bob.written_streams.get(0, [])
        self.assertEqual(len(bob_received), 1)
        self.assertIn(b"Top Secret for Bob", bob_received[0])

        # Charlie MUST NOT have received anything!
        charlie_received = conn_charlie.written_streams.get(0, [])
        self.assertEqual(len(charlie_received), 0)

        # Alice herself also receives nothing in stream 0
        alice_received = conn_alice.written_streams.get(0, [])
        self.assertEqual(len(alice_received), 0)

    def test_unicast_rejects_stale_username_mapping(self) -> None:
        conn = MockConnection(2, ("127.0.0.1", 5002))
        session = Session(2, conn.remote_addr, conn)
        session.authenticated = True
        session.username = "charlie"
        with self.server._sessions_lock:
            self.server._sessions[2] = session
            self.server._user_to_conn["bob"] = 2

        delivered = self.server._unicast(
            "alice",
            "bob",
            {"type": "MSG", "text": "should not reach charlie"},
        )

        self.assertFalse(delivered)
        self.assertEqual(conn.written_streams.get(0, []), [])

    def test_duplicate_login_kicks_previous_session(self) -> None:
        old_conn = MockConnection(1, ("127.0.0.1", 5001))
        new_conn = MockConnection(2, ("127.0.0.1", 5002))
        old_session = Session(1, old_conn.remote_addr, old_conn)
        old_session.authenticated = True
        old_session.username = "alice"
        new_session = Session(2, new_conn.remote_addr, new_conn)
        new_session.authenticated = True
        new_session.username = "alice"

        with self.server._sessions_lock:
            self.server._sessions[1] = old_session
            self.server._sessions[2] = new_session
            self.server._addr_to_conn[old_session.client_addr] = 1
            self.server._addr_to_conn[new_session.client_addr] = 2
            self.server._user_to_conn["alice"] = 1

        self.server._on_auth_success(2, "alice")

        self.assertTrue(old_conn.closed)
        self.assertFalse(old_session.authenticated)
        self.assertIsNone(old_session.username)
        self.assertEqual(self.server._user_to_conn["alice"], 2)
        self.assertEqual(self.server.get_online_users(), ["alice"])

    def test_logout_cleans_user_mapping_and_updates_online_users(self) -> None:
        conn = MockConnection(1, ("127.0.0.1", 5001))
        session = Session(
            1,
            conn.remote_addr,
            conn,
            broadcast_fn=self.server._broadcast,
            on_auth_logout=self.server._on_auth_logout,
        )
        session.authenticated = True
        session.username = "alice"
        with self.server._sessions_lock:
            self.server._sessions[1] = session
            self.server._addr_to_conn[conn.remote_addr] = 1
            self.server._user_to_conn["alice"] = 1

        session.process_payload(b'{"type":"LOGOUT"}')

        self.assertNotIn("alice", self.server._user_to_conn)
        self.assertEqual(self.server.get_online_users(), [])
        self.assertIn(1, self.server._sessions)
        self.assertIn(conn.remote_addr, self.server._addr_to_conn)

    def test_broadcast_reaches_all_peers_except_sender(self) -> None:
        """Broadcast reaches all authenticated clients except the sender."""
        conn_alice = MockConnection(1, ("127.0.0.1", 5001))
        conn_bob = MockConnection(2, ("127.0.0.1", 5002))
        conn_charlie = MockConnection(3, ("127.0.0.1", 5003))

        sess_alice = Session(1, ("127.0.0.1", 5001), conn_alice, self.server._broadcast, self.server._unicast)
        sess_alice.authenticated = True
        sess_alice.username = "alice"

        sess_bob = Session(2, ("127.0.0.1", 5002), conn_bob, self.server._broadcast, self.server._unicast)
        sess_bob.authenticated = True
        sess_bob.username = "bob"

        sess_charlie = Session(3, ("127.0.0.1", 5003), conn_charlie, self.server._broadcast, self.server._unicast)
        sess_charlie.authenticated = True
        sess_charlie.username = "charlie"

        with self.server._sessions_lock:
            self.server._sessions[1] = sess_alice
            self.server._sessions[2] = sess_bob
            self.server._sessions[3] = sess_charlie

        bcast_msg = {"type": "MSG", "from": "alice", "text": "Hello Everyone"}
        self.server._broadcast("alice", bcast_msg, exclude="alice")

        # Bob and Charlie receive; Alice does not
        self.assertEqual(len(conn_bob.written_streams.get(0, [])), 1)
        self.assertEqual(len(conn_charlie.written_streams.get(0, [])), 1)
        self.assertEqual(len(conn_alice.written_streams.get(0, [])), 0)

    def test_file_metadata_and_chunks_broadcast_only_to_authenticated_peers(self) -> None:
        connections = [
            MockConnection(idx, ("127.0.0.1", 5100 + idx))
            for idx in range(1, 4)
        ]
        sessions = [
            Session(conn.conn_id, conn.remote_addr, conn, self.server._broadcast, self.server._unicast)
            for conn in connections
        ]
        for session, username in zip(sessions, ("alice", "bob", "charlie")):
            session.username = username
            session.authenticated = username != "charlie"
        with self.server._sessions_lock:
            self.server._sessions.update(
                (session.conn_id, session) for session in sessions
            )
            self.server._user_to_conn.update({"alice": 1, "bob": 2})

        sender = sessions[0]
        sender.process_payload(json.dumps({
            "type": "FILE",
            "target": "all",
            "filename": "report.pdf",
            "size": 4,
            "sha256": "a" * 64,
            "stream_id": 9,
        }).encode("utf-8"))
        sender.process_payload(json.dumps({
            "type": "FILE_DATA",
            "target": "all",
            "stream_id": 9,
            "offset": 0,
            "data": "ZGF0YQ==",
        }).encode("utf-8"))

        alice_messages = connections[0].written_streams.get(0, [])
        bob_messages = connections[1].written_streams.get(0, [])
        charlie_messages = connections[2].written_streams.get(0, [])
        self.assertEqual(alice_messages, [])
        self.assertEqual(len(bob_messages), 2)
        self.assertEqual(charlie_messages, [])
        self.assertEqual(
            [json.loads(message)["type"] for message in bob_messages],
            ["FILE_READY", "FILE_DATA"],
        )

    def test_file_metadata_and_chunks_unicast_to_only_named_recipient(self) -> None:
        connections = [
            MockConnection(idx, ("127.0.0.1", 5200 + idx))
            for idx in range(1, 4)
        ]
        sessions = [
            Session(conn.conn_id, conn.remote_addr, conn, self.server._broadcast, self.server._unicast)
            for conn in connections
        ]
        for session, username in zip(sessions, ("alice", "bob", "charlie")):
            session.username = username
            session.authenticated = True
        with self.server._sessions_lock:
            self.server._sessions.update(
                (session.conn_id, session) for session in sessions
            )
            self.server._user_to_conn.update({"alice": 1, "bob": 2, "charlie": 3})

        sender = sessions[0]
        sender.process_payload(json.dumps({
            "type": "FILE",
            "target": "BOB",
            "filename": "report.pdf",
            "size": 4,
            "sha256": "a" * 64,
            "stream_id": 9,
        }).encode("utf-8"))
        sender.process_payload(json.dumps({
            "type": "FILE_DATA",
            "target": "bob",
            "stream_id": 9,
            "offset": 0,
            "data": "ZGF0YQ==",
        }).encode("utf-8"))

        self.assertEqual(connections[0].written_streams.get(0, []), [])
        self.assertEqual(len(connections[1].written_streams[0]), 2)
        self.assertEqual(connections[2].written_streams.get(0, []), [])
        self.assertEqual(
            [json.loads(message)["type"] for message in connections[1].written_streams[0]],
            ["FILE_READY", "FILE_DATA"],
        )

    def test_raw_file_stream_chunks_follow_metadata_routing(self) -> None:
        connections = [
            MockConnection(idx, ("127.0.0.1", 5300 + idx))
            for idx in range(1, 4)
        ]
        sessions = [
            Session(
                conn.conn_id,
                conn.remote_addr,
                conn,
                broadcast_fn=self.server._broadcast,
                unicast_fn=self.server._unicast,
                file_chunk_fn=self.server._route_file_chunk,
            )
            for conn in connections
        ]
        for session, username in zip(sessions, ("alice", "bob", "charlie")):
            session.username = username
            session.authenticated = username != "charlie"
        with self.server._sessions_lock:
            self.server._sessions.update(
                (session.conn_id, session) for session in sessions
            )
            self.server._user_to_conn.update({"alice": 1, "bob": 2})

        sender = sessions[0]
        sender.process_payload(json.dumps({
            "type": "FILE",
            "target": "all",
            "filename": "report.pdf",
            "size": 4,
            "sha256": "a" * 64,
            "stream_id": 9,
        }).encode("utf-8"))
        self.server._on_app_message(1, 9, b"data")

        self.assertEqual(connections[0].written_streams.get(9, []), [])
        self.assertEqual(connections[1].written_streams[9], [b"data"])
        self.assertEqual(connections[2].written_streams.get(9, []), [])

    def test_raw_file_stream_chunk_unicasts_only_to_named_user(self) -> None:
        connections = [
            MockConnection(idx, ("127.0.0.1", 5350 + idx))
            for idx in range(1, 4)
        ]
        sessions = [
            Session(
                conn.conn_id,
                conn.remote_addr,
                conn,
                broadcast_fn=self.server._broadcast,
                unicast_fn=self.server._unicast,
                file_chunk_fn=self.server._route_file_chunk,
            )
            for conn in connections
        ]
        for session, username in zip(sessions, ("alice", "bob", "charlie")):
            session.username = username
            session.authenticated = True
        with self.server._sessions_lock:
            self.server._sessions.update(
                (session.conn_id, session) for session in sessions
            )
            self.server._user_to_conn.update(
                {"alice": 1, "bob": 2, "charlie": 3}
            )

        sessions[0].process_payload(json.dumps({
            "type": "FILE",
            "target": "bob",
            "filename": "report.pdf",
            "size": 4,
            "sha256": "a" * 64,
            "stream_id": 11,
        }).encode("utf-8"))
        self.server._on_app_message(1, 11, b"data")

        self.assertEqual(connections[0].written_streams.get(11, []), [])
        self.assertEqual(connections[1].written_streams[11], [b"data"])
        self.assertEqual(connections[2].written_streams.get(11, []), [])

    def test_raw_file_chunks_require_metadata_and_declared_size(self) -> None:
        conn = MockConnection(1, ("127.0.0.1", 5401))
        session = Session(
            1,
            conn.remote_addr,
            conn,
            file_chunk_fn=self.server._route_file_chunk,
        )
        session.authenticated = True
        session.username = "alice"
        with self.server._sessions_lock:
            self.server._sessions[1] = session

        self.server._on_app_message(1, 9, b"no metadata")
        self.assertEqual(conn.written_streams.get(9, []), [])

        session.process_payload(json.dumps({
            "type": "FILE",
            "target": "all",
            "filename": "tiny.bin",
            "size": 2,
            "sha256": "a" * 64,
            "stream_id": 9,
        }).encode("utf-8"))
        self.server._on_app_message(1, 9, b"too large")

        self.assertEqual(conn.written_streams.get(9, []), [])
        errors = [
            json.loads(payload)["type"]
            for payload in conn.written_streams.get(0, [])
        ]
        self.assertIn("ERROR", errors)

    def test_nat_rebinding_support(self) -> None:
        """A valid encrypted DATA packet proves ownership before address migration."""
        conn_id = 999
        old_addr = ("127.0.0.1", 40001)
        new_addr = ("127.0.0.1", 40002)
        session_key = b"k" * 32

        raw_packet_old = packet.pack(
            msg_type=PKT_DATA,
            conn_id=conn_id,
            seq_num=1,
            flags=FLAG_NONE,
            stream_id=0,
            offset=0,
            payload=b"packet_from_old_port",
        )

        # Receive from old port
        self.server.handle_datagram(raw_packet_old, old_addr)
        self.assertIn(conn_id, self.server._sessions)
        session = self.server._sessions[conn_id]
        self.assertEqual(session.client_addr, old_addr)
        self.assertEqual(self.server._addr_to_conn[old_addr], conn_id)
        session.connection._session_key = session_key

        raw_packet_new = self._encrypted_client_packet(
            conn_id, 2, b"packet_from_new_port", session_key
        )
        self.server.handle_datagram(raw_packet_new, new_addr)

        # Valid AEAD proof moves both address references and the send destination.
        self.assertEqual(session.client_addr, new_addr)
        self.assertEqual(session.connection.remote_addr, new_addr)
        self.assertNotIn(old_addr, self.server._addr_to_conn)
        self.assertEqual(self.server._addr_to_conn[new_addr], conn_id)

    def test_spoofed_nat_rebinding_does_not_change_address_mapping(self) -> None:
        conn_id = 998
        old_addr = ("127.0.0.1", 41001)
        attacker_addr = ("127.0.0.1", 41002)
        session_key = b"s" * 32

        initial = packet.pack(
            msg_type=PKT_DATA,
            conn_id=conn_id,
            seq_num=1,
            flags=FLAG_NONE,
            stream_id=0,
            offset=0,
            payload=b"initial packet",
        )
        self.server.handle_datagram(initial, old_addr)
        session = self.server._sessions[conn_id]
        session.connection._session_key = session_key

        forged = self._encrypted_client_packet(
            conn_id, 2, b"forged packet", b"a" * 32
        )
        self.server.handle_datagram(forged, attacker_addr)

        self.assertEqual(session.client_addr, old_addr)
        self.assertEqual(session.connection.remote_addr, old_addr)
        self.assertEqual(self.server._addr_to_conn[old_addr], conn_id)
        self.assertNotIn(attacker_addr, self.server._addr_to_conn)

    def test_nat_rebinding_cannot_claim_another_sessions_address(self) -> None:
        conn_id = 997
        old_addr = ("127.0.0.1", 42001)
        occupied_addr = ("127.0.0.1", 42002)
        session_key = b"t" * 32
        initial = packet.pack(
            msg_type=PKT_DATA,
            conn_id=conn_id,
            seq_num=1,
            flags=FLAG_NONE,
            stream_id=0,
            offset=0,
            payload=b"initial packet",
        )
        self.server.handle_datagram(initial, old_addr)
        session = self.server._sessions[conn_id]
        session.connection._session_key = session_key
        self.server._addr_to_conn[occupied_addr] = 1234

        valid = self._encrypted_client_packet(conn_id, 2, b"valid packet", session_key)
        self.server.handle_datagram(valid, occupied_addr)

        self.assertEqual(session.client_addr, old_addr)
        self.assertEqual(session.connection.remote_addr, old_addr)
        self.assertEqual(self.server._addr_to_conn[old_addr], conn_id)
        self.assertEqual(self.server._addr_to_conn[occupied_addr], 1234)

    @staticmethod
    def _encrypted_client_packet(
        conn_id: int,
        seq_num: int,
        payload: bytes,
        key: bytes,
    ) -> bytes:
        flags = FLAG_ENCRYPTED | FLAG_EOM
        header_aad = packet.pack(
            msg_type=PKT_DATA,
            conn_id=conn_id,
            seq_num=seq_num,
            flags=flags,
            stream_id=0,
            offset=0,
            payload=b"\x00" * (len(payload) + GCM_TAG_SIZE),
        )[:22]
        ciphertext = encrypt(
            payload,
            key,
            seq_num,
            associated_data=header_aad,
            is_server=False,
        )
        return packet.pack(
            msg_type=PKT_DATA,
            conn_id=conn_id,
            seq_num=seq_num,
            flags=flags,
            stream_id=0,
            offset=0,
            payload=ciphertext,
        )

    def test_concurrent_sessions_scalability(self) -> None:
        """Scalability requirement: Server handles 20 concurrent sessions simultaneously."""
        num_clients = 20

        # Simulate 20 concurrent clients sending datagrams in parallel threads
        def send_client_datagram(client_idx: int) -> None:
            conn_id = 1000 + client_idx
            addr = ("127.0.0.1", 6000 + client_idx)
            raw = packet.pack(
                msg_type=PKT_DATA,
                conn_id=conn_id,
                seq_num=1,
                flags=FLAG_NONE,
                stream_id=0,
                offset=0,
                payload=f"hello from client {client_idx}".encode("utf-8"),
            )
            self.server.handle_datagram(raw, addr)

        threads = [
            threading.Thread(target=send_client_datagram, args=(i,))
            for i in range(num_clients)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All 20 sessions must be registered cleanly
        with self.server._sessions_lock:
            self.assertEqual(len(self.server._sessions), num_clients)
            self.assertEqual(len(self.server._addr_to_conn), num_clients)

    def test_tick_loop_cleans_up_closed_session_and_broadcasts_departure(self) -> None:
        """Closed sessions are evicted in tick loop and trigger USER_LIST/SYSTEM broadcast."""
        conn_alice = MockConnection(1, ("127.0.0.1", 5001))
        conn_bob = MockConnection(2, ("127.0.0.1", 5002))

        sess_alice = Session(1, ("127.0.0.1", 5001), conn_alice, self.server._broadcast, self.server._unicast)
        sess_alice.authenticated = True
        sess_alice.username = "alice"

        sess_bob = Session(2, ("127.0.0.1", 5002), conn_bob, self.server._broadcast, self.server._unicast)
        sess_bob.authenticated = True
        sess_bob.username = "bob"

        with self.server._sessions_lock:
            self.server._sessions[1] = sess_alice
            self.server._sessions[2] = sess_bob
            self.server._user_to_conn["alice"] = 1
            self.server._user_to_conn["bob"] = 2
            self.server._addr_to_conn[sess_alice.client_addr] = 1
            self.server._addr_to_conn[sess_bob.client_addr] = 2

        # Bob disconnects / crashes
        conn_bob.closed = True

        # Run single tick iteration
        self.server._running = True
        # Emulate one tick execution
        t = threading.Thread(target=self.server._tick_loop, daemon=True)
        t.start()
        time.sleep(0.15)
        self.server._running = False
        t.join(timeout=0.5)

        # Bob must be evicted
        with self.server._sessions_lock:
            self.assertNotIn(2, self.server._sessions)
            self.assertNotIn("bob", self.server._user_to_conn)
            self.assertNotIn(("127.0.0.1", 5002), self.server._addr_to_conn)
            # Alice still active
            self.assertIn(1, self.server._sessions)

        # Alice should have received leave notification and updated user list
        alice_received = conn_alice.written_streams.get(0, [])
        self.assertTrue(len(alice_received) >= 1)
        received_types = [json.loads(p.decode("utf-8")).get("type") for p in alice_received]
        self.assertIn("SYSTEM", received_types)
        self.assertIn("USER_LIST", received_types)

    def test_tick_loop_logs_connection_error_and_continues(self) -> None:
        class FailingMockConnection(MockConnection):
            def tick(self) -> None:
                raise RuntimeError("simulated tick failure")

        failing_conn = FailingMockConnection(1, ("127.0.0.1", 5001))
        healthy_conn = MockConnection(2, ("127.0.0.1", 5002))
        failing_session = Session(1, failing_conn.remote_addr, failing_conn)
        healthy_session = Session(2, healthy_conn.remote_addr, healthy_conn)

        with self.server._sessions_lock:
            self.server._sessions[1] = failing_session
            self.server._sessions[2] = healthy_session

        self.server._running = True
        with patch(
            "server.server.time.sleep",
            side_effect=lambda _: setattr(self.server, "_running", False),
        ), self.assertLogs("server.server", level="ERROR") as logs:
            self.server._tick_loop()

        self.assertEqual(healthy_conn.ticks, 1)
        self.assertIn("conn_id=1", logs.output[0])
        with self.server._sessions_lock:
            self.assertIn(1, self.server._sessions)
            self.assertIn(2, self.server._sessions)


if __name__ == "__main__":
    unittest.main()
