"""
common/constants.py
~~~~~~~~~~~~~~~~~~~
Global constants for the custom reliable transport protocol.

Includes: magic number, packet types, flags, header sizes, timeout configs,
MTU, and congestion control parameters.
"""

from __future__ import annotations

# ──────────────────────────────────────────────────
# Magic number – first 4 bytes for protocol identification
# ──────────────────────────────────────────────────
PROTOCOL_MAGIC: bytes = b"\xDE\xAD\xBE\xEF"

# ──────────────────────────────────────────────────
# Packet Types (1 byte – uint8)
# ──────────────────────────────────────────────────
PKT_HELLO: int = 0x01          # Client sends to initiate handshake
PKT_HELLO_ACK: int = 0x02      # Server responds to handshake
PKT_DATA: int = 0x03           # Application data
PKT_ACK: int = 0x04            # Selective ACK confirmation
PKT_HEARTBEAT: int = 0x05      # Keepalive ping
PKT_HEARTBEAT_ACK: int = 0x06  # Keepalive pong
PKT_CLOSE: int = 0x07          # Close connection
PKT_AUTH_REQ: int = 0x08       # Authentication request
PKT_AUTH_RESP: int = 0x09      # Authentication response

# ──────────────────────────────────────────────────
# Flags (1 byte – bitmask)
# ──────────────────────────────────────────────────
FLAG_NONE: int = 0x00
FLAG_EOM: int = 0x01            # End-of-Message
FLAG_FIN: int = 0x02            # Stream termination
FLAG_SYN: int = 0x04            # Stream initialization
FLAG_ENCRYPTED: int = 0x08      # Payload is AES-256-GCM encrypted

# ──────────────────────────────────────────────────
# Header layout (22 bytes total)
#   msg_type   : uint8     (1)
#   conn_id    : uint32    (4)
#   seq_num    : uint32    (4)
#   flags      : uint8     (1)
#   stream_id  : uint16    (2)
#   offset     : uint32    (4)
#   payload_len: uint16    (2)
#   reserved   : uint32    (4)
#                           ──
#                           22 bytes
# ──────────────────────────────────────────────────
HEADER_SIZE: int = 22
HEADER_FORMAT: str = "!BIBHIHHI"

HEADER_STRUCT_FMT: str = "!BIIBHIH2s"

# ──────────────────────────────────────────────────
# Sizes & MTU
# ──────────────────────────────────────────────────
MAX_UDP_PAYLOAD: int = 1400     # bytes – safe threshold against IP fragmentation
MAX_APP_PAYLOAD: int = MAX_UDP_PAYLOAD - HEADER_SIZE  # 1378 bytes
RECV_BUFFER_SIZE: int = 65535   # Maximum socket receive buffer size

# ──────────────────────────────────────────────────
# Timeout & Keepalive (seconds)
# ──────────────────────────────────────────────────
INITIAL_RTO: float = 1.0        # Initial retransmission timeout
MIN_RTO: float = 0.2            # Minimum RTO clamp
MAX_RTO: float = 10.0           # Maximum RTO clamp
HEARTBEAT_INTERVAL: float = 5.0 # Send heartbeat every 5 seconds
IDLE_TIMEOUT: float = 30.0      # Terminate connection if idle > 30 seconds
TICK_INTERVAL: float = 0.05     # Tick loop periodicity (50ms)

# ──────────────────────────────────────────────────
# CUBIC Congestion Parameters
# ──────────────────────────────────────────────────
CUBIC_C: float = 0.4            # CUBIC scaling constant C
CUBIC_BETA: float = 0.7         # Multiplicative decrease factor on loss
INITIAL_CWND: int = 10          # Initial congestion window (packets)
MIN_CWND: int = 2               # Minimum congestion window clamp
MAX_CWND: int = 1024            # Maximum congestion window clamp
SSTHRESH_INITIAL: int = 64      # Initial slow-start threshold

# ──────────────────────────────────────────────────
# Default Server Configuration
# ──────────────────────────────────────────────────
DEFAULT_SERVER_HOST: str = "127.0.0.1"
DEFAULT_SERVER_PORT: int = 9000
DEFAULT_HTTP_PORT: int = 8080

# ──────────────────────────────────────────────────
# Encryption
# ──────────────────────────────────────────────────
AES_KEY_SIZE: int = 32          # AES-256
GCM_NONCE_SIZE: int = 12        # 96-bit nonce for AES-GCM
GCM_TAG_SIZE: int = 16          # 128-bit authentication tag
