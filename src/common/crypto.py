"""
common/crypto.py
~~~~~~~~~~~~~~~~
Security layer: X25519 key exchange, HKDF session key derivation,
and AES-256-GCM authenticated encryption/decryption.

Uses the ``cryptography`` library (install via ``pip install cryptography``).
"""

from __future__ import annotations

import hashlib
import threading
from typing import Optional, Tuple

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from common.constants import AES_KEY_SIZE, GCM_NONCE_SIZE, GCM_TAG_SIZE

# Độ dài khóa công khai X25519 dạng raw bytes
X25519_KEY_SIZE: int = 32

# ─────────────────────────────────────────────────────────────────────────────
# Bộ theo dõi Nonce phòng ngừa tái sử dụng (Nonce Reuse Guard)
# ─────────────────────────────────────────────────────────────────────────────
_used_nonces: dict[bytes, set[bytes]] = {}
_nonce_lock: threading.Lock = threading.Lock()


def reset_nonce_tracker() -> None:
    """Xóa toàn bộ lịch sử Nonce đã ghi nhận trong bộ đệm.
    
    Hàm này phục vụ việc dọn dẹp môi trường kiểm thử (unit testing) giữa các ca kiểm thử.
    """
    with _nonce_lock:
        _used_nonces.clear()


def _construct_nonce(packet_num: int, is_server: Optional[bool] = None) -> bytes:
    """Tạo Nonce 12-byte (96-bit) cho chuẩn AES-GCM từ số thứ tự gói tin.

    Cơ chế sinh Nonce:
    - Nếu ``is_server`` là ``None``: Sử dụng trực tiếp 12 bytes Big-Endian của ``packet_num``
      để đảm bảo tính tương thích ngược tuyệt đối với các caller hiện có.
    - Nếu ``is_server`` được chỉ định:
      - Byte đầu tiên (Byte 0): ``0x01`` (Server) hoặc ``0x00`` (Client).
      - 11 bytes tiếp theo: Chuyển đổi ``packet_num`` thành 11 bytes Big-Endian.
      Cơ chế này đảm bảo Client và Server dù gửi cùng một số thứ tự gói tin (ví dụ: ``seq=0``)
      thì Nonce sinh ra vẫn hoàn toàn khác biệt, loại trừ triệt để nguy cơ Nonce Collision.

    Args:
        packet_num: Số thứ tự gói tin (phải là số nguyên không âm).
        is_server: Bật cờ phân biệt vai trò gửi (True nếu Server, False nếu Client).

    Returns:
        12 bytes Nonce nhị phân chuẩn cho AES-GCM.

    Raises:
        ValueError: Nếu ``packet_num`` âm hoặc vượt quá khả năng biểu diễn của Nonce.
    """
    if packet_num < 0:
        raise ValueError(f"packet_num phải là số nguyên không âm, nhận được: {packet_num}")

    if is_server is None:
        try:
            return packet_num.to_bytes(GCM_NONCE_SIZE, byteorder="big")
        except OverflowError:
            raise ValueError(f"packet_num={packet_num} quá lớn vượt quá {GCM_NONCE_SIZE} bytes")

    # Phân tách không gian Nonce theo vai trò gửi (Directional Nonce)
    prefix_byte = b"\x01" if is_server else b"\x00"
    try:
        seq_bytes = packet_num.to_bytes(GCM_NONCE_SIZE - 1, byteorder="big")
    except OverflowError:
        raise ValueError(f"packet_num={packet_num} quá lớn vượt quá {GCM_NONCE_SIZE - 1} bytes")

    return prefix_byte + seq_bytes


# ─────────────────────────────────────────────────────────────────────────────
# Trao đổi khóa X25519 & Dẫn xuất khóa HKDF
# ─────────────────────────────────────────────────────────────────────────────

