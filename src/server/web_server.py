"""
server/web_server.py
~~~~~~~~~~~~~~~~~~~~
Unified HTTP Web and Server-Sent Events (SSE) server on a single port (default 8080)
using raw TCP sockets following standard Client-Server architecture.

Key characteristics:
- No external frameworks (Flask, Django, FastAPI) and NO WebSocket library.
- Pure Raw TCP Socket (socket.SOCK_STREAM) for parsing HTTP/1.1 requests.
- Real-time event streaming via Server-Sent Events (SSE / text-event-stream).
- Concurrently manages multiple independent client sessions on the same host:port.
- Supports public chat room (Broadcast), private messaging (Unicast), and binary file transfer.
"""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import os
from pathlib import Path
import socket
import threading
import time
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qs, unquote, urlsplit
import uuid

from server.database import db as user_db

logger = logging.getLogger(__name__)

# Project root and static / upload asset directories
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
STATIC_DIR = Path(__file__).resolve().parent.parent / "client" / "static"
UPLOADS_DIR = PROJECT_ROOT / "uploads"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)


class SSEClient:
    """Represents an active Server-Sent Events connection over HTTP/1.1 from a browser."""

    def __init__(self, sock: socket.socket, addr: Tuple[str, int], username: str) -> None:
        self.sock = sock
        self.addr = addr
        self.username = username
        self.connected_at = time.time()
        self._send_lock = threading.Lock()
        self.is_closed = False

    def send_event(self, data: dict) -> bool:
        """Send a JSON payload formatted as an SSE stream event: data: {...}\n\n."""
        if self.is_closed:
            return False
        try:
            payload = f"data: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")
            with self._send_lock:
                self.sock.sendall(payload)
            return True
        except OSError:
            self.close()
            return False

    def send_heartbeat(self) -> bool:
        """Send periodic SSE comment to keep the HTTP keep-alive connection active."""
        if self.is_closed:
            return False
        try:
            with self._send_lock:
                self.sock.sendall(b": keepalive\n\n")
            return True
        except OSError:
            self.close()
            return False

    def close(self) -> None:
        """Safely close the SSE client connection."""
        if not self.is_closed:
            self.is_closed = True
            try:
                self.sock.close()
            except Exception:
                pass


