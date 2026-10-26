"""
client/http_ui.py
~~~~~~~~~~~~~~~~~
Máy chủ web nội bộ tĩnh sử dụng raw TCP socket.

Cấp file HTML/CSS/JS tĩnh, xử lý REST API (POST), và duy trì
kết nối Server-Sent Events (SSE) để đẩy dữ liệu real-time lên
trình duyệt.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import queue
import socket
import tempfile
import threading
import time
from typing import Optional
from urllib.parse import unquote, parse_qs, urlsplit
import uuid

from client.client import ChatClient
from common.constants import DEFAULT_HTTP_PORT

logger = logging.getLogger(__name__)

# Thư mục static
_STATIC_DIR: str = os.path.join(os.path.dirname(__file__), "static")


class HttpUI:
    """Máy chủ HTTP nội bộ dùng raw TCP socket.

    Phục vụ file tĩnh (HTML/CSS/JS) và cung cấp các endpoint:
    - ``GET /`` → index.html
    - ``GET /static/<path>`` → file tĩnh
    - ``GET /events`` → SSE stream
    - ``POST /api/login`` → đăng nhập
    - ``POST /api/send`` → gửi chat
    - ``POST /api/file`` → gửi file
    - ``GET /api/status`` → trạng thái
    - ``GET /api/progress`` → tiến trình file
    """

    def __init__(
        self,
        port: int = DEFAULT_HTTP_PORT,
        client: Optional[ChatClient] = None,
    ) -> None:
        """Khởi tạo socket TCP tại 127.0.0.1.

        Args:
            port: Cổng HTTP (mặc định 8080).
            client: Đối tượng ChatClient để giao tiếp.
        """
        self._port: int = port
        self._client: Optional[ChatClient] = client
        self._sock: Optional[socket.socket] = None
        self._running: bool = False
        self._sse_clients: list[socket.socket] = []
        self._sse_lock: threading.Lock = threading.Lock()

    def set_client(self, client: ChatClient) -> None:
        """Gán ChatClient sau khi khởi tạo.

        Args:
            client: Đối tượng ChatClient.
        """
        self._client = client

    # ─────────────────────── Start ───────────────────────

    def start(self) -> None:
        """Lắng nghe kết nối TCP từ trình duyệt.

        Chạy blocking – nên gọi trong thread riêng.
        """
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", self._port))
        self._sock.listen(16)
        self._sock.settimeout(1.0)
        self._running = True

        logger.info("HTTP UI tại http://127.0.0.1:%d", self._port)

        # Thread đẩy SSE events
        sse_thread = threading.Thread(target=self._sse_pump, daemon=True)
        sse_thread.start()

        while self._running:
            try:
                client_sock, addr = self._sock.accept()
                handler = threading.Thread(
                    target=self.handle_http_request,
                    args=(client_sock,),
                    daemon=True,
                )
                handler.start()
            except socket.timeout:
                continue
            except OSError:
                break

    # ─────────────────────── Request handler ─────────────────────

    def handle_http_request(self, client_socket: socket.socket) -> None:
        """Phân tích cú pháp HTTP GET/POST thô và phản hồi.

        Args:
            client_socket: Socket TCP đã accept.
        """
        try:
            client_socket.settimeout(15.0)
            header_buf = bytearray()
            while b"\r\n\r\n" not in header_buf:
                chunk = client_socket.recv(4096)
                if not chunk:
                    break
                header_buf.extend(chunk)

            if b"\r\n\r\n" not in header_buf:
                client_socket.close()
                return

            header_bytes, body_prefix = header_buf.split(b"\r\n\r\n", 1)
            header_text = header_bytes.decode("iso-8859-1")
            lines = header_text.split("\r\n")
            if not lines:
                client_socket.close()
                return

            request_line = lines[0]
            parts = request_line.split(" ")
            if len(parts) < 2:
                client_socket.close()
                return

            method = parts[0].upper()
            raw_path = parts[1]
            split_url = urlsplit(raw_path)
            path = unquote(split_url.path)
            query_params = parse_qs(split_url.query)

            headers: dict[str, str] = {}
            for line in lines[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip().lower()] = v.strip()

            content_length = int(headers.get("content-length", 0))
            content_type = headers.get("content-type", "")

            # CORS Preflight
            if method == "OPTIONS":
                self._send_response(client_socket, 204, "text/plain", b"")
                client_socket.close()
                return

            # ── Routing ──
            if method == "GET" and path == "/":
                self._serve_static(client_socket, "index.html")
            elif method == "GET" and path == "/events":
                self._handle_sse(client_socket)
                return  # SSE keeps the connection open
            elif method == "GET" and path.startswith("/static/"):
                filename: str = path[len("/static/"):]
                self._serve_static(client_socket, filename)
            elif method == "GET" and path == "/api/status":
                self._handle_status(client_socket)
            elif method == "GET" and path == "/api/progress":
                self._handle_progress(client_socket)
            elif method == "POST" and path == "/api/login":
                body = self._read_text_body(client_socket, body_prefix, content_length)
                self._handle_login(client_socket, body)
            elif method == "POST" and path == "/api/send":
                body = self._read_text_body(client_socket, body_prefix, content_length)
                self._handle_send(client_socket, body)
            elif method == "POST" and path == "/api/file":
                if content_type.startswith("application/json"):
                    body = self._read_text_body(client_socket, body_prefix, content_length)
                    self._handle_file_upload(client_socket, body)
                else:
                    self._handle_file_stream_upload(
                        client_socket, query_params, headers, body_prefix, content_length
                    )
            else:
                self._send_response(client_socket, 404, "text/plain", b"Not Found")
                client_socket.close()
        except Exception as exc:
            logger.debug("HTTP request error: %s", exc)
            try:
                client_socket.close()
            except OSError:
                pass

    def _read_text_body(
        self, client_socket: socket.socket, body_prefix: bytes, content_length: int
    ) -> str:
        """Đọc toàn bộ nội dung body dạng text UTF-8."""
        body_bytes = bytearray(body_prefix)
        while len(body_bytes) < content_length:
            to_read = min(4096, content_length - len(body_bytes))
            chunk = client_socket.recv(to_read)
            if not chunk:
                break
            body_bytes.extend(chunk)
        return body_bytes.decode("utf-8", errors="replace")

    # ─────────────────────── Static files ────────────────────────

    def _serve_static(
        self, client_socket: socket.socket, filename: str
    ) -> None:
        """Phục vụ file tĩnh từ thư mục static/.

        Args:
            client_socket: Socket client.
            filename: Tên file cần phục vụ.
        """
        filepath: str = os.path.join(_STATIC_DIR, filename)
        if not os.path.isfile(filepath):
            self._send_response(client_socket, 404, "text/plain", b"Not Found")
            client_socket.close()
            return

        content_type, _ = mimetypes.guess_type(filepath)
        if content_type is None:
            content_type = "application/octet-stream"

        with open(filepath, "rb") as f:
            content: bytes = f.read()

        self._send_response(client_socket, 200, content_type, content)
        client_socket.close()

    # ─────────────────────── SSE ───────────────────────

    def _handle_sse(self, client_socket: socket.socket) -> None:
        """Thiết lập kết nối Server-Sent Events.

        Args:
            client_socket: Socket client.
        """
        response_header: str = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: text/event-stream\r\n"
            "Cache-Control: no-cache\r\n"
            "Connection: keep-alive\r\n"
            "Access-Control-Allow-Origin: *\r\n"
            "\r\n"
        )
        try:
            client_socket.sendall(response_header.encode("utf-8"))
            # Gửi comment ban đầu để giữ kết nối
            client_socket.sendall(b": connected\n\n")
            with self._sse_lock:
                self._sse_clients.append(client_socket)
            logger.debug("SSE client kết nối")
        except OSError:
            try:
                client_socket.close()
            except OSError:
                pass

    def _sse_pump(self) -> None:
        """Thread liên tục đọc event_queue và đẩy SSE events."""
        while self._running:
            if self._client is None:
                time.sleep(0.1)
                continue

            try:
                event: dict = self._client.event_queue.get(timeout=0.5)
                self._broadcast_sse(event)
            except queue.Empty:
                # Gửi heartbeat SSE để giữ kết nối
                self._broadcast_sse_raw(": heartbeat\n\n")

    def _broadcast_sse(self, event: dict) -> None:
        """Gửi một event JSON đến tất cả SSE clients.

        Args:
            event: Dict event data.
        """
        data_str: str = json.dumps(event, ensure_ascii=False)
        sse_message: str = f"data: {data_str}\n\n"
        self._broadcast_sse_raw(sse_message)

    def _broadcast_sse_raw(self, message: str) -> None:
        """Gửi raw SSE message đến tất cả clients.

        Args:
            message: Chuỗi SSE đã format.
        """
        encoded: bytes = message.encode("utf-8")
        with self._sse_lock:
            dead: list[socket.socket] = []
            for sock in self._sse_clients:
                try:
                    sock.sendall(encoded)
                except OSError:
                    dead.append(sock)
            for sock in dead:
                self._sse_clients.remove(sock)
                try:
                    sock.close()
                except OSError:
                    pass

    # ─────────────────────── API handlers ────────────────────────

    def _handle_login(
        self, client_socket: socket.socket, body: str
    ) -> None:
        """Xử lý POST /api/login.

        Args:
            client_socket: Socket client.
            body: JSON body.
        """
        try:
            data: dict = json.loads(body)
            username: str = data.get("username", "")
            password: str = data.get("password", "")

            if self._client is not None:
                self._client.connect(username, password)
                resp: dict = {
                    "status": "connecting",
                    "message": f"Đang kết nối với tư cách {username}...",
                }
            else:
                resp = {"status": "error", "message": "Client chưa sẵn sàng"}

            self._send_json_response(client_socket, 200, resp)
        except json.JSONDecodeError:
            self._send_json_response(
                client_socket, 400, {"status": "error", "message": "JSON không hợp lệ"}
            )
        client_socket.close()

    def _handle_send(
        self, client_socket: socket.socket, body: str
    ) -> None:
        """Xử lý POST /api/send.

        Args:
            client_socket: Socket client.
            body: JSON body chứa ``target`` và ``text``.
        """
        try:
            data: dict = json.loads(body)
            target: str = data.get("target", "all")
            text: str = data.get("text", "")

            if self._client is not None and self._client.is_authenticated:
                self._client.send_chat(target, text)
                self._send_json_response(
                    client_socket, 200, {"status": "ok", "message": "Đã gửi"}
                )
            else:
                self._send_json_response(
                    client_socket, 401, {"status": "error", "message": "Chưa đăng nhập"}
                )
        except json.JSONDecodeError:
            self._send_json_response(
                client_socket, 400, {"status": "error", "message": "JSON không hợp lệ"}
            )
        client_socket.close()

    def _handle_file_upload(
        self, client_socket: socket.socket, body: str
    ) -> None:
        """Xử lý POST /api/file.

        Args:
            client_socket: Socket client.
            body: JSON body chứa ``file_path`` và ``target``.
        """
        try:
            data: dict = json.loads(body)
            file_path: str = data.get("file_path", "")
            target: str = data.get("target", "all")

            if self._client is not None and self._client.is_authenticated:
                stream_id = self._client.start_file_transfer(file_path, target)
                if stream_id is not None:
                    self._send_json_response(
                        client_socket,
                        200,
                        {"status": "ok", "stream_id": stream_id, "message": "Bắt đầu truyền file"},
                    )
                else:
                    self._send_json_response(
                        client_socket, 400, {"status": "error", "message": "File không tồn tại"}
                    )
            else:
                self._send_json_response(
                    client_socket, 401, {"status": "error", "message": "Chưa đăng nhập"}
                )
        except json.JSONDecodeError:
            self._send_json_response(
                client_socket, 400, {"status": "error", "message": "JSON không hợp lệ"}
            )
        client_socket.close()

    def _handle_file_stream_upload(
        self,
        client_socket: socket.socket,
        query_params: dict,
        headers: dict,
        body_prefix: bytes,
        content_length: int,
    ) -> None:
        """Xử lý upload file nhị phân trực tiếp từ trình duyệt qua POST /api/file.

        Args:
            client_socket: Socket client.
            query_params: Query params từ URL.
            headers: Headers của request.
            body_prefix: Dữ liệu body đã đọc sẵn.
            content_length: Tổng độ dài body.
        """
        try:
            if self._client is None or not self._client.is_authenticated:
                self._send_json_response(
                    client_socket, 401, {"status": "error", "message": "Chưa đăng nhập"}
                )
                client_socket.close()
                return

            raw_filename = (
                query_params.get("filename", [""])[0]
                or headers.get("x-filename", "")
                or "uploaded_file"
            )
            raw_filename = unquote(raw_filename)
            safe_filename = os.path.basename(raw_filename) or "uploaded_file"

            raw_target = (
                query_params.get("target", ["all"])[0]
                or headers.get("x-target", "")
                or "all"
            )
            target = unquote(raw_target) if raw_target else "all"

            # Lưu vào thư mục tạm thời riêng biệt theo từng lượt gửi
            upload_subfolder = os.path.join(
                tempfile.gettempdir(),
                "streamchat_uploads",
                f"{int(time.time()*1000)}_{uuid.uuid4().hex[:6]}",
            )
            os.makedirs(upload_subfolder, exist_ok=True)
            saved_path = os.path.join(upload_subfolder, safe_filename)

            with open(saved_path, "wb") as f:
                if body_prefix:
                    f.write(body_prefix)
                bytes_written = len(body_prefix)
                while bytes_written < content_length:
                    to_read = min(131072, content_length - bytes_written)
                    chunk = client_socket.recv(to_read)
                    if not chunk:
                        break
                    f.write(chunk)
                    bytes_written += len(chunk)

            stream_id = self._client.start_file_transfer(saved_path, target)
            if stream_id is not None:
                self._send_json_response(
                    client_socket,
                    200,
                    {
                        "status": "ok",
                        "stream_id": stream_id,
                        "filename": safe_filename,
                        "message": "Bắt đầu truyền file",
                    },
                )
            else:
                self._send_json_response(
                    client_socket,
                    400,
                    {"status": "error", "message": "Không thể bắt đầu truyền file"},
                )
        except Exception as exc:
            logger.error("Lỗi nhận file upload: %s", exc)
            self._send_json_response(
                client_socket, 500, {"status": "error", "message": str(exc)}
            )
        finally:
            try:
                client_socket.close()
            except OSError:
                pass

    def _handle_status(self, client_socket: socket.socket) -> None:
        """Xử lý GET /api/status.

        Args:
            client_socket: Socket client.
        """
        status: dict = {
            "authenticated": self._client.is_authenticated if self._client else False,
            "username": self._client.username if self._client else None,
            "connected": self._client is not None and self._client._running,
        }
        self._send_json_response(client_socket, 200, status)
        client_socket.close()

    def _handle_progress(self, client_socket: socket.socket) -> None:
        """Xử lý GET /api/progress.

        Args:
            client_socket: Socket client.
        """
        progress: dict = {}
        if self._client is not None:
            progress = self._client.get_file_progress()
        self._send_json_response(client_socket, 200, progress)
        client_socket.close()

    # ─────────────────────── HTTP helpers ────────────────────────

    def _send_response(
        self,
        client_socket: socket.socket,
        status_code: int,
        content_type: str,
        body: bytes,
    ) -> None:
        """Gửi HTTP response thô.

        Args:
            client_socket: Socket client.
            status_code: Mã trạng thái HTTP.
            content_type: MIME type.
            body: Nội dung phản hồi.
        """
        status_text: str = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found"}.get(
            status_code, "Unknown"
        )
        header: str = (
            f"HTTP/1.1 {status_code} {status_text}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Access-Control-Allow-Origin: *\r\n"
            "Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n"
            "Access-Control-Allow-Headers: Content-Type, X-Filename, X-Target\r\n"
            "Connection: close\r\n"
            "\r\n"
        )
        try:
            client_socket.sendall(header.encode("utf-8") + body)
        except OSError as exc:
            logger.debug("Gửi response thất bại: %s", exc)

    def _send_json_response(
        self,
        client_socket: socket.socket,
        status_code: int,
        data: dict,
    ) -> None:
        """Gửi JSON response.

        Args:
            client_socket: Socket client.
            status_code: Mã trạng thái HTTP.
            data: Dict dữ liệu.
        """
        body: bytes = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._send_response(client_socket, status_code, "application/json", body)

    # ─────────────────────── Shutdown ───────────────────────

    def stop(self) -> None:
        """Dừng HTTP server."""
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        with self._sse_lock:
            for sock in self._sse_clients:
                try:
                    sock.close()
                except OSError:
                    pass
            self._sse_clients.clear()
        logger.info("HTTP UI đã dừng")
