"""
run_client.py
~~~~~~~~~~~~~
Script khởi chạy máy khách với giao diện web.

Sử dụng:
    python run_client.py [--server-ip IP] [--server-port PORT] [--http-port PORT]
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

from common.constants import (
    DEFAULT_SERVER_HOST,
    DEFAULT_SERVER_PORT,
    DEFAULT_HTTP_PORT,
)
from client.client import ChatClient
from client.http_ui import HttpUI


def main() -> None:
    """Entry point cho client."""
    parser = argparse.ArgumentParser(description="StreamChat Client")
    parser.add_argument(
        "--server-ip",
        type=str,
        default=DEFAULT_SERVER_HOST,
        help=f"IP server (mặc định: {DEFAULT_SERVER_HOST})",
    )
    parser.add_argument(
        "--server-port",
        type=int,
        default=DEFAULT_SERVER_PORT,
        help=f"Cổng UDP server (mặc định: {DEFAULT_SERVER_PORT})",
    )
    parser.add_argument(
        "--http-port",
        type=int,
        default=DEFAULT_HTTP_PORT,
        help=f"Cổng HTTP cho giao diện web (mặc định: {DEFAULT_HTTP_PORT})",
    )
    args = parser.parse_args()

    print("===========================================")
    print("  StreamChat Client")
    print(f"  Server: {args.server_ip}:{args.server_port}")
    print(f"  Web UI: http://127.0.0.1:{args.http_port}")
    print("===========================================")

    # Tạo ChatClient (chưa connect – đợi user đăng nhập qua web)
    client = ChatClient(
        server_ip=args.server_ip,
        server_port=args.server_port,
    )

    # Tạo HTTP UI server
    http_ui = HttpUI(port=args.http_port, client=client)

    # Chạy HTTP UI (blocking)
    try:
        http_ui.start()
    except KeyboardInterrupt:
        print("\nClient đã dừng.")
        http_ui.stop()
        client.disconnect()
        sys.exit(0)


if __name__ == "__main__":
    main()
