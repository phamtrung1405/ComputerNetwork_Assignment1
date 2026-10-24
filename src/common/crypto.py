"""
common/crypto.py
~~~~~~~~~~~~~~~~
Lớp bảo mật: trao đổi khóa X25519, dẫn xuất khóa phiên HKDF,
mã hóa/giải mã AES-256-GCM.

Sử dụng thư viện ``cryptography`` (cài bằng ``pip install cryptography``).
"""

from __future__ import annotations

from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from common.constants import AES_KEY_SIZE, GCM_NONCE_SIZE


def generate_keypair() -> tuple[X25519PrivateKey, bytes]:
    """Tạo cặp khóa X25519 cho quá trình Handshake.

    Returns:
        Tuple gồm đối tượng private key và public key dạng raw bytes (32 bytes).
    """
    private_key: X25519PrivateKey = X25519PrivateKey.generate()
    public_bytes: bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return private_key, public_bytes


def derive_shared_secret(
    private_key: X25519PrivateKey,
    peer_public_bytes: bytes,
) -> bytes:
    """Dẫn xuất khóa phiên AES-256 bằng ECDH + HKDF.

    Args:
        private_key: Khóa riêng X25519 của bên này.
        peer_public_bytes: 32 bytes khóa công khai của bên kia.

    Returns:
        32 bytes khóa phiên AES-256 dùng chung.
    """
    peer_public_key: X25519PublicKey = X25519PublicKey.from_public_bytes(
        peer_public_bytes
    )
    shared_raw: bytes = private_key.exchange(peer_public_key)
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=AES_KEY_SIZE,
        salt=None,
        info=b"streaming-protocol-session-key",
    )
    session_key: bytes = hkdf.derive(shared_raw)
    return session_key


def encrypt(payload: bytes, session_key: bytes, packet_num: int) -> bytes:
    """Mã hóa payload bằng AES-256-GCM.

    Args:
        payload: Dữ liệu ứng dụng cần mã hóa.
        session_key: Khóa phiên 32 bytes.
        packet_num: Số thứ tự gói tin, dùng làm nonce (96-bit).

    Returns:
        bytes: ciphertext || tag (tag 16 bytes được nối bởi AESGCM).
    """
    nonce: bytes = packet_num.to_bytes(GCM_NONCE_SIZE, byteorder="big")
    aes_gcm = AESGCM(session_key)
    ciphertext: bytes = aes_gcm.encrypt(nonce, payload, associated_data=None)
    return ciphertext


def decrypt(ciphertext: bytes, session_key: bytes, packet_num: int) -> bytes:
    """Giải mã và xác thực tính toàn vẹn gói tin.

    Args:
        ciphertext: Dữ liệu đã mã hóa (bao gồm tag).
        session_key: Khóa phiên 32 bytes.
        packet_num: Số thứ tự gói tin dùng để khôi phục nonce.

    Returns:
        bytes: Payload gốc sau giải mã.

    Raises:
        cryptography.exceptions.InvalidTag: Khi xác thực thất bại.
    """
    nonce: bytes = packet_num.to_bytes(GCM_NONCE_SIZE, byteorder="big")
    aes_gcm = AESGCM(session_key)
    plaintext: bytes = aes_gcm.decrypt(nonce, ciphertext, associated_data=None)
    return plaintext
