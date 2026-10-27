"""
common/crypto.py
~~~~~~~~~~~~~~~~
Security layer: X25519 key exchange, HKDF session key derivation,
and AES-256-GCM authenticated encryption/decryption.

Uses the ``cryptography`` library (install via ``pip install cryptography``).
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
) -> bytes:
    """Derive a 256-bit AES session key using ECDH + HKDF.

    Args:
        private_key: Local X25519 private key.
        peer_public_bytes: 32 bytes of peer's public key.

    Returns:
        32-byte shared symmetric AES-256 session key.
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
    """Encrypt payload using AES-256-GCM.

    Args:
        payload: Application payload to encrypt.
        session_key: 32-byte symmetric session key.
        packet_num: Packet sequence number used as 96-bit unique nonce.

    Returns:
        bytes: ciphertext || tag (16-byte authentication tag appended by AESGCM).
    """
    nonce: bytes = packet_num.to_bytes(GCM_NONCE_SIZE, byteorder="big")
    aes_gcm = AESGCM(session_key)
    ciphertext: bytes = aes_gcm.encrypt(nonce, payload, associated_data=None)
    return ciphertext


def decrypt(ciphertext: bytes, session_key: bytes, packet_num: int) -> bytes:
    """Decrypt and verify packet integrity with AES-256-GCM.

    Args:
        ciphertext: Encrypted payload (including 16-byte authentication tag).
        session_key: 32-byte symmetric session key.
        packet_num: Packet sequence number used to reconstruct nonce.

    Returns:
        bytes: Original decrypted plaintext payload.

    Raises:
        cryptography.exceptions.InvalidTag: If authentication or integrity check fails.
    """
    nonce: bytes = packet_num.to_bytes(GCM_NONCE_SIZE, byteorder="big")
    aes_gcm = AESGCM(session_key)
    plaintext: bytes = aes_gcm.decrypt(nonce, ciphertext, associated_data=None)
    return plaintext
