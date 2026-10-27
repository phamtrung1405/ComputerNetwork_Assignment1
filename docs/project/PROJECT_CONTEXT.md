# STREAMCHAT — Project Context & Architecture Baseline

**Document Version:** 1.0.0  
**Date:** 2026-10-27  
**Target Audience:** Engineering Team & AI Coding Assistants  
**Status:** Living Document  

---

## 1. Project Purpose & Scope

STREAMCHAT is an academic and practical software project for the **Network Application Development** course at Ho Chi Minh City University of Technology (ĐHBK TP.HCM / HCMUT).

The primary goal is to build a self-hosted, distributed collaboration system over LAN/VPN featuring:
1. **Real-time Messaging:** Global broadcast chat and private 1-to-1 unicast chat.
2. **Active User Discovery & Presence:** Live tracking of connected users and status updates.
3. **Reliable File Transfer:** Zero-corruption transfer of files of arbitrary types and sizes over a custom UDP protocol.

### Core Architectural Constraints (Course Requirements)
- **Strictly No High-Level Networking Frameworks:** Standard library `socket` only. High-level frameworks (Flask, Django, FastAPI) and WebSockets are strictly prohibited.
- **Custom Transport over UDP:** Application logic must run on top of a custom, reliable, connection-oriented UDP transport layer (inspired by Google QUIC and Steam GameNetworkingSockets).
- **Transport Mechanisms Required:** Monotonic packet sequencing, sliding-window flow control, Selective ACK (SACK), RTT/RTO measurement (RFC 6298), congestion control (CUBIC), elimination of Head-of-Line (HoL) blocking across streams, and payload cryptography (AES-GCM).
- **Client UI:** Built with raw TCP socket HTTP/1.1 and Server-Sent Events (SSE) with vanilla HTML/CSS/JavaScript (no external UI frameworks).

---

## 2. Technology Stack

- **Language:** Python 3.8+ (Verified on Python 3.12).
- **Dependencies (`requirements.txt`):** `cryptography>=41.0.0` (Only external dependency).
- **Operating Systems:** Cross-platform (Windows, Linux, macOS).
- **Concurrency:** Non-blocking I/O with `selectors`, Python standard `threading`, and thread-safe queues.
- **Data Persistence:** SQLite (`users.db`) for user accounts and SHA-256 credentials.

---

## 3. Actual Repository Structure

```text
ComputerNetwork_Assignment1/
├── docs/
│   └── project/
│       ├── PROJECT_CONTEXT.md          # This context baseline document
│       ├── PROGRESS.md                 # Shared progress tracker across M1-M4
│       ├── IMPLEMENTATION_PLAN.md      # Dependency-aware 8-phase execution plan
│       ├── DECISIONS.md                # Architecture and design decision log
│       └── M2_IMPLEMENTATION_GUIDE.md  # Deep technical guide for M2 (Phước)
├── src/
│   ├── common/
│   │   ├── constants.py                # Protocol opcodes, flags, header layout, limits
│   │   ├── crypto.py                   # X25519 ECDH, HKDF-SHA256, AES-256-GCM
│   │   └── packet.py                   # 22-byte binary header serialization/deserialization
│   ├── transport/
│   │   ├── connection.py               # Stateful UDP connection, handshake, heartbeat, dispatch
│   │   ├── reliability.py              # SACK, RFC 6298 RTT/RTO, CUBIC congestion control
│   │   └── stream.py                   # Multi-stream framing, chunking, and reassembly
│   ├── server/
│   │   ├── database.py                 # SQLite database helper for user accounts
│   │   ├── server.py                   # Central UDP server with selector event loop
│   │   ├── session.py                  # Client session handling, auth, message routing
│   │   └── web_server.py               # [CONCURRENT/CONFLICT] Monolithic HTTP/SSE server (port 8080)
│   └── client/
│       ├── client.py                   # UDP ChatClient engine (handshake, transfers, event queue)
│       ├── http_ui.py                  # Raw TCP HTTP server bridging Web UI & ChatClient via SSE
│       └── static/                     # Web UI assets (index.html, style.css, app.js)
├── admin_server.py                     # Standalone admin Web portal (http.server + SQLite)
├── run_server.py                       # Server entry point
├── run_client.py                       # Client entry point
├── run_all.py                          # Multi-process orchestrator & syntax validation tool
├── users.db                            # SQLite database containing seeded test users
├── GENERAL.md                          # Phase 1 reconnaissance summary report
└── SKILL.md                            # Development behavioral rules (Karpathy guidelines)
```

