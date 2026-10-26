"""
server/web_server.py
~~~~~~~~~~~~~~~~~~~~
Máy chủ Web và WebSocket tích hợp trên một cổng duy nhất (mặc định 8080)
sử dụng raw TCP socket theo kiến trúc Client-Server chuẩn.

Đặc điểm:
- Không sử dụng framework ngoài (tuân thủ quy định bài tập lớn mạng máy tính).
- Thực hiện đầy đủ đặc tả WebSocket RFC 6455 (bắt tay HTTP 101, frame text/ping/pong/close, mask/unmask).
- Quản lý đồng thời hàng trăm tài khoản người dùng độc lập trên cùng một địa chỉ:cổng.
- Định tuyến tin nhắn phòng chung (broadcast) và tin nhắn riêng tư (unicast).
- Hỗ trợ truyền và tải tệp tin thời gian thực.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import mimetypes
import os
from pathlib import Path
import socket
import struct
import threading
import time
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qs, unquote, urlsplit
import uuid

from server.database import db as user_db

logger = logging.getLogger(__name__)

# Thư mục gốc dự án và các thư mục tĩnh / upload
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
STATIC_DIR = Path(__file__).resolve().parent.parent / "client" / "static"
UPLOADS_DIR = PROJECT_ROOT / "uploads"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def recv_exact(sock: socket.socket, num_bytes: int) -> bytes:
    """Đọc chính xác số byte yêu cầu từ socket TCP.

    Args:
        sock: Socket TCP.
        num_bytes: Số byte cần đọc.

    Returns:
        Dữ liệu bytes đã đọc.

    Raises:
        ConnectionResetError nếu kết nối bị đóng trước khi đọc đủ.
    """
    buf = bytearray()
    while len(buf) < num_bytes:
        chunk = sock.recv(num_bytes - len(buf))
        if not chunk:
            raise ConnectionResetError("Socket đóng bất ngờ khi đang nhận dữ liệu")
        buf.extend(chunk)
    return bytes(buf)


def make_ws_accept_key(sec_websocket_key: str) -> str:
    """Tạo giá trị Sec-WebSocket-Accept theo chuẩn RFC 6455."""
    sha1 = hashlib.sha1((sec_websocket_key.strip() + WS_GUID).encode("utf-8")).digest()
    return base64.b64encode(sha1).decode("utf-8")


def encode_ws_frame(message: str, opcode: int = 0x1) -> bytes:
    """Đóng gói frame WebSocket từ Server gửi tới Client (không che/unmasked).

    Args:
        message: Nội dung văn bản UTF-8 (nếu opcode=0x1).
        opcode: Mã opcode (0x1: text, 0x9: ping, 0xA: pong, 0x8: close).

    Returns:
        Dữ liệu nhị phân frame RFC 6455.
    """
    payload = message.encode("utf-8") if isinstance(message, str) else message
    length = len(payload)

    if length < 126:
        header = struct.pack("!BB", 0x80 | opcode, length)
    elif length <= 65535:
        header = struct.pack("!BBH", 0x80 | opcode, 126, length)
    else:
        header = struct.pack("!BBQ", 0x80 | opcode, 127, length)

    return header + payload


class WebSocketClient:
    """Đại diện cho một phiên kết nối WebSocket độc lập từ trình duyệt."""

    def __init__(self, sock: socket.socket, addr: Tuple[str, int]) -> None:
        self.sock = sock
        self.addr = addr
        self.username: Optional[str] = None
        self.authenticated: bool = False
        self.connected_at: float = time.time()
        self._send_lock = threading.Lock()
        self.is_closed: bool = False

    def send_text(self, text: str) -> bool:
        """Gửi text frame đến client qua WebSocket."""
        if self.is_closed:
            return False
        try:
            frame = encode_ws_frame(text, opcode=0x1)
            with self._send_lock:
                self.sock.sendall(frame)
            return True
        except OSError:
            self.close()
            return False

    def send_json(self, data: dict) -> bool:
        """Gửi dict dưới dạng JSON frame."""
        return self.send_text(json.dumps(data, ensure_ascii=False))

    def close(self) -> None:
        """Đóng kết nối client một cách an toàn."""
        if not self.is_closed:
            self.is_closed = True
            try:
                # Gửi close frame nếu có thể
                with self._send_lock:
                    self.sock.sendall(struct.pack("!BB", 0x88, 0))
            except Exception:
                pass
            try:
                self.sock.close()
            except Exception:
                pass


class UnifiedChatServer:
    """Máy chủ HTTP + WebSocket tập trung trên cổng 8080."""

    def __init__(self, host: str = "0.0.0.0", port: int = 8080) -> None:
        self.host = host
        self.port = port
        self.running = False
        self.sock: Optional[socket.socket] = None

        # Quản lý kết nối WebSocket: client_obj -> bool
        self._clients: Set[WebSocketClient] = set()
        self._clients_lock = threading.Lock()

        # Quản lý tệp tin tải lên: file_id -> info
        self._files_registry: Dict[str, dict] = {}
        self._files_lock = threading.Lock()

    def start(self) -> None:
        """Khởi tạo socket TCP và bắt đầu lắng nghe kết nối."""
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((self.host, self.port))
        self.sock.listen(128)
        self.running = True

        display_host = "127.0.0.1" if self.host == "0.0.0.0" else self.host
        print("=" * 65)
        print("   STREAMCHAT UNIFIED SERVER (HTTP & WEBSOCKET)")
        print(f"   * Dia chi truy cap Web UI: http://{display_host}:{self.port}")
        print(f"   * WebSocket Endpoint:     ws://{display_host}:{self.port}/ws")
        print("   * Co the mo dong thoi hang tram tab/acc tren cung port 8080!")
        print("=" * 65)

        try:
            while self.running:
                try:
                    client_sock, addr = self.sock.accept()
                except OSError:
                    if not self.running:
                        break
                    continue

                threading.Thread(
                    target=self._handle_connection,
                    args=(client_sock, addr),
                    daemon=True,
                ).start()
        except KeyboardInterrupt:
            logger.info("Server đang tắt...")
        finally:
            self.stop()

    def stop(self) -> None:
        """Dừng server và đóng tất cả kết nối."""
        self.running = False
        with self._clients_lock:
            for client in list(self._clients):
                client.close()
            self._clients.clear()
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass

    # ─────────────────── Quản lý người dùng ───────────────────

    def get_online_users(self) -> List[str]:
        """Lấy danh sách các username đang trực tuyến."""
        with self._clients_lock:
            users = {
                client.username
                for client in self._clients
                if client.authenticated and client.username
            }
        return sorted(list(users))

    def broadcast_event(self, event: dict, exclude: Optional[WebSocketClient] = None) -> None:
        """Gửi sự kiện đến tất cả các client đã xác thực."""
        with self._clients_lock:
            target_clients = [
                c for c in self._clients
                if c.authenticated and c != exclude and not c.is_closed
            ]
        for client in target_clients:
            client.send_json(event)

    def send_to_user(self, target_username: str, event: dict) -> bool:
        """Gửi sự kiện đến các kết nối của một user cụ thể (unicast)."""
        sent = False
        with self._clients_lock:
            target_clients = [
                c for c in self._clients
                if c.authenticated and c.username == target_username and not c.is_closed
            ]
        for client in target_clients:
            if client.send_json(event):
                sent = True
        return sent

    def _delayed_close_client(self, client: WebSocketClient, delay: float = 1.0) -> None:
        """Đóng kết nối client một cách an toàn sau khi đã gửi thông báo."""
        time.sleep(delay)
        client.close()

    # ─────────────────── Xử lý kết nối TCP ───────────────────

    def _handle_connection(self, client_sock: socket.socket, addr: Tuple[str, int]) -> None:
        """Xử lý phân loại request: WebSocket Handshake hoặc HTTP Request."""
        try:
            client_sock.settimeout(30.0)
            try:
                client_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                client_sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 262144)
                client_sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 262144)
            except OSError:
                pass
            raw_data = bytearray()
            # Đọc đến khi thấy ranh giới headers \r\n\r\n
            while b"\r\n\r\n" not in raw_data:
                chunk = client_sock.recv(4096)
                if not chunk:
                    client_sock.close()
                    return
                raw_data.extend(chunk)
                if len(raw_data) > 65536:  # Giới hạn kích thước header 64KB
                    client_sock.close()
                    return

            header_bytes, body_prefix = raw_data.split(b"\r\n\r\n", 1)
            header_text = header_bytes.decode("iso-8859-1")
            lines = header_text.split("\r\n")
            if not lines:
                client_sock.close()
                return

            req_line = lines[0].split(" ")
            if len(req_line) < 2:
                client_sock.close()
                return

            method = req_line[0].upper()
            full_path = req_line[1]

            headers: Dict[str, str] = {}
            for line in lines[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip().lower()] = v.strip()

            url_parts = urlsplit(full_path)
            path = url_parts.path
            query_params = parse_qs(url_parts.query)

            # Kiểm tra xem có phải yêu cầu nâng cấp WebSocket không
            is_ws = (
                headers.get("upgrade", "").lower() == "websocket"
                and "sec-websocket-key" in headers
            )

            if is_ws and path == "/ws":
                self._handle_websocket_handshake(
                    client_sock, addr, headers["sec-websocket-key"]
                )
            else:
                self._handle_http_request(
                    client_sock, method, path, query_params, headers, body_prefix
                )
        except Exception as exc:
            logger.debug("Loi xu ly ket noi tu %s: %s", addr, exc)
            try:
                client_sock.close()
            except Exception:
                pass

    # ─────────────────── Xử lý WebSocket ───────────────────

    def _handle_websocket_handshake(
        self, client_sock: socket.socket, addr: Tuple[str, int], sec_key: str
    ) -> None:
        """Thực hiện bắt tay WebSocket RFC 6455."""
        accept_key = make_ws_accept_key(sec_key)
        response = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept_key}\r\n\r\n"
        )
        client_sock.sendall(response.encode("utf-8"))
        client_sock.settimeout(None)  # Bỏ timeout cho WebSocket lâu dài

        client = WebSocketClient(client_sock, addr)
        with self._clients_lock:
            self._clients.add(client)

        logger.info("[WS] Client moi ket noi tu %s:%d", addr[0], addr[1])
        try:
            self._websocket_loop(client)
        finally:
            self._disconnect_client(client)

    def _websocket_loop(self, client: WebSocketClient) -> None:
        """Vòng lặp đọc các frames từ kết nối WebSocket."""
        sock = client.sock
        while self.running and not client.is_closed:
            try:
                head = recv_exact(sock, 2)
            except (ConnectionResetError, OSError):
                break

            fin = bool(head[0] & 0x80)
            opcode = head[0] & 0x0F
            has_mask = bool(head[1] & 0x80)
            payload_len = head[1] & 0x7F

            if payload_len == 126:
                ext = recv_exact(sock, 2)
                payload_len = struct.unpack("!H", ext)[0]
            elif payload_len == 127:
                ext = recv_exact(sock, 8)
                payload_len = struct.unpack("!Q", ext)[0]

            mask_key = recv_exact(sock, 4) if has_mask else None
            payload_bytes = recv_exact(sock, payload_len) if payload_len > 0 else b""

            if mask_key:
                unmasked = bytearray(payload_len)
                for i in range(payload_len):
                    unmasked[i] = payload_bytes[i] ^ mask_key[i % 4]
                payload_bytes = bytes(unmasked)

            # Xử lý theo opcode
            if opcode == 0x8:  # Close frame
                client.close()
                break
            elif opcode == 0x9:  # Ping frame
                pong = encode_ws_frame(payload_bytes, opcode=0xA)
                with client._send_lock:
                    sock.sendall(pong)
            elif opcode == 0x1:  # Text frame (JSON message)
                try:
                    text_msg = payload_bytes.decode("utf-8")
                    data = json.loads(text_msg)
                    self._handle_client_message(client, data)
                except Exception as exc:
                    logger.debug("Loi doc JSON tu client: %s", exc)

    def _handle_client_message(self, client: WebSocketClient, data: dict) -> None:
        """Xử lý các thông điệp JSON nhận được từ Client WebSocket."""
        msg_type = data.get("type")

        # 1. Đăng nhập
        if msg_type == "login":
            username = str(data.get("username", "")).strip().lower()
            password = str(data.get("password", "")).strip()

            if not username or not password:
                client.send_json({
                    "type": "AUTH_RESP",
                    "success": False,
                    "message": "Vui lòng nhập đầy đủ tên đăng nhập và mật khẩu.",
                })
                return

            p_hash = hashlib.sha256(password.encode("utf-8")).hexdigest()
            if user_db.verify_user(username, p_hash):
                # ── Kiểm tra và kick session cũ nếu user đã đăng nhập ở session khác ──
                with self._clients_lock:
                    old_sessions = [
                        c for c in self._clients
                        if c != client and c.authenticated and c.username == username and not c.is_closed
                    ]
                    for old_c in old_sessions:
                        old_c.authenticated = False
                        old_c.username = None

                for old_c in old_sessions:
                    logger.info("[AUTH] Kick session cu cua user '%s' tai %s", username, old_c.addr)
                    old_c.send_json({
                        "type": "KICKED",
                        "message": "Tài khoản của bạn đã được đăng nhập từ một thiết bị hoặc tab khác.",
                    })
                    threading.Thread(
                        target=self._delayed_close_client,
                        args=(old_c, 1.0),
                        daemon=True,
                    ).start()

                client.username = username
                client.authenticated = True

                client.send_json({
                    "type": "AUTH_RESP",
                    "success": True,
                    "username": username,
                    "message": f"Chào mừng {username}! Đăng nhập thành công.",
                })

                # Gửi danh sách người dùng đang trực tuyến
                online_users = self.get_online_users()
                self.broadcast_event({
                    "type": "USER_LIST",
                    "users": online_users,
                })

                # Thông báo hệ thống có người mới vào phòng
                self.broadcast_event({
                    "type": "SYSTEM",
                    "message": f"✨ Người dùng @{username} vừa tham gia phòng chat.",
                }, exclude=client)
                logger.info("[AUTH] User '%s' dang nhap thanh cong tu %s", username, client.addr)
            else:
                client.send_json({
                    "type": "AUTH_RESP",
                    "success": False,
                    "message": "Tên đăng nhập hoặc mật khẩu không chính xác.",
                })

        # 2. Gửi tin nhắn chat
        elif msg_type == "send":
            if not client.authenticated or not client.username:
                client.send_json({
                    "type": "ERROR",
                    "message": "Bạn chưa đăng nhập.",
                })
                return

            target = str(data.get("target", "all")).strip().lower()
            text = str(data.get("text", "")).strip()
            if not text:
                return

            timestamp = time.time()

            if target in ("all", "", "phong_chung"):
                # Broadcast tin nhắn cho toàn bộ người dùng đang online
                chat_event = {
                    "type": "MSG",
                    "from": client.username,
                    "target": "all",
                    "text": text,
                    "timestamp": timestamp,
                    "is_private": False,
                }
                self.broadcast_event(chat_event)
            else:
                # Gửi tin nhắn riêng (Unicast)
                private_event = {
                    "type": "MSG",
                    "from": client.username,
                    "target": target,
                    "text": text,
                    "timestamp": timestamp,
                    "is_private": True,
                }
                sent = self.send_to_user(target, private_event)
                # Echo lại cho chính người gửi để hiển thị trên giao diện
                client.send_json(private_event)

                if not sent:
                    client.send_json({
                        "type": "ERROR",
                        "message": f"Người dùng @{target} hiện không trực tuyến.",
                    })

        # 3. Yêu cầu lấy danh sách người dùng online
        elif msg_type == "get_users":
            client.send_json({
                "type": "USER_LIST",
                "users": self.get_online_users(),
            })

    def _disconnect_client(self, client: WebSocketClient) -> None:
        """Dọn dẹp khi một client ngắt kết nối WebSocket."""
        client.close()
        with self._clients_lock:
            self._clients.discard(client)

        if client.authenticated and client.username:
            username = client.username
            # Kiểm tra xem user này còn tab/thiết bị nào khác đang mở không
            still_online = any(
                c.username == username and c.authenticated and not c.is_closed
                for c in self._clients
            )
            if not still_online:
                self.broadcast_event({
                    "type": "USER_LIST",
                    "users": self.get_online_users(),
                })
                self.broadcast_event({
                    "type": "SYSTEM",
                    "message": f"🔌 Người dùng @{username} đã rời phòng chat.",
                })
            logger.info("[WS] Client '%s' da ngat ket noi", username)

    # ─────────────────── Xử lý HTTP thường ───────────────────

    def _handle_http_request(
        self,
        sock: socket.socket,
        method: str,
        path: str,
        query_params: dict,
        headers: dict,
        body_prefix: bytes,
    ) -> None:
        """Phục vụ file tĩnh và API upload/download."""
        try:
            content_length = int(headers.get("content-length", 0))

            # Phục vụ trang chủ
            if method == "GET" and path in ("/", "/index.html"):
                self._serve_file(sock, STATIC_DIR / "index.html")

            # Phục vụ tài nguyên tĩnh (CSS, JS, Fonts, Ảnh)
            elif method == "GET" and path.startswith("/static/"):
                rel_path = path[len("/static/"):].lstrip("/")
                target_file = (STATIC_DIR / rel_path).resolve()
                if STATIC_DIR in target_file.parents and target_file.is_file():
                    self._serve_file(sock, target_file)
                else:
                    self._send_http_response(sock, 404, "text/plain", b"File not found")

            # Tải tệp tin
            elif method == "GET" and path == "/api/download":
                file_id = query_params.get("id", [""])[0]
                with self._files_lock:
                    file_info = self._files_registry.get(file_id)

                if file_info and Path(file_info["path"]).is_file():
                    filepath = Path(file_info["path"])
                    filename = file_info["filename"]
                    size = filepath.stat().st_size
                    custom_headers = {
                        "Content-Disposition": f'attachment; filename="{filename}"',
                        "Content-Length": str(size),
                    }
                    self._serve_file(sock, filepath, custom_headers=custom_headers)
                else:
                    self._send_http_response(sock, 404, "text/plain", b"File not found or expired")

            # Trạng thái server
            elif method == "GET" and path == "/api/status":
                data = {
                    "status": "online",
                    "online_users": self.get_online_users(),
                    "total_clients": len(self._clients),
                }
                body = json.dumps(data).encode("utf-8")
                self._send_http_response(sock, 200, "application/json", body)

            # Upload tệp tin qua HTTP POST
            elif method == "POST" and path == "/api/file":
                self._handle_file_upload(sock, query_params, headers, body_prefix, content_length)

            else:
                self._send_http_response(sock, 404, "text/plain", b"Not Found")
        finally:
            try:
                sock.close()
            except Exception:
                pass

    def _handle_file_upload(
        self,
        sock: socket.socket,
        query_params: dict,
        headers: dict,
        body_prefix: bytes,
        content_length: int,
    ) -> None:
        """Đọc và lưu file nhị phân được upload từ trình duyệt."""
        filename = query_params.get("filename", [""])[0] or headers.get("x-filename", "")
        filename = unquote(filename) if filename else "file_transfer"
        safe_filename = os.path.basename(filename) or "file_transfer"

        target = query_params.get("target", ["all"])[0] or headers.get("x-target", "all")
        target = unquote(target).strip().lower()

        sender = query_params.get("sender", [""])[0] or headers.get("x-sender", "")
        sender = unquote(sender).strip().lower()

        file_id = str(uuid.uuid4())[:8]
        dest_filename = f"{file_id}_{safe_filename}"
        dest_path = UPLOADS_DIR / dest_filename

        # Đọc toàn bộ body file nhị phân
        with open(dest_path, "wb") as f:
            if body_prefix:
                f.write(body_prefix)
            bytes_received = len(body_prefix)
            while bytes_received < content_length:
                chunk_size = min(131072, content_length - bytes_received)
                chunk = sock.recv(chunk_size)
                if not chunk:
                    break
                f.write(chunk)
                bytes_received += len(chunk)

        file_size = dest_path.stat().st_size
        with self._files_lock:
            self._files_registry[file_id] = {
                "file_id": file_id,
                "filename": safe_filename,
                "path": str(dest_path),
                "size": file_size,
                "sender": sender or "Ẩn danh",
                "target": target,
            }

        # Bắn thông báo thời gian thực qua WebSocket đến người nhận
        file_event = {
            "type": "FILE_READY",
            "stream_id": file_id,
            "from": sender or "Một người dùng",
            "target": target,
            "filename": safe_filename,
            "size": file_size,
            "download_url": f"/api/download?id={file_id}",
            "timestamp": time.time(),
        }

        if target in ("all", "", "phong_chung"):
            self.broadcast_event(file_event)
        else:
            self.send_to_user(target, file_event)
            # Gửi cho người gửi
            if sender:
                self.send_to_user(sender, file_event)

        resp_data = {
            "status": "ok",
            "stream_id": file_id,
            "filename": safe_filename,
            "size": file_size,
            "download_url": f"/api/download?id={file_id}",
        }
        self._send_http_response(
            sock, 200, "application/json", json.dumps(resp_data).encode("utf-8")
        )

    def _serve_file(
        self,
        sock: socket.socket,
        filepath: Path,
        custom_headers: Optional[Dict[str, str]] = None,
    ) -> None:
        """Gửi nội dung file tĩnh với MIME type thích hợp."""
        mime, _ = mimetypes.guess_type(str(filepath))
        if mime is None:
            mime = "application/octet-stream"

        try:
            with open(filepath, "rb") as f:
                content = f.read()
            self._send_http_response(sock, 200, mime, content, custom_headers=custom_headers)
        except OSError:
            self._send_http_response(sock, 404, "text/plain", b"File read error")

    def _send_http_response(
        self,
        sock: socket.socket,
        status_code: int,
        content_type: str,
        body: bytes,
        custom_headers: Optional[Dict[str, str]] = None,
    ) -> None:
        """Tạo và gửi HTTP response chuẩn."""
        status_text = {
            200: "OK",
            404: "Not Found",
            400: "Bad Request",
            500: "Internal Server Error",
        }.get(status_code, "OK")

        headers = [
            f"HTTP/1.1 {status_code} {status_text}",
            f"Content-Type: {content_type}; charset=utf-8" if "text" in content_type or "json" in content_type else f"Content-Type: {content_type}",
            f"Content-Length: {len(body)}",
            "Access-Control-Allow-Origin: *",
            "Connection: close",
        ]
        if custom_headers:
            for k, v in custom_headers.items():
                headers.append(f"{k}: {v}")

        header_bytes = "\r\n".join(headers).encode("utf-8") + b"\r\n\r\n"
        try:
            sock.sendall(header_bytes + body)
        except OSError:
            pass


def start_unified_server(host: str = "0.0.0.0", port: int = 8080) -> UnifiedChatServer:
    """Hàm tiện ích khởi chạy UnifiedChatServer."""
    server = UnifiedChatServer(host=host, port=port)
    server.start()
    return server


if __name__ == "__main__":
    start_unified_server()
