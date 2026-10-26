"""
run_server.py
~~~~~~~~~~~~~
Script khởi chạy máy chủ trung tâm StreamChat:
- Web UI & SSE Server: Lắng nghe trên cổng 8080 (Client-Server HTTP & SSE chuẩn).
- UDP Transport Engine: Lắng nghe trên cổng UDP 9000 (đáp ứng tiêu chuẩn truyền tải phân tán).

Sử dụng:
    python run_server.py [--host HOST] [--port UDP_PORT] [--web-port WEB_PORT]
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import threading

# Thêm thư mục src vào sys.path để import các module
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

# Đảm bảo UTF-8 output trên Windows console
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from common.constants import DEFAULT_SERVER_HOST, DEFAULT_SERVER_PORT, DEFAULT_HTTP_PORT
from server.server import ChatServer
from server.web_server import UnifiedChatServer


def main() -> None:
    """Entry point cho server."""
    parser = argparse.ArgumentParser(description="StreamChat Unified Server")
    parser.add_argument(
        "--host",
        type=str,
        default=DEFAULT_SERVER_HOST,
        help=f"Địa chỉ IP lắng nghe (mặc định: {DEFAULT_SERVER_HOST})",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_SERVER_PORT,
        help=f"Cổng UDP (mặc định: {DEFAULT_SERVER_PORT})",
    )
    parser.add_argument(
        "--web-port",
        type=int,
        default=DEFAULT_HTTP_PORT,
        help=f"Cổng Web UI và SSE (mặc định: {DEFAULT_HTTP_PORT})",
    )
    args = parser.parse_args()

    print("===============================================================")
    print("                 STREAMCHAT CENTRAL SERVER")
    print("===============================================================")
    print(f"  * Web UI & SSE Server:    http://127.0.0.1:{args.web_port}")
    print(f"  * UDP Transport Engine:   127.0.0.1:{args.port} (UDP)")
    print("  * Ho tro hang tram tai khoan mo tren cung dia chi:cong 8080")
    print("===============================================================")

    # 1. Khởi chạy UDP ChatServer trong thread nền
    udp_server = ChatServer()
    udp_thread = threading.Thread(
        target=udp_server.start_server,
        kwargs={"host": args.host, "port": args.port},
        daemon=True,
    )
    udp_thread.start()

    # 2. Khởi chạy Web & WebSocket Server trên cổng 8080 (blocking)
    web_server = UnifiedChatServer(host="0.0.0.0", port=args.web_port)
    try:
        web_server.start()
    except KeyboardInterrupt:
        print("\n[HỆ THỐNG] Máy chủ StreamChat đã dừng.")
        web_server.stop()
        sys.exit(0)


if __name__ == "__main__":
    main()
