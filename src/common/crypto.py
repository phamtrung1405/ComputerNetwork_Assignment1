"""
common/crypto.py
~~~~~~~~~~~~~~~~
Lớp mật mã học bảo mật tầng truyền tải (Transport Security Layer) cho STREAMCHAT.

Hiện thực hóa:
- Trao đổi khóa bất đối xứng Diffie-Hellman trên đường cong elliptic Curve25519 (X25519).
- Dẫn xuất khóa phiên đối xứng (HKDF-SHA256) với hỗ trợ khóa ghi hai chiều (Directional Subkeys).
- Mã hóa và giải mã dữ liệu có xác thực (AEAD) bằng chuẩn AES-256-GCM.
- Phòng chống tái sử dụng Nonce (Nonce Reuse Protection) và ràng buộc Header làm Authenticated Additional Data (AAD).

Sử dụng thư viện chuẩn công nghiệp ``cryptography``.
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
    """Tạo cặp khóa bất đối xứng X25519 cho quá trình Handshake trao đổi khóa ECDH.

    Thuật toán:
    - Sinh khóa riêng ngẫu nhiên an toàn mật mã học (cryptographically secure random)
      trên đường cong Elliptic Curve25519.
    - Trích xuất khóa công khai tương ứng dưới dạng 32 raw bytes (không dùng định dạng ASN.1/DER).

    Returns:
        Tuple gồm:
        - `X25519PrivateKey`: Đối tượng khóa riêng tư (phải được giữ bí mật tại endpoint).
        - `bytes`: 32 bytes khóa công khai sẵn sàng gửi qua gói tin PKT_HELLO / PKT_HELLO_ACK.
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
    """Dẫn xuất khóa phiên đối xứng AES-256 từ khóa riêng và khóa công khai của đối tác.

    Thuật toán:
    1. Kiểm tra tính hợp lệ của tham số đầu vào (loại dữ liệu, kích thước khóa đối tác = 32B).
    2. Khôi phục đối tượng `X25519PublicKey` từ mảng 32 bytes thô.
    3. Thực hiện phép nhân điểm trên đường cong Elliptic Diffie-Hellman (ECDH) để tìm
       điểm bí mật chung 32 bytes (`shared_raw = private_key.exchange(...)`).
    4. Sử dụng thuật toán HKDF (HMAC-based Extract-and-Expand Key Derivation Function, RFC 5869)
       với hàm băm SHA-256 để trích xuất khóa đối xứng AES-256 có độ ngẫu nhiên cao.

    Args:
        private_key: Khóa riêng X25519 của endpoint hiện tại.
        peer_public_bytes: 32 bytes khóa công khai nhận từ endpoint đối tác.
        salt: Chuỗi byte muối tùy chọn (mặc định None theo đặc tả giao thức).
        info: Ngữ cảnh ràng buộc ứng dụng (mặc định b"streaming-protocol-session-key").

    Returns:
        32 bytes khóa đối xứng AES-256 dùng chung.

    Raises:
        TypeError: Nếu kiểu dữ liệu của `private_key` hoặc `peer_public_bytes` không hợp lệ.
        ValueError: Nếu độ dài của `peer_public_bytes` khác 32 bytes hoặc khóa bị suy biến.
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
    salt: Optional[bytes] = None,
) -> tuple[bytes, bytes]:
    """Dẫn xuất hai khóa phiên ghi riêng biệt (Directional Subkeys) cho Client và Server.

    Theo chuẩn thiết kế an toàn của TLS 1.3 và QUIC:
    - `client_write_key`: Dùng cho Client mã hóa gửi lên Server (Server dùng để giải mã).
    - `server_write_key`: Dùng cho Server mã hóa gửi xuống Client (Client dùng để giải mã).
    Việc sử dụng 2 khóa độc lập triệt tiêu hoàn toàn nguy cơ trùng lặp cặp (Key, Nonce)
    giữa hai chiều truyền dữ liệu.

    Args:
        private_key: Khóa riêng X25519 của endpoint hiện tại.
        peer_public_bytes: 32 bytes khóa công khai của bên kia.
        salt: Chuỗi byte muối tùy chọn.

    Returns:
        Tuple gồm `(client_write_key, server_write_key)` — mỗi khóa có độ dài 32 bytes.
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

    hkdf_client = HKDF(
        algorithm=hashes.SHA256(),
        length=AES_KEY_SIZE,
        salt=salt,
        info=b"streamchat-client-write-key",
    )
    client_write_key: bytes = hkdf_client.derive(shared_raw)

    hkdf_server = HKDF(
        algorithm=hashes.SHA256(),
        length=AES_KEY_SIZE,
        salt=salt,
        info=b"streamchat-server-write-key",
    )
    server_write_key: bytes = hkdf_server.derive(shared_raw)

    return client_write_key, server_write_key