class UnifiedChatServer:
    """Central HTTP + Server-Sent Events (SSE) server on port 8080."""

    def __init__(self, host: str = "0.0.0.0", port: int = 8080) -> None:
        self.host = host
        self.port = port
        self.running = False
        self.sock: Optional[socket.socket] = None

        # Manage active SSE clients: Set[SSEClient]
        self._clients: Set[SSEClient] = set()
        self._clients_lock = threading.Lock()

        # Manage uploaded files registry: file_id -> info
        self._files_registry: Dict[str, dict] = {}
        self._files_lock = threading.Lock()

    def start(self) -> None:
        """Initialize TCP socket and begin listening for connections."""
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((self.host, self.port))
        self.sock.listen(128)
        self.running = True

        display_host = "127.0.0.1" if self.host == "0.0.0.0" else self.host
        print("=" * 65)
        print("   STREAMCHAT UNIFIED SERVER (HTTP & SSE - NO WEBSOCKET)")
        print(f"   * Web UI URL:        http://{display_host}:{self.port}")
        print(f"   * Real-time SSE:     http://{display_host}:{self.port}/api/events (SSE)")
        print("   * 100% Pure HTTP/1.1 on Raw TCP Socket")
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
            logger.info("Server shutting down...")
        finally:
            self.stop()

    def stop(self) -> None:
        """Stop server and terminate all active connections."""
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

    # ------------------- User Management -------------------

    def get_online_users(self) -> List[str]:
        """Get list of currently online usernames."""
        with self._clients_lock:
            users = {
                client.username
                for client in self._clients
                if client.username and not client.is_closed
            }
        return sorted(list(users))

    def broadcast_event(self, event: dict, exclude: Optional[SSEClient] = None) -> None:
        """Broadcast an event to all connected active SSE clients."""
        with self._clients_lock:
            target_clients = [
                c for c in self._clients
                if c != exclude and not c.is_closed
            ]
        for client in target_clients:
            client.send_event(event)

    def send_to_user(self, target_username: str, event: dict) -> bool:
        """Send an event to all active connections of a specific user (unicast)."""
        sent = False
        with self._clients_lock:
            target_clients = [
                c for c in self._clients
                if c.username == target_username and not c.is_closed
            ]
        for client in target_clients:
            if client.send_event(event):
                sent = True
        return sent

    # ------------------- TCP & HTTP Request Handling -------------------

    def _handle_connection(self, client_sock: socket.socket, addr: Tuple[str, int]) -> None:
        """Handle incoming connection and route HTTP requests."""
        try:
            client_sock.settimeout(30.0)
            try:
                client_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                client_sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 262144)
                client_sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 262144)
            except OSError:
                pass

            raw_data = bytearray()
            # Read until headers boundary \r\n\r\n
            while b"\r\n\r\n" not in raw_data:
                chunk = client_sock.recv(4096)
                if not chunk:
                    client_sock.close()
                    return
                raw_data.extend(chunk)
                if len(raw_data) > 65536:  # 64KB headers limit
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
            content_length = int(headers.get("content-length", 0))

            # 1. Server-Sent Events Stream (Persistent long-lived connection)
            if method == "GET" and path == "/api/events":
                self._handle_sse_stream(client_sock, addr, query_params, headers)
                return

            # 2. Standard HTTP Request-Response handling
            self._handle_http_request(
                client_sock, method, path, query_params, headers, body_prefix, content_length
            )
        except Exception as exc:
            logger.debug("Connection handling error from %s: %s", addr, exc)
            try:
                client_sock.close()
            except Exception:
                pass

    def _handle_sse_stream(
        self,
        sock: socket.socket,
        addr: Tuple[str, int],
        query_params: dict,
        headers: dict,
    ) -> None:
        """Establish real-time Server-Sent Events (SSE) connection."""
        username = query_params.get("username", [""])[0] or headers.get("x-username", "")
        username = unquote(username).strip().lower()

        if not username:
            self._send_http_response(
                sock, 400, "application/json", b'{"status":"error","message":"Missing username"}'
            )
            sock.close()
            return

        # Terminate previous session if same username logs in from another tab or device
        with self._clients_lock:
            old_sessions = [c for c in self._clients if c.username == username and not c.is_closed]
        for old_c in old_sessions:
            old_c.send_event({
                "type": "KICKED",
                "message": "Your account has been logged in from another tab or device.",
            })
            old_c.close()

        # Send HTTP 200 SSE header
        sse_header = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: text/event-stream; charset=utf-8\r\n"
            "Cache-Control: no-cache, no-transform\r\n"
            "Connection: keep-alive\r\n"
            "Access-Control-Allow-Origin: *\r\n"
            "X-Accel-Buffering: no\r\n\r\n"
        )
        try:
            sock.sendall(sse_header.encode("utf-8"))
        except OSError:
            sock.close()
            return

        sock.settimeout(None)  # Remove timeout for long-lived SSE
        client = SSEClient(sock, addr, username)
        with self._clients_lock:
            self._clients.add(client)

        logger.info("[SSE] User '%s' connected stream from %s:%d", username, addr[0], addr[1])

        # Send initial data to client
        client.send_event({
            "type": "AUTH_RESP",
            "success": True,
            "username": username,
            "message": f"Welcome {username}! Logged in successfully.",
        })
        online_users = self.get_online_users()
        self.broadcast_event({
            "type": "USER_LIST",
            "users": online_users,
        })
        self.broadcast_event({
            "type": "SYSTEM",
            "message": f"User @{username} has joined the chat room.",
        }, exclude=client)

        # Heartbeat loop
        try:
            while self.running and not client.is_closed:
                time.sleep(15)
                if not client.send_heartbeat():
                    break
        finally:
            self._disconnect_client(client)

    def _disconnect_client(self, client: SSEClient) -> None:
        """Clean up when an SSE client disconnects."""
        client.close()
        with self._clients_lock:
            self._clients.discard(client)

        username = client.username
        if username:
            still_online = any(
                c.username == username and not c.is_closed
                for c in self._clients
            )
            if not still_online:
                self.broadcast_event({
                    "type": "USER_LIST",
                    "users": self.get_online_users(),
                })
                self.broadcast_event({
                    "type": "SYSTEM",
                    "message": f"User @{username} has left the chat room.",
                })
            logger.info("[SSE] Client '%s' disconnected", username)

    # ------------------- HTTP Request-Response Handling -------------------

    def _read_full_body(
        self, sock: socket.socket, body_prefix: bytes, content_length: int
    ) -> bytes:
        """Read complete HTTP request body."""
        body = bytearray(body_prefix)
        while len(body) < content_length:
            to_read = min(65536, content_length - len(body))
            chunk = sock.recv(to_read)
            if not chunk:
                break
            body.extend(chunk)
        return bytes(body)

    def _handle_http_request(
        self,
        sock: socket.socket,
        method: str,
        path: str,
        query_params: dict,
        headers: dict,
        body_prefix: bytes,
        content_length: int,
    ) -> None:
        """Serve static files and REST API endpoints."""
        try:
            # 1. Serve home page
            if method == "GET" and path in ("/", "/index.html"):
                self._serve_file(sock, STATIC_DIR / "index.html")

            # 2. Serve static assets (CSS, JS, Fonts, Images)
            elif method == "GET" and path.startswith("/static/"):
                rel_path = path[len("/static/"):].lstrip("/")
                target_file = (STATIC_DIR / rel_path).resolve()
                if STATIC_DIR in target_file.parents and target_file.is_file():
                    self._serve_file(sock, target_file)
                else:
                    self._send_http_response(sock, 404, "text/plain", b"File not found")

            # 3. Login API
            elif method == "POST" and path == "/api/login":
                body = self._read_full_body(sock, body_prefix, content_length)
                try:
                    data = json.loads(body.decode("utf-8"))
                    username = str(data.get("username", "")).strip().lower()
                    password = str(data.get("password", "")).strip()

                    if not username or not password:
                        self._send_http_response(
                            sock, 400, "application/json",
                            json.dumps({"status": "error", "message": "Please provide both username and password"}).encode("utf-8")
                        )
                        return

                    p_hash = hashlib.sha256(password.encode("utf-8")).hexdigest()
                    if user_db.verify_user(username, p_hash):
                        self._send_http_response(
                            sock, 200, "application/json",
                            json.dumps({
                                "status": "ok",
                                "username": username,
                                "message": f"Welcome {username}! Login successful.",
                            }).encode("utf-8")
                        )
                    else:
                        self._send_http_response(
                            sock, 401, "application/json",
                            json.dumps({"status": "error", "message": "Incorrect username or password."}).encode("utf-8")
                        )
                except Exception as exc:
                    self._send_http_response(
                        sock, 400, "application/json",
                        json.dumps({"status": "error", "message": str(exc)}).encode("utf-8")
                    )

            # 4. Logout API
            elif method == "POST" and path == "/api/logout":
                username = query_params.get("username", [""])[0]
                if username:
                    with self._clients_lock:
                        user_clients = [c for c in self._clients if c.username == username]
                    for c in user_clients:
                        c.close()
                self._send_http_response(sock, 200, "application/json", b'{"status":"ok"}')

            # 5. Send Chat Message API
            elif method == "POST" and path == "/api/send":
                body = self._read_full_body(sock, body_prefix, content_length)
                try:
                    data = json.loads(body.decode("utf-8"))
                    sender = str(data.get("from", "")).strip().lower()
                    target = str(data.get("target", "all")).strip().lower()
                    text = str(data.get("text", "")).strip()

                    if not text:
                        self._send_http_response(sock, 400, "application/json", b'{"status":"error","message":"Message is empty"}')
                        return

                    msg_event = {
                        "type": "MSG",
                        "from": sender or "Anonymous",
                        "target": target,
                        "text": text,
                        "timestamp": time.time(),
                        "is_private": target not in ("all", "", "general"),
                    }

                    if target in ("all", "", "general"):
                        self.broadcast_event(msg_event)
                    else:
                        self.send_to_user(target, msg_event)
                        if sender and sender != target:
                            self.send_to_user(sender, msg_event)

                    self._send_http_response(sock, 200, "application/json", b'{"status":"ok"}')
                except Exception as exc:
                    self._send_http_response(sock, 400, "application/json", json.dumps({"status": "error", "message": str(exc)}).encode("utf-8"))

            # 6. File Upload via HTTP POST
            elif method == "POST" and path == "/api/file":
                self._handle_file_upload(sock, query_params, headers, body_prefix, content_length)

            # 7. File Download
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

            # 8. Server Status & Online Users
            elif method == "GET" and path in ("/api/status", "/api/users"):
                data = {
                    "status": "online",
                    "online_users": self.get_online_users(),
                    "total_clients": len(self._clients),
                }
                body = json.dumps(data).encode("utf-8")
                self._send_http_response(sock, 200, "application/json", body)

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
        """Read and store binary file uploaded from browser."""
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

        # Read binary file body
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
                "sender": sender or "Anonymous",
                "target": target,
            }

        # Broadcast real-time SSE notification to recipient
        file_event = {
            "type": "FILE_READY",
            "stream_id": file_id,
            "from": sender or "A user",
            "target": target,
            "filename": safe_filename,
            "size": file_size,
            "download_url": f"/api/download?id={file_id}",
            "timestamp": time.time(),
        }

        if target in ("all", "", "general"):
            self.broadcast_event(file_event)
        else:
            self.send_to_user(target, file_event)
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
        """Serve static file content with appropriate MIME type."""
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
        """Construct and transmit standard HTTP response."""
        status_text = {
            200: "OK",
            404: "Not Found",
            400: "Bad Request",
            401: "Unauthorized",
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
    """Utility function to launch UnifiedChatServer."""
    server = UnifiedChatServer(host=host, port=port)
    server.start()
    return server


if __name__ == "__main__":
    start_unified_server()
