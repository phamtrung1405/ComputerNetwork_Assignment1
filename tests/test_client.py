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
        ui._client = Mock()
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


if __name__ == "__main__":
    unittest.main()