---

## 4. Module Ownership & Team Responsibilities

| Role | Member | Primary Modules | Focus Areas |
| :--- | :--- | :--- | :--- |
| **M1** | **Trung** (`trung.phamnghia`) | `constants.py`, `packet.py`, `connection.py` | Binary packet framing, UDP socket lifecycle, packet dispatch, handshake state machine. |
| **M2** | **Phước** | `crypto.py`, `reliability.py`, `stream.py` | ECDH/AEAD cryptography, SACK, RTO (RFC 6298), CUBIC congestion control, stream framing/assembly. |
| **M3** | **Toàn** | `server.py`, `session.py` | UDP server event loop (`selectors`), session management, authentication, routing (broadcast/unicast). |
| **M4** | **Bảo** (`MemBau`) | `client.py`, `http_ui.py`, `static/` | Client network worker, raw TCP HTTP/SSE bridge, frontend Web UI, file upload dialog. |

---

## 5. Protocol Specification & Binary Layout

### 5.1. Binary Header Layout (22 Bytes, Big-Endian Network Byte Order)

Header format string: `!BIIBHIHI` (defined in [`src/common/packet.py`](file:///home/mpman123/Mine/BACH KHOA/BACH KHOA-CHUAN/BACH KHOA-BAI TAP/Networking/ComputerNetwork_Assignment1/src/common/packet.py#L22))

```text
 0                   1                   2                   3
 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|   msg_type    |                   conn_id                     |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|     conn_id (cont.)           |           seq_num             |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|     seq_num (cont.)           |     flags     |   stream_id   |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|   stream_id   |                    offset                     |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|     offset (cont.)            |          payload_len          |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                            reserved                           |
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                       Payload Data (Variable)                ...
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
```

- `msg_type` (1B, uint8): `0x01` (HELLO), `0x02` (HELLO_ACK), `0x03` (DATA), `0x04` (ACK), `0x05` (HEARTBEAT), `0x06` (HEARTBEAT_ACK), `0x07` (CLOSE).
- `conn_id` (4B, uint32): Random session identifier (generated by client).
- `seq_num` (4B, uint32): Monotonically increasing packet sequence number.
- `flags` (1B, uint8): Bitmask: `0x01` (FLAG_EOM), `0x02` (FLAG_FIN), `0x04` (FLAG_SYN), `0x08` (FLAG_ENCRYPTED).
- `stream_id` (2B, uint16): 0 = Control/Chat stream, Odd = Client initiated streams, Even = Server initiated.
- `offset` (4B, uint32): Byte offset of the payload chunk within the logical stream.
- `payload_len` (2B, uint16): Byte length of the accompanying payload.
- `reserved` (4B, uint32): Reserved for alignment/future use (currently 0).

### 5.2. MTU and Payload Constraints
- `MAX_UDP_PAYLOAD`: 1400 bytes (Safe Ethernet/Internet MTU boundary to avoid IP fragmentation).
- `HEADER_SIZE`: 22 bytes.
- `AEAD_TAG_SIZE`: 16 bytes (AES-GCM tag appended to encrypted ciphertext).
- **Correct Max Application Plaintext Chunk:** $1400 - 22 - 16 = \mathbf{1362 \text{ bytes}}$.  
  *(Note: `constants.py` currently sets `MAX_APP_PAYLOAD = 1378`, which leads to an over-MTU packet of 1416 bytes when encrypted).*

---

## 6. Current Verified Project State

- **Branch Status:** On branch `main` at commit `ad9b95f`. Working tree is clean.
- **Architectural Disconnect:**
  - On `main`, `web_server.py` runs on TCP 8080 and handles login, chat, and files directly over HTTP REST and SSE. The entire UDP stack (`connection.py`, `reliability.py`, `stream.py`, `client.py`) is bypassed by the browser UI.
  - On remote branch `origin/membau` (commit `7d348b7`), Bảo attempted to revert back to pure UDP by making `http_ui.py` bridge between Web UI and `ChatClient`. This branch is unmerged.
- **Test Suite Status:** **0% automated tests.** No unit tests or integration test scripts currently exist in the repository.
- **Compilation/Syntax:** Verified clean across all Python files using `python run_all.py --check`.

---

## 7. Known Critical Bugs & Blockers

1. **[CRITICAL] Nonce Reuse in AES-GCM (`crypto.py` + `connection.py`):**
   Client and Server share the same `session_key` and both derive the 12-byte nonce from `seq_num` starting from 0. The first packet in each direction shares the identical key and nonce, destroying AES-GCM security.
2. **[CRITICAL] Buffer Wipe Data Loss (`stream.py`):**
   `_try_assemble()` calls `self._recv_buffer.clear()` upon completing an assembly. Any out-of-order chunks belonging to subsequent messages already in the buffer are deleted.
3. **[CRITICAL] Duplicate Chunk Deadlock (`stream.py`):**
   Retransmitted duplicate chunks with `offset < _expected_offset` cause `sorted_offsets[0] != current` to trigger, permanently returning `None` and deadlocking the stream.
4. **[HIGH] CUBIC Multi-Loss Window Collapse (`reliability.py`):**
   When multiple packets expire in a single tick, `update_cwnd(is_loss=True)` is called once per lost packet, exponentially slashing `cwnd` down to the minimum window.
5. **[HIGH] Missing Flow Control Enforcement (`connection.py`):**
   `send_packet()` never queries `reliability.can_send()`, blasting UDP packets directly to the socket regardless of congestion window state.

---

## 8. Project Navigation & Related Documents

- [**Shared Progress Tracker**](file:///home/mpman123/Mine/BACH%20KHOA/BACH%20KHOA-CHUAN/BACH%20KHOA-BAI%20TAP/Networking/ComputerNetwork_Assignment1/docs/project/PROGRESS.md): Live task table across all four members (M1–M4).
- [**Implementation Plan**](file:///home/mpman123/Mine/BACH%20KHOA/BACH%20KHOA-CHUAN/BACH%20KHOA-BAI%20TAP/Networking/ComputerNetwork_Assignment1/docs/project/IMPLEMENTATION_PLAN.md): 8-phase execution roadmap with strict acceptance criteria and Definitions of Done.
- [**Architecture Decisions Log**](file:///home/mpman123/Mine/BACH%20KHOA/BACH%20KHOA-CHUAN/BACH%20KHOA-BAI TAP/Networking/ComputerNetwork_Assignment1/docs/project/DECISIONS.md): Architectural decisions, trade-offs, and consensus history.
- [**M2 Implementation Guide**](file:///home/mpman123/Mine/BACH%20KHOA/BACH%20KHOA-CHUAN/BACH%20KHOA-BAI%20TAP/Networking/ComputerNetwork_Assignment1/docs/project/M2_IMPLEMENTATION_GUIDE.md): Technical deep-dive, step-by-step code instructions, and test harness design for Phước.
- [**Engineering Guidelines (`SKILL.md`)**](file:///home/mpman123/Mine/BACH%20KHOA/BACH%20KHOA-CHUAN/BACH%20KHOA-BAI%20TAP/Networking/ComputerNetwork_Assignment1/SKILL.md): Behavioral rules for surgical, goal-driven development.
