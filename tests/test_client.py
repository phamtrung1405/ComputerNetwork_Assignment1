"""Tests for client handshake and login behavior."""

from __future__ import annotations

import hashlib
import json
import os
import queue
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from client.client import ChatClient
from client.http_ui import HttpUI


class ChatClientConnectTests(unittest.TestCase):
    def _client_with_handshake_result(self, completed: bool) -> ChatClient:
        client = ChatClient.__new__(ChatClient)
        client._username = None
        client._running = True
        client._connection = SimpleNamespace(
            handshake_complete=Mock(),
        )
        client._connection.handshake_complete.wait.return_value = completed
        client._send_json = Mock()
        return client

    def test_handshake_timeout_does_not_send_auth(self) -> None:
        client = self._client_with_handshake_result(False)

        with self.assertLogs("client.client", level="ERROR") as logs:
            result = client.connect("alice", "password123")

        self.assertFalse(result)
        self.assertIsNone(client._username)
        client._send_json.assert_not_called()
        self.assertIn("refusing to send AUTH", logs.output[0])

    def test_completed_handshake_still_sends_auth(self) -> None:
        client = self._client_with_handshake_result(True)

        result = client.connect("alice", "password123")

        self.assertTrue(result)
        client._send_json.assert_called_once_with({
            "type": "AUTH",
            "username": "alice",
            "password_hash": hashlib.sha256(b"password123").hexdigest(),
        })


class HttpLoginTimeoutTests(unittest.TestCase):
    def test_handshake_timeout_is_reported_without_waiting_for_auth(self) -> None:
        ui = HttpUI.__new__(HttpUI)
        ui._port = 8080
        ui._client = Mock()
        ui._client.is_authenticated = False
        ui._client.connect.return_value = False
        ui._send_json_response = Mock()
        client_socket = object()

        ui._handle_login(client_socket, json.dumps({
            "username": "alice",
            "password": "password123",
        }))

        ui._client.connect.assert_called_once_with("alice", "password123")
        ui._send_json_response.assert_called_once()
        args, _ = ui._send_json_response.call_args
        self.assertIs(args[0], client_socket)
        self.assertEqual(args[1], 200)
        self.assertEqual(args[2]["status"], "error")
        self.assertIn("Authentication was not sent", args[2]["message"])


class HttpUISessionLockTests(unittest.TestCase):
    def test_status_reports_port_locked_for_unauthorized_browser(self) -> None:
        ui = HttpUI.__new__(HttpUI)
        ui._port = 8080
        ui._active_session_token = "secret123"
        ui._session_lock = Mock()
        ui._session_lock.__enter__ = Mock()
        ui._session_lock.__exit__ = Mock()
        ui._client = Mock()
        ui._client.is_authenticated = True
        ui._client.username = "alice"
        ui._client._running = True
        ui._get_active_users = Mock(return_value=[])
        ui._send_json_response = Mock()
        sock = Mock()

        # 1. Request from another browser (no cookie)
        ui._handle_status(sock, headers={})
        sock.close.assert_called_once()
        args, _ = ui._send_json_response.call_args
        self.assertEqual(args[1], 200)
        self.assertFalse(args[2]["authenticated"])
        self.assertTrue(args[2]["port_locked"])
        self.assertEqual(args[2]["locked_by"], "alice")

        # 2. Request from the owner browser (matching cookie)
        sock.reset_mock()
        ui._send_json_response.reset_mock()
        ui._handle_status(sock, headers={"cookie": "streamchat_session=secret123"})
        sock.close.assert_called_once()
        args, _ = ui._send_json_response.call_args
        self.assertEqual(args[1], 200)
        self.assertTrue(args[2]["authenticated"])
        self.assertFalse(args[2]["port_locked"])
        self.assertEqual(args[2]["username"], "alice")

    def test_login_rejected_when_port_already_authenticated(self) -> None:
        ui = HttpUI.__new__(HttpUI)
        ui._port = 8080
        ui._active_session_token = "secret123"
        ui._client = Mock()
        ui._client.is_authenticated = True
        ui._client.username = "alice"
        ui._send_json_response = Mock()
        sock = Mock()

        ui._handle_login(sock, json.dumps({"username": "bob", "password": "123"}))
        sock.close.assert_called_once()
        args, _ = ui._send_json_response.call_args
        self.assertEqual(args[1], 403)
        self.assertTrue(args[2]["port_locked"])
        self.assertIn("đang có tài khoản @alice", args[2]["message"])


class ClientFileTransferRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_mock_client(self, username: str) -> ChatClient:
        client = ChatClient.__new__(ChatClient)
        client._username = username
        client._authenticated = True
        client._running = True
        client.event_queue = queue.Queue()
        client._file_transfers = {}
        client._incoming_files = {}
        client._early_file_chunks = {}
        client._download_dir = os.path.join(self.temp_dir, f"dl_{username}")
        os.makedirs(client._download_dir, exist_ok=True)
        client._connection = Mock()
        client._connection._next_stream_id = 1
        client._connection._streams = {}
        client._send_json = Mock()
        return client

    def test_outgoing_file_does_not_contaminate_incoming_files(self) -> None:
        client = self._create_mock_client("alice")
        test_file = os.path.join(self.temp_dir, "alice_notes.txt")
        with open(test_file, "wb") as f:
            f.write(b"Alice's secret content")

        mock_stream = Mock()
        mock_stream.stream_id = 42
        client._connection.allocate_stream = Mock(return_value=mock_stream)

        sid = client.start_file_transfer(test_file, "bob")
        self.assertEqual(sid, 42)
        # Outgoing file MUST NOT be in incoming_files
        self.assertNotIn(42, client._incoming_files)
        self.assertIn(42, client._file_transfers)
        # Senders can still download their own file via fallback to file_transfers
        name, path = client.get_download_file(42)
        self.assertEqual(name, "alice_notes.txt")
        self.assertEqual(path, test_file)

    def test_bidirectional_file_download_resolution(self) -> None:
        alice = self._create_mock_client("alice")
        bob = self._create_mock_client("bob")

        # 1. Alice creates outgoing transfer on stream 10
        alice_file = os.path.join(self.temp_dir, "alice_file.txt")
        with open(alice_file, "wb") as f:
            f.write(b"Content from Alice")
        alice_stream = Mock()
        alice_stream.stream_id = 10
        alice._connection.allocate_stream = Mock(return_value=alice_stream)
        alice.start_file_transfer(alice_file, "bob")

        # 2. Bob creates outgoing transfer on stream 20
        bob_file = os.path.join(self.temp_dir, "bob_file.txt")
        with open(bob_file, "wb") as f:
            f.write(b"Content from Bob")
        bob_stream = Mock()
        bob_stream.stream_id = 20
        bob._connection.allocate_stream = Mock(return_value=bob_stream)
        bob.start_file_transfer(bob_file, "alice")

        # 3. Simulate Bob receiving Alice's file (stream 10)
        alice_hash = hashlib.sha256(b"Content from Alice").hexdigest()
        bob._on_app_message(0, json.dumps({
            "type": "FILE_READY",
            "stream_id": 10,
            "filename": "alice_file.txt",
            "size": len(b"Content from Alice"),
            "sha256": alice_hash,
            "from": "alice",
            "target": "bob",
        }).encode("utf-8"))
        bob._on_app_message(10, b"Content from Alice")

        # 4. Simulate Alice receiving Bob's file (stream 20)
        bob_hash = hashlib.sha256(b"Content from Bob").hexdigest()
        alice._on_app_message(0, json.dumps({
            "type": "FILE_READY",
            "stream_id": 20,
            "filename": "bob_file.txt",
            "size": len(b"Content from Bob"),
            "sha256": bob_hash,
            "from": "bob",
            "target": "alice",
        }).encode("utf-8"))
        alice._on_app_message(20, b"Content from Bob")

        # Verify Alice downloads Bob's file when passing Bob's stream_id
        dl_bob_name, dl_bob_path = alice.get_download_file(20)
        self.assertEqual(dl_bob_name, "bob_file.txt")
        with open(dl_bob_path, "rb") as f:
            self.assertEqual(f.read(), b"Content from Bob")

        # Verify Bob downloads Alice's file when passing Alice's stream_id
        dl_alice_name, dl_alice_path = bob.get_download_file(10)
        self.assertEqual(dl_alice_name, "alice_file.txt")
        with open(dl_alice_path, "rb") as f:
            self.assertEqual(f.read(), b"Content from Alice")

    def test_early_chunks_buffered_until_file_ready(self) -> None:
        client = self._create_mock_client("bob")
        raw_data = b"Pre-delivered file chunks before metadata"
        file_hash = hashlib.sha256(raw_data).hexdigest()

        # Chunk arrives BEFORE FILE_READY
        client._on_app_message(55, raw_data)
        self.assertIn(55, client._early_file_chunks)
        self.assertEqual(client._early_file_chunks[55], [raw_data])

        # Now FILE_READY metadata arrives
        client._on_app_message(0, json.dumps({
            "type": "FILE_READY",
            "stream_id": 55,
            "filename": "early.dat",
            "size": len(raw_data),
            "sha256": file_hash,
            "from": "alice",
            "target": "bob",
        }).encode("utf-8"))

        # Early chunk should be flushed and file assembled
        self.assertNotIn(55, client._early_file_chunks)
        self.assertEqual(client._incoming_files[55]["status"], "completed")
        fname, fpath = client.get_download_file(55)
        self.assertEqual(fname, "early.dat")
        with open(fpath, "rb") as f:
            self.assertEqual(f.read(), raw_data)


    def test_allocate_stream_skips_existing_and_wraps(self) -> None:
        from transport.connection import Connection
        mock_sock = Mock()
        conn = Connection(mock_sock, ("127.0.0.1", 9000), conn_id=1, is_server=False)
        conn._next_stream_id = 65533
        conn.get_or_create_stream(65533)
        conn.get_or_create_stream(1)

        # 65533 and 1 exist. Allocating should skip 65533, wrap to 1, skip 1, and allocate 3
        s = conn.allocate_stream()
        self.assertEqual(s.stream_id, 3)
        self.assertIn(3, conn._streams)

    def test_transfer_file_waits_for_inflight_and_queue_drain(self) -> None:
        """Kiểm tra luồng truyền file lớn: không báo FILE_COMPLETE ảo khi queue/in_flight chưa gửi xong."""
        client = self._create_mock_client("alice")
        test_file = os.path.join(self.temp_dir, "large_test.dat")
        with open(test_file, "wb") as f:
            f.write(b"X" * 8192)

        stream = Mock()
        stream.stream_id = 99
        client._connection.closed = False
        client._connection.allocate_stream = Mock(return_value=stream)
        client._file_transfers[99] = {
            "filename": "large_test.dat",
            "size": 8192,
            "sent": 0,
            "status": "transferring",
        }

        # Giả lập queue có gói tin đang bay
        client._connection._tx_queue = [(99, b"data", 0, 0)]
        client._connection.reliability = Mock()
        client._connection.reliability.can_send.return_value = True
        client._connection.reliability.in_flight = 0

        # Chạy transfer trong thread riêng
        t = threading.Thread(target=client._transfer_file_data, args=(test_file, stream, "bob"), daemon=True)
        t.start()

        # Trong lúc tx_queue còn phần tử, chưa thể có FILE_COMPLETE
        time.sleep(0.05)
        self.assertTrue(t.is_alive())
        self.assertEqual(client._file_transfers[99]["status"], "transferring")

        # Xả hết queue và in_flight -> thread kết thúc và hoàn thành chuẩn xác
        client._connection._tx_queue.clear()
        t.join(timeout=1.0)
        self.assertFalse(t.is_alive())
        self.assertEqual(client._file_transfers[99]["status"], "completed")

    def test_send_chat_while_file_transfer_active(self) -> None:
        """Đảm bảo trong lúc đang truyền file lớn, client vẫn gửi được tin nhắn chat qua Stream 0 ngay lập tức."""
        client = self._create_mock_client("alice")
        test_file = os.path.join(self.temp_dir, "bg_transfer.dat")
        with open(test_file, "wb") as f:
            f.write(b"DATA" * 4096)

        stream = Mock()
        stream.stream_id = 11
        client._connection.closed = False
        client._connection.allocate_stream = Mock(return_value=stream)
        client._file_transfers[11] = {
            "filename": "bg_transfer.dat",
            "size": 16384,
            "sent": 0,
            "status": "transferring",
        }
        client._connection._tx_queue = [(11, b"chunk", 0, 0)]
        client._connection.reliability = Mock()
        client._connection.reliability.can_send.return_value = True
        client._connection.reliability.in_flight = 1

        # Chạy file transfer ngầm
        t = threading.Thread(target=client._transfer_file_data, args=(test_file, stream, "bob"), daemon=True)
        t.start()

        # Trong lúc file đang truyền ngầm, gọi send_chat
        client.send_chat("bob", "Hello Bob, I am sending you a file!")
        client._send_json.assert_called_with({
            "type": "MSG",
            "target": "bob",
            "text": "Hello Bob, I am sending you a file!",
        })

        # Dọn dẹp thread
        client._connection._tx_queue.clear()
        client._connection.reliability.in_flight = 0
        t.join(timeout=1.0)


if __name__ == "__main__":
    unittest.main()
