import os
import sys
import unittest
import socket

# Ensure src/ is in sys.path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(BASE_DIR, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from common.crypto import (
    generate_identity_keypair,
    sign_server_hello,
    verify_server_hello,
)
from transport.connection import Connection


class TestServerAuthentication(unittest.TestCase):
    """Test suite verifying Ed25519 Server Authentication and Certificate Pinning."""

    def setUp(self):
        self.server_priv, self.server_pub = generate_identity_keypair()
        self.fake_priv, self.fake_pub = generate_identity_keypair()

    def test_signature_generation_and_verification(self):
        """Valid server signature should be verified successfully."""
        conn_id = 12345
        ephemeral_server_x25519 = b"\x01" * 32

        sig = sign_server_hello(self.server_priv, conn_id, ephemeral_server_x25519)
        self.assertEqual(len(sig), 64)

        # Verification with correct pinned key
        is_valid = verify_server_hello(self.server_pub, conn_id, ephemeral_server_x25519, sig)
        self.assertTrue(is_valid)

    def test_signature_tampered_fails(self):
        """Tampered conn_id or ephemeral key must fail verification."""
        conn_id = 12345
        ephemeral_server_x25519 = b"\x01" * 32
        sig = sign_server_hello(self.server_priv, conn_id, ephemeral_server_x25519)

        # Attacker tampers with conn_id
        self.assertFalse(verify_server_hello(self.server_pub, 99999, ephemeral_server_x25519, sig))

        # Attacker tampers with ephemeral key
        tampered_key = b"\x02" * 32
        self.assertFalse(verify_server_hello(self.server_pub, conn_id, tampered_key, sig))

    def test_mitm_attacker_key_rejected(self):
        """If MITM signs with its own key, client with pinned key must reject it."""
        conn_id = 12345
        ephemeral_server_x25519 = b"\x01" * 32

        # Attacker signs with fake identity key
        mitm_sig = sign_server_hello(self.fake_priv, conn_id, ephemeral_server_x25519)

        # Client verifies using the legitimate pinned server key -> MUST FAIL
        self.assertFalse(verify_server_hello(self.server_pub, conn_id, ephemeral_server_x25519, mitm_sig))

    def test_handshake_flow_with_pinned_key(self):
        """End-to-end handshake with pinned server key succeeds when authentic, fails when MITM."""
        dummy_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        # 1. Authentic Server Connection
        server_conn = Connection(
            sock=dummy_sock,
            remote_addr=("127.0.0.1", 9001),
            conn_id=1001,
            is_server=True,
            server_identity_key=self.server_priv,
        )

        # Client Connection with legitimate pinned key
        client_conn = Connection(
            sock=dummy_sock,
            remote_addr=("127.0.0.1", 9000),
            conn_id=1001,
            is_server=False,
            pinned_server_key=self.server_pub,
        )

        # Client sends HELLO
        client_conn.handle_handshake()
        hello_raw = client_conn._hello_raw

        # Server receives HELLO and creates HELLO_ACK
        server_conn.receive_packet(hello_raw)
        hello_ack_raw = server_conn._hello_ack_raw
        self.assertIsNotNone(hello_ack_raw)

        # Client receives authentic HELLO_ACK
        client_conn.receive_packet(hello_ack_raw)
        self.assertTrue(client_conn.handshake_complete.is_set())

        # 2. MITM Test: Client with pinned key receives HELLO_ACK from rogue server
        rogue_server = Connection(
            sock=dummy_sock,
            remote_addr=("127.0.0.1", 9001),
            conn_id=1002,
            is_server=True,
            server_identity_key=self.fake_priv,  # Attacker's key
        )

        victim_client = Connection(
            sock=dummy_sock,
            remote_addr=("127.0.0.1", 9000),
            conn_id=1002,
            is_server=False,
            pinned_server_key=self.server_pub,  # Expects real server
        )

        victim_client.handle_handshake()
        rogue_server.receive_packet(victim_client._hello_raw)
        fake_ack = rogue_server._hello_ack_raw

        # Victim receives fake ACK -> MUST DROP and NOT complete handshake
        victim_client.receive_packet(fake_ack)
        self.assertFalse(victim_client.handshake_complete.is_set())

        dummy_sock.close()


if __name__ == "__main__":
    unittest.main()