# ─────────────────────────────────────────────────────────────────────────────
# Mã hóa & Giải mã AES-256-GCM (Authenticated Encryption with Associated Data)
# ─────────────────────────────────────────────────────────────────────────────

def encrypt(
    payload: bytes,
    session_key: bytes,
    packet_num: int,
    associated_data: Optional[bytes] = None,
    is_server: Optional[bool] = None,
) -> bytes:
    """Mã hóa và tạo mã xác thực toàn vẹn (AEAD) bằng thuật toán AES-256-GCM.

    Thuật toán:
    1. Kiểm tra tính hợp lệ của tham số đầu vào (payload, session_key = 32B, packet_num >= 0).
    2. Xây dựng Nonce 12-byte duy nhất từ `packet_num` (và `is_server` nếu có).
    3. Kiểm tra tính duy nhất của Nonce đối với `session_key` hiện hành; ném `ValueError`
       nếu phát hiện hành vi tái sử dụng Nonce (chống phá vỡ tính bảo mật của AES-GCM).
    4. Thực hiện mã hóa AES-GCM, tự động đính kèm 16 bytes Authentication Tag vào cuối ciphertext.
       Nếu `associated_data` được cung cấp (ví dụ 22 bytes Header), dữ liệu này được xác thực
       tính toàn vẹn nhưng không bị mã hóa.

    Args:
        payload: Dữ liệu ứng dụng cần mã hóa (dạng bytes hoặc bytearray).
        session_key: Khóa phiên đối xứng 32 bytes (AES-256).
        packet_num: Số thứ tự gói tin (dùng làm cơ sở tạo Nonce).
        associated_data: Dữ liệu bổ sung cần bảo vệ tính toàn vẹn (AAD, ví dụ: 22-byte Header).
        is_server: Cờ định hướng vai trò gửi (True nếu Server, False nếu Client).

    Returns:
        bytes: Chuỗi nhị phân gồm `ciphertext || authentication_tag` (tag 16 bytes gắn ở cuối).

    Raises:
        TypeError: Nếu `payload`, `session_key` hoặc `associated_data` không phải kiểu bytes.
        ValueError: Nếu độ dài khóa khác 32 bytes, số thứ tự âm, hoặc phát hiện Nonce bị tái sử dụng.
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
    """Xác thực tính toàn vẹn và giải mã gói tin mã hóa AES-256-GCM.

    Thuật toán:
    1. Kiểm tra tính hợp lệ của dữ liệu đầu vào.
    2. Xác minh độ dài tối thiểu của ciphertext: bắt buộc >= 16 bytes (Authentication Tag).
    3. Tái tạo Nonce 12-byte khớp chính xác với Nonce phía gửi đã sử dụng.
    4. Sử dụng `AESGCM.decrypt()` thực hiện giải mã và đối soát Authentication Tag.
       Nếu dữ liệu bị biến đổi dù chỉ 1 bit (hoặc AAD không khớp), thư viện sẽ bắn ngoại lệ
       `cryptography.exceptions.InvalidTag`.

    Args:
        ciphertext: Dữ liệu đã mã hóa (bao gồm 16 bytes Tag ở cuối).
        session_key: Khóa phiên đối xứng 32 bytes (phải khớp với bên gửi).
        packet_num: Số thứ tự gói tin dùng để khôi phục Nonce.
        associated_data: Dữ liệu bổ sung AAD cần đối soát toàn vẹn (ví dụ Header 22-byte).
        is_server: Cờ định hướng vai trò gửi (phải khớp với bên đã mã hóa).

    Returns:
        bytes: Bản rõ (Plaintext) nguyên gốc sau khi giải mã thành công.

    Raises:
        TypeError: Nếu `ciphertext`, `session_key` hoặc `associated_data` sai kiểu dữ liệu.
        ValueError: Nếu ciphertext ngắn hơn 16 bytes hoặc độ dài khóa khác 32 bytes.
        InvalidTag: Khi tính toàn vẹn bị vi phạm (dữ liệu bị giả mạo, sai khóa, hoặc sai Nonce/AAD).
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