def generate_keypair() -> tuple[X25519PrivateKey, bytes]:
    """Generate an X25519 keypair for the Handshake process.

    Returns:
        Tuple containing the private key object and raw 32-byte public key.
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
    salt: Optional[bytes] = None,
    info: bytes = b"streaming-protocol-session-key",
) -> bytes:
    """Derive a 256-bit AES session key using ECDH + HKDF.

    Args:
        private_key: Local X25519 private key.
        peer_public_bytes: 32 bytes of peer's public key.

    Returns:
        32-byte shared symmetric AES-256 session key.
    """
    if not isinstance(private_key, X25519PrivateKey):
        raise TypeError(f"private_key phải là thể hiện X25519PrivateKey, nhận được {type(private_key)}")

    if not isinstance(peer_public_bytes, (bytes, bytearray)):
        raise TypeError(f"peer_public_bytes phải là bytes, nhận được {type(peer_public_bytes)}")

    if len(peer_public_bytes) != X25519_KEY_SIZE:
        raise ValueError(
            f"peer_public_bytes phải có độ dài chính xác {X25519_KEY_SIZE} bytes, "
            f"nhận được: {len(peer_public_bytes)} bytes."
        )

    peer_public_key: X25519PublicKey = X25519PublicKey.from_public_bytes(bytes(peer_public_bytes))
    shared_raw: bytes = private_key.exchange(peer_public_key)

    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=AES_KEY_SIZE,
        salt=salt,
        info=info,
    )
    session_key: bytes = hkdf.derive(shared_raw)
    return session_key


def derive_session_keys(
    private_key: X25519PrivateKey,
    peer_public_bytes: bytes,
) -> tuple[bytes, bytes]:
    """Derive distinct client-to-server and server-to-client AES keys."""
    shared_secret = derive_shared_secret(
        private_key,
        peer_public_bytes,
        info=b"streaming-protocol-directional-session-keys",
    )
    key_material = HKDF(
        algorithm=hashes.SHA256(),
        length=2 * AES_KEY_SIZE,
        salt=None,
        info=b"streaming-protocol-directional-key-expansion",
    ).derive(shared_secret)
    return key_material[:AES_KEY_SIZE], key_material[AES_KEY_SIZE:]


def encrypt(
    payload: bytes,
    session_key: bytes,
    packet_num: int,
    associated_data: Optional[bytes] = None,
    is_server: Optional[bool] = None,
) -> bytes:
    """Encrypt payload using AES-256-GCM.

    Args:
        payload: Application payload to encrypt.
        session_key: 32-byte symmetric session key.
        packet_num: Packet sequence number used as 96-bit unique nonce.
        associated_data: Optional authenticated data that is not encrypted.
        is_server: Optional sender role used to separate directional nonces.

    Returns:
        bytes: ciphertext || tag (16-byte authentication tag appended by AESGCM).
    """
    if not isinstance(payload, (bytes, bytearray)):
        raise TypeError(f"payload phải là bytes hoặc bytearray, nhận được {type(payload)}")

    if not isinstance(session_key, (bytes, bytearray)):
        raise TypeError(f"session_key phải là bytes, nhận được {type(session_key)}")

    if len(session_key) != AES_KEY_SIZE:
        raise ValueError(f"session_key phải có độ dài chính xác {AES_KEY_SIZE} bytes, nhận được {len(session_key)}")

    if not isinstance(packet_num, int):
        raise TypeError(f"packet_num phải là số nguyên, nhận được {type(packet_num)}")

    if associated_data is not None and not isinstance(associated_data, (bytes, bytearray)):
        raise TypeError(f"associated_data phải là bytes hoặc None, nhận được {type(associated_data)}")

    nonce: bytes = _construct_nonce(packet_num, is_server=is_server)

    # ── Kiểm tra chống tái sử dụng Nonce với cùng khóa phiên ──
    key_fingerprint: bytes = hashlib.sha256(bytes(session_key)).digest()[:8]
    with _nonce_lock:
        if key_fingerprint not in _used_nonces:
            _used_nonces[key_fingerprint] = set()
        if nonce in _used_nonces[key_fingerprint]:
            raise ValueError(
                f"Nonce reuse detected: Nonce '{nonce.hex()}' đã từng được sử dụng "
                f"với khóa phiên này! AES-GCM nghiêm cấm mã hóa hai bản rõ khác nhau "
                f"với cùng một cặp (Key, Nonce)."
            )
        _used_nonces[key_fingerprint].add(nonce)

    aad_bytes: Optional[bytes] = bytes(associated_data) if associated_data is not None else None
    aes_gcm = AESGCM(bytes(session_key))
    ciphertext: bytes = aes_gcm.encrypt(nonce, bytes(payload), associated_data=aad_bytes)
    return ciphertext


def decrypt(
    ciphertext: bytes,
    session_key: bytes,
    packet_num: int,
    associated_data: Optional[bytes] = None,
    is_server: Optional[bool] = None,
) -> bytes:
    """Decrypt and verify packet integrity with AES-256-GCM.

    Args:
        ciphertext: Encrypted payload (including 16-byte authentication tag).
        session_key: 32-byte symmetric session key.
        packet_num: Packet sequence number used to reconstruct nonce.
        associated_data: Optional authenticated data that is not encrypted.
        is_server: Sender role used to reconstruct directional nonces.

    Returns:
        bytes: Original decrypted plaintext payload.

    Raises:
        cryptography.exceptions.InvalidTag: If authentication or integrity check fails.
    """
    if not isinstance(ciphertext, (bytes, bytearray)):
        raise TypeError(f"ciphertext phải là bytes hoặc bytearray, nhận được {type(ciphertext)}")

    if len(ciphertext) < GCM_TAG_SIZE:
        raise ValueError(
            f"ciphertext quá ngắn: yêu cầu tối thiểu {GCM_TAG_SIZE} bytes (tag size), "
            f"nhận được: {len(ciphertext)} bytes."
        )

    if not isinstance(session_key, (bytes, bytearray)):
        raise TypeError(f"session_key phải là bytes, nhận được {type(session_key)}")

    if len(session_key) != AES_KEY_SIZE:
        raise ValueError(f"session_key phải có độ dài chính xác {AES_KEY_SIZE} bytes, nhận được {len(session_key)}")

    if not isinstance(packet_num, int):
        raise TypeError(f"packet_num phải là số nguyên, nhận được {type(packet_num)}")

    if associated_data is not None and not isinstance(associated_data, (bytes, bytearray)):
        raise TypeError(f"associated_data phải là bytes hoặc None, nhận được {type(associated_data)}")

    nonce: bytes = _construct_nonce(packet_num, is_server=is_server)
    aad_bytes: Optional[bytes] = bytes(associated_data) if associated_data is not None else None

    aes_gcm = AESGCM(bytes(session_key))
    plaintext: bytes = aes_gcm.decrypt(nonce, bytes(ciphertext), associated_data=aad_bytes)
    return plaintext
