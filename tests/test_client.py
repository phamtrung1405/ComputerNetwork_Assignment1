"""Tests for client handshake and login behavior."""

from __future__ import annotations

import hashlib
import json
import sys
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


if __name__ == "__main__":
    unittest.main()
