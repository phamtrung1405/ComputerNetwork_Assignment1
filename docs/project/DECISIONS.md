# STREAMCHAT — Architecture Decision Log

**Status Values:**
- `PROPOSED`: Under active consideration; awaiting team review or experimental verification.
- `ACCEPTED`: Formally approved and implemented as the architectural baseline.
- `REJECTED`: Formally discarded due to technical or course constraint violations.
- `SUPERSEDED`: Previously accepted, but replaced by a subsequent decision.

---

## Master Decision Table

| ID | Date | Decision | Rationale | Status | Owner | Affected Modules |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **ADR-001** | 2026-10-24 | Use custom 22-byte binary header over UDP for all streaming | Strict course requirement to avoid high-level frameworks and implement connection-oriented features over raw UDP. | `ACCEPTED` | M1 (Trung) | `constants.py`, `packet.py`, `connection.py` |
| **ADR-002** | 2026-10-24 | Use SQLite (`users.db`) for user credential storage | Replaces hardcoded in-memory dictionaries with a persistent, queryable data store across restarts. | `ACCEPTED` | M4 (Bảo) | `database.py`, `session.py`, `admin_server.py` |
| **ADR-003** | 2026-10-26 | Create centralized single-port HTTP/SSE server (`web_server.py`) | Intended to allow multiple browser tabs to connect to `http://127.0.0.1:8080` without managing multiple ports. | `REJECTED` | M4 (Bảo) | `web_server.py`, `run_server.py`, `run_client.py` |
| **ADR-004** | 2026-10-26 | Revert to distributed architecture with automatic port-hunting for Client Web UI | `web_server.py` completely bypassed the custom UDP transport stack. Auto port-hunting (8080, 8081...) allows multiple local clients while keeping 100% of data traffic on UDP port 9000. | `PROPOSED` | M4 (Bảo) | `run_client.py`, `run_server.py`, `run_all.py`, `http_ui.py` |
| **ADR-005** | 2026-10-27 | Derive separate `client_write_key` and `server_write_key` via HKDF | In AES-GCM, encrypting two different messages with the same key and nonce destroys confidentiality and authentication tag integrity. Separate directional subkeys eliminate cross-directional nonce collisions. | `ACCEPTED` | M2 (Phước) | `crypto.py`, `connection.py` |
| **ADR-006** | 2026-10-27 | Bind 22-byte packet header into AEAD Associated Data (AAD) | Currently `associated_data=None`. Tampering with packet metadata (`stream_id`, `offset`, `flags`) in transit is not caught by AES-GCM. Binding header bytes ensures end-to-end metadata integrity. | `ACCEPTED` | M2 (Phước) | `crypto.py`, `connection.py` |
| **ADR-007** | 2026-10-27 | Adjust `MAX_APP_PAYLOAD` from 1378 bytes to 1362 bytes | 1378B plaintext + 16B GCM tag + 22B Header = 1416B, exceeding the 1400B MTU limit. 1362B ensures the total UDP datagram never exceeds 1400 bytes. | `ACCEPTED` | M2 (Phước) | `constants.py`, `stream.py` |
| **ADR-008** | 2026-10-27 | Refactor `Stream._try_assemble()` to use selective offset pruning | `_recv_buffer.clear()` wiped out subsequent out-of-order chunks that had already arrived. Deleting only chunks with $offset < expected\_offset$ preserves future chunks and eliminates stream corruption. | `ACCEPTED` | M2 (Phước) | `stream.py` |
| **ADR-009** | 2026-10-27 | Enforce maximum of 1 CUBIC congestion window reduction per RTT | Slashing $cwnd$ on every expired packet inside a multi-loss tick causes catastrophic window collapse ($0.7^N$). Standard CUBIC limits backoff to once per loss epoch. | `ACCEPTED` | M2 (Phước) | `reliability.py` |
| **ADR-010** | 2026-10-27 | Postpone ACK dispatch until after successful AEAD tag decryption | Sending an ACK before calling `decrypt()` acknowledges corrupted or tampered datagrams, causing the sender to drop them from retransmission while the receiver discards the payload. | `PROPOSED` | M2 (Phước) | `connection.py` |

---

## Decision Details & Context

### ADR-003 vs. ADR-004: The Single-Port vs. Distributed UDP Conflict
- **Context:** In commit `bde7d5c` and `ad9b95f`, M4 implemented `src/server/web_server.py` to allow all users to open `http://127.0.0.1:8080`. However, this implementation directly handled `/api/send` and `/api/file` over raw TCP sockets, bypassing `ChatServer` and `Connection` UDP sockets entirely.
- **Consequence:** The project failed the primary course requirement of implementing a custom transport protocol over UDP.
- **Resolution (ADR-004):** Remote branch `origin/membau` (commit `7d348b7`) introduced auto port-hunting in `run_client.py` where each client runs a lightweight raw TCP HTTP UI (port 8080 for Alice, 8081 for Bob) which bridges browser requests directly into `ChatClient` over UDP port 9000. ADR-004 supersedes ADR-003 once merged.

### ADR-005: Elimination of AES-GCM Nonce Catastrophe
- **Context:** `packet_num` (starting at 0) was used as the 12-byte nonce for both Client and Server with the same 32-byte session key.
- **Security Assessment:** Critical cryptographic vulnerability. If both parties transmit their first data packet with `seq=0`, both use Nonce `0x000000000000000000000000`. In Galois/Counter Mode (GCM), encrypting two plaintexts with the same key and nonce reveals the plaintext XOR ($P_1 \oplus P_2 = C_1 \oplus C_2$) and allows universal forgery of the GMAC tag.
- **Resolution:** Expand HKDF info parameter to derive two separate 32-byte keys from the shared ECDH secret:
  - `client_write_key = HKDF(info=b"streamchat-client-write-key", length=32)`
  - `server_write_key = HKDF(info=b"streamchat-server-write-key", length=32)`
