"""
run_client.py
~~~~~~~~~~~~~
Script khởi chạy máy khách StreamChat với giao diện web cục bộ (HttpUI):
- Kết nối UDP bảo mật (AES-GCM, Selective ACK, BBR/CUBIC) đến máy chủ StreamChat.
- Phục vụ Web UI tại cổng HTTP cục bộ sử dụng raw TCP socket (không framework ngoài).
- Tự động tìm kiếm cổng rảnh (Auto Port Hunting) nếu cổng mặc định (8080) đang bận.

Sử dụng:
    python run_client.py [--server-ip IP] [--server-port PORT] [--http-port PORT] [--no-browser]
"""

from __future__ import annotations

import argparse
from pathlib import Path
import socket
import sys
import threading
import time
import webbrowser

# Thêm thư mục src vào sys.path để import các module
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

# Đảm bảo UTF-8 output trên Windows console
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from common.constants import (
    DEFAULT_SERVER_HOST,
    DEFAULT_SERVER_PORT,
    DEFAULT_HTTP_PORT,
)
from client.client import ChatClient
from client.http_ui import HttpUI


def main() -> None:
    """Entry point cho client."""
    parser = argparse.ArgumentParser(description="StreamChat Distributed Client")
    parser.add_argument(
        "--server-ip",
        type=str,
        default=DEFAULT_SERVER_HOST,
        help=f"IP máy chủ StreamChat (mặc định: {DEFAULT_SERVER_HOST})",
    )
    parser.add_argument(
        "--server-port",
        type=int,
        default=DEFAULT_SERVER_PORT,
        help=f"Cổng UDP máy chủ StreamChat (mặc định: {DEFAULT_SERVER_PORT})",
    )
    parser.add_argument(
        "--http-port",
        type=int,
        default=DEFAULT_HTTP_PORT,
        help=f"Cổng HTTP Web UI ban đầu (mặc định: {DEFAULT_HTTP_PORT})",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Không tự động mở trình duyệt web",
    )
    args = parser.parse_args()

    # 1. Tạo ChatClient (UDP Transport kết nối tới server_port)
    client = ChatClient(
        server_ip=args.server_ip,
        server_port=args.server_port,
    )

    # 2. Tạo HttpUI và bind ngay vào cổng TCP khả dụng để giữ chỗ độc quyền
    http_ui = HttpUI(port=args.http_port, client=client)
    actual_http_port = http_ui.bind_available_port()

    if actual_http_port != args.http_port:
        print(f"[THÔNG BÁO] Cổng {args.http_port} đang bận, tự động chuyển sang cổng {actual_http_port}", flush=True)

    url = f"http://127.0.0.1:{actual_http_port}"
    print("===============================================================", flush=True)
    print("                    STREAMCHAT CLIENT", flush=True)
    print("===============================================================", flush=True)
    print(f"  * UDP Server đích:        {args.server_ip}:{args.server_port} (UDP)", flush=True)
    print(f"  * Giao diện Web UI cục bộ: {url}", flush=True)
    print("  * Mã hóa truyền tải:       AES-GCM 256-bit + Selective Repeat", flush=True)
    print("===============================================================", flush=True)

    # 3. Tự động mở trình duyệt web
    if not args.no_browser:
        def _open_tab():
            time.sleep(0.6)
            webbrowser.open(url)
        threading.Thread(target=_open_tab, daemon=True).start()
    else:
        print(f"\nVui lòng mở trình duyệt và truy cập: {url}")

    # 4. Khởi chạy HttpUI (blocking)
    try:
        http_ui.start()
    except KeyboardInterrupt:
        print("\n[HỆ THỐNG] Client đang dừng...")
        http_ui.stop()
        client.disconnect()
        sys.exit(0)


if __name__ == "__main__":
    main()

