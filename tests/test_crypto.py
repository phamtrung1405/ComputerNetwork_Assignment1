"""
tests/test_crypto.py
~~~~~~~~~~~~~~~~~~~~
Bộ kiểm thử đơn vị cho module mật mã học (src/common/crypto.py).
Kiểm thử trao đổi khóa X25519, dẫn xuất khóa HKDF, mã hóa/giải mã AES-256-GCM,
ràng buộc AAD, phát hiện sửa đổi dữ liệu (tampered ciphertext) và phòng chống tái sử dụng Nonce.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest

# Đảm bảo src có trong sys.path
SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from common.constants import AES_KEY_SIZE, GCM_NONCE_SIZE, GCM_TAG_SIZE
from common.crypto import (
    generate_keypair,
    derive_shared_secret,
    derive_session_keys,
    encrypt,
    decrypt,
    reset_nonce_tracker,
)


class TestKeyAgreement(unittest.TestCase):
    """Kiểm thử sinh cặp khóa X25519 và dẫn xuất khóa phiên ECDH + HKDF."""

    def test_generate_keypair_validity(self) -> None:
        """Kiểm tra kiểu dữ liệu và độ dài của private key & public key sinh ra."""
        private_key, public_bytes = generate_keypair()
        self.assertIsInstance(private_key, X25519PrivateKey)
        self.assertIsInstance(public_bytes, bytes)
        self.assertEqual(len(public_bytes), 32)

    def test_generate_keypair_randomness(self) -> None:
        """Đảm bảo hai lần sinh khóa liên tiếp tạo ra hai cặp khóa khác nhau."""
        _, pub1 = generate_keypair()
        _, pub2 = generate_keypair()
        self.assertNotEqual(pub1, pub2)

    def test_derive_shared_secret_matching(self) -> None:
        """Alice và Bob trao đổi public key, dẫn xuất ra cùng một khóa phiên đối xứng."""
        alice_priv, alice_pub = generate_keypair()
        bob_priv, bob_pub = generate_keypair()

        alice_secret = derive_shared_secret(alice_priv, bob_pub)
        bob_secret = derive_shared_secret(bob_priv, alice_pub)

        self.assertIsInstance(alice_secret, bytes)
        self.assertEqual(len(alice_secret), AES_KEY_SIZE)
        self.assertEqual(alice_secret, bob_secret)

    def test_derive_session_keys_directional(self) -> None:
        """Kiểm tra dẫn xuất cặp khóa hai chiều (client_write_key, server_write_key)."""
        alice_priv, alice_pub = generate_keypair()
        bob_priv, bob_pub = generate_keypair()

        alice_client_key, alice_server_key = derive_session_keys(alice_priv, bob_pub)
        bob_client_key, bob_server_key = derive_session_keys(bob_priv, alice_pub)

        # Alice và Bob phải tính ra cùng các subkeys
        self.assertEqual(alice_client_key, bob_client_key)
        self.assertEqual(alice_server_key, bob_server_key)

        # Hai chiều gửi-nhận phải có 2 khóa hoàn toàn khác nhau để chống Nonce Collision
        self.assertNotEqual(alice_client_key, alice_server_key)
        self.assertEqual(len(alice_client_key), AES_KEY_SIZE)
        self.assertEqual(len(alice_server_key), AES_KEY_SIZE)

    def test_different_peers_produce_different_secrets(self) -> None:
        """Trao đổi với bên thứ 3 (Charlie) phải tạo ra khóa phiên hoàn toàn khác."""
        alice_priv, alice_pub = generate_keypair()
        bob_priv, bob_pub = generate_keypair()
        charlie_priv, _ = generate_keypair()

        secret_ab = derive_shared_secret(alice_priv, bob_pub)
        secret_cb = derive_shared_secret(charlie_priv, bob_pub)
        self.assertNotEqual(secret_ab, secret_cb)


class TestEncryptionDecryption(unittest.TestCase):
    """Kiểm thử tính năng mã hóa và giải mã AES-256-GCM."""

    def setUp(self) -> None:
        reset_nonce_tracker()
        self.key = os.urandom(AES_KEY_SIZE)

    def tearDown(self) -> None:
        reset_nonce_tracker()

    def test_encrypt_decrypt_roundtrip_ascii(self) -> None:
        """Mã hóa và giải mã chuỗi văn bản ASCII."""
        plaintext = b"Hello, StreamChat Secure Protocol!"
        packet_num = 1
        ciphertext = encrypt(plaintext, self.key, packet_num)

        # Ciphertext phải bao gồm dữ liệu mã hóa + 16 bytes tag
        self.assertEqual(len(ciphertext), len(plaintext) + GCM_TAG_SIZE)
        decrypted = decrypt(ciphertext, self.key, packet_num)
        self.assertEqual(decrypted, plaintext)

    def test_encrypt_decrypt_empty_payload(self) -> None:
        """Mã hóa payload rỗng (chỉ có tag 16 bytes)."""
        plaintext = b""
        packet_num = 2
        ciphertext = encrypt(plaintext, self.key, packet_num)
        self.assertEqual(len(ciphertext), GCM_TAG_SIZE)

        decrypted = decrypt(ciphertext, self.key, packet_num)
        self.assertEqual(decrypted, plaintext)

    def test_encrypt_decrypt_large_binary_payload(self) -> None:
        """Mã hóa payload nhị phân ngẫu nhiên kích thước lớn (64 KB)."""
        plaintext = os.urandom(65536)
        packet_num = 100
        ciphertext = encrypt(plaintext, self.key, packet_num)
        decrypted = decrypt(ciphertext, self.key, packet_num)
        self.assertEqual(decrypted, plaintext)

    def test_encrypt_decrypt_with_associated_data(self) -> None:
        """Mã hóa và giải mã có gắn kèm Authenticated Associated Data (Header 22-byte)."""
        plaintext = b"Secret application message"
        aad = b"\x03" + os.urandom(21)  # Giả lập 22 bytes packet header
        packet_num = 42

        ciphertext = encrypt(plaintext, self.key, packet_num, associated_data=aad)
        decrypted = decrypt(ciphertext, self.key, packet_num, associated_data=aad)
        self.assertEqual(decrypted, plaintext)

    def test_directional_flag_encryption(self) -> None:
        """Kiểm tra mã hóa có phân biệt chiều (is_server=True vs is_server=False)."""
        plaintext = b"Bidirectional communication test"
        seq = 0

        # Cùng seq=0 nhưng khác chiều gửi
        c_client = encrypt(plaintext, self.key, seq, is_server=False)
        c_server = encrypt(plaintext, self.key, seq, is_server=True)

        # Vì Nonce khác nhau ở bit định hướng, ciphertext phải khác nhau
        self.assertNotEqual(c_client, c_server)

        # Giải mã đúng chiều phải khớp
        self.assertEqual(decrypt(c_client, self.key, seq, is_server=False), plaintext)
        self.assertEqual(decrypt(c_server, self.key, seq, is_server=True), plaintext)


class TestInvalidInputs(unittest.TestCase):
    """Kiểm thử xử lý lỗi khi tham số đầu vào không hợp lệ."""

    def setUp(self) -> None:
        reset_nonce_tracker()
        self.key = os.urandom(AES_KEY_SIZE)

    def tearDown(self) -> None:
        reset_nonce_tracker()

    def test_derive_shared_secret_invalid_public_key_type(self) -> None:
        priv, _ = generate_keypair()
        with self.assertRaises(TypeError):
            derive_shared_secret(priv, "not_bytes_public_key")  # type: ignore

    def test_derive_shared_secret_invalid_public_key_length(self) -> None:
        priv, _ = generate_keypair()
        with self.assertRaises(ValueError):
            derive_shared_secret(priv, b"\x00" * 31)
        with self.assertRaises(ValueError):
            derive_shared_secret(priv, b"\x00" * 33)

    def test_derive_shared_secret_invalid_private_key_type(self) -> None:
        with self.assertRaises(TypeError):
            derive_shared_secret("invalid_priv_key", b"\x00" * 32)  # type: ignore

    def test_encrypt_invalid_key_length(self) -> None:
        with self.assertRaises(ValueError):
            encrypt(b"data", b"short_key", 1)
        with self.assertRaises(ValueError):
            encrypt(b"data", os.urandom(16), 1)

    def test_encrypt_negative_packet_num(self) -> None:
        with self.assertRaises(ValueError):
            encrypt(b"data", self.key, -1)

    def test_encrypt_invalid_payload_type(self) -> None:
        with self.assertRaises(TypeError):
            encrypt("not bytes", self.key, 1)  # type: ignore

    def test_decrypt_ciphertext_too_short(self) -> None:
        # Ciphertext ngắn hơn 16 bytes tag phải bị từ chối ngay lập tức
        with self.assertRaises(ValueError):
            decrypt(b"short", self.key, 1)

    def test_decrypt_invalid_key_length(self) -> None:
        with self.assertRaises(ValueError):
            decrypt(os.urandom(20), b"invalid_key", 1)


class TestTamperedCiphertextAndSecurity(unittest.TestCase):
    """Kiểm thử phát hiện sửa đổi dữ liệu, giả mạo và phòng chống tái sử dụng Nonce."""

    def setUp(self) -> None:
        reset_nonce_tracker()
        self.key = os.urandom(AES_KEY_SIZE)

    def tearDown(self) -> None:
        reset_nonce_tracker()

    def test_tampered_ciphertext_payload_raises_invalid_tag(self) -> None:
        """Sửa đổi 1 byte trong phần ciphertext phải kích hoạt InvalidTag."""
        plaintext = b"Sensitive transactional message"
        packet_num = 10
        ciphertext = bytearray(encrypt(plaintext, self.key, packet_num))

        # Đảo 1 bit trong payload
        ciphertext[0] ^= 0x01

        with self.assertRaises(InvalidTag):
            decrypt(bytes(ciphertext), self.key, packet_num)

    def test_tampered_auth_tag_raises_invalid_tag(self) -> None:
        """Sửa đổi 1 byte trong Authentication Tag (16 bytes cuối) phải kích hoạt InvalidTag."""
        plaintext = b"Another confidential payload"
        packet_num = 11
        ciphertext = bytearray(encrypt(plaintext, self.key, packet_num))

        # Đảo 1 bit ở byte cuối cùng (thuộc tag)
        ciphertext[-1] ^= 0xFF

        with self.assertRaises(InvalidTag):
            decrypt(bytes(ciphertext), self.key, packet_num)

    def test_tampered_associated_data_raises_invalid_tag(self) -> None:
        """Header (AAD) bị can thiệp trên đường truyền phải khiến giải mã thất bại."""
        plaintext = b"Payload protected by AAD"
        valid_aad = b"Header_StreamID_01_Seq_12"
        packet_num = 12

        ciphertext = encrypt(plaintext, self.key, packet_num, associated_data=valid_aad)

        # Kẻ tấn công sửa đổi Stream ID trong AAD
        tampered_aad = b"Header_StreamID_02_Seq_12"
        with self.assertRaises(InvalidTag):
            decrypt(ciphertext, self.key, packet_num, associated_data=tampered_aad)

    def test_wrong_packet_num_raises_invalid_tag(self) -> None:
        """Giải mã với packet_num sai (dẫn đến sai Nonce) phải thất bại."""
        plaintext = b"Replay protection test"
        ciphertext = encrypt(plaintext, self.key, 15)

        with self.assertRaises(InvalidTag):
            decrypt(ciphertext, self.key, 16)

    def test_wrong_key_raises_invalid_tag(self) -> None:
        """Giải mã với session key khác phải thất bại."""
        plaintext = b"Cross-session isolation test"
        ciphertext = encrypt(plaintext, self.key, 20)
        wrong_key = os.urandom(AES_KEY_SIZE)

        with self.assertRaises(InvalidTag):
            decrypt(ciphertext, wrong_key, 20)

    def test_nonce_reuse_detection_prevents_catastrophe(self) -> None:
        """Thử gọi encrypt 2 lần với cùng 1 key và cùng 1 packet_num phải bị chặn đứng."""
        payload1 = b"Message one"
        payload2 = b"Message two"
        packet_num = 99

        # Lần 1: Thành công
        _ = encrypt(payload1, self.key, packet_num)

        # Lần 2: Cố tình dùng lại cùng key và packet_num -> Phải ném ValueError
        with self.assertRaises(ValueError) as ctx:
            encrypt(payload2, self.key, packet_num)
        self.assertIn("Nonce reuse detected", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
